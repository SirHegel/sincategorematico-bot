from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from sincategorematico_bot import engine as engine_module
from sincategorematico_bot import runtime as runtime_module
from sincategorematico_bot.config import BotConfig
from sincategorematico_bot.engine import Engine
from sincategorematico_bot.linkedin import LinkedInError, linkedin_credentials_usable
from sincategorematico_bot.runtime import (
    ENGINE_HEARTBEAT_TTL,
    apply_defaults,
    load_rules,
    publication_gate,
    snapshot,
)
from sincategorematico_bot.storage import StateStore


CONFIG = BotConfig(
    display_name="Prueba de seguridad",
    timezone="America/Bogota",
    poll_timeout_seconds=25,
    max_retry_seconds=60,
    paused_by_default=False,
    max_posts_per_day=10,
)


class PassiveWriter:
    def available(self) -> bool:
        return True


class RecordingClient:
    def __init__(self, *, error: LinkedInError | None = None) -> None:
        self.error = error
        self.calls = 0

    def create_post(self, **_kwargs: object) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "urn:li:share:seguro"


class SimulatedProcessCrash(BaseException):
    """Imita una terminación que el bloque de errores ordinario no puede absorber."""


class PublishSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "state.db"
        self.store = StateStore(self.db_path)
        apply_defaults(self.store, CONFIG)
        self.store.set("publishing_paused", False)
        self.store.set("publish_window_start", "00:00")
        self.store.set("publish_window_end", "23:59")
        self.store.set("min_gap_minutes", 0)
        self.store.set("max_posts_per_day", 10)
        self.engine = Engine(store=self.store, config=CONFIG, writer=PassiveWriter())

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def approve_draft(self) -> int:
        draft_id = self.store.add_draft(
            item_id=None,
            body="Texto listo para publicar",
            link=None,
            title="Titular",
        )
        self.store.set_draft_state(draft_id, "approved")
        return draft_id

    def enable_real_linkedin(self) -> None:
        self.store.set("dry_run", False)
        self.store.set("linkedin_access_token", "token-valido")
        self.store.set("linkedin_author_urn", "urn:li:person:abc")
        self.store.set("linkedin_expires_at", int(time.time()) + 3600)
        self.store.set("linkedin_scope", "openid profile w_member_social")

    def crash_during_post(self, draft_id: int) -> None:
        class CrashingClient:
            def create_post(self, **_kwargs: object) -> str:
                raise SimulatedProcessCrash()

        with mock.patch.object(Engine, "client", return_value=CrashingClient()):
            with self.assertRaises(SimulatedProcessCrash):
                self.engine.publish_next()
        self.assertEqual(self.store.draft(draft_id)["state"], "publishing")

    def test_claim_is_committed_before_the_external_post(self) -> None:
        draft_id = self.approve_draft()
        self.enable_real_linkedin()
        observed: dict[str, object] = {}

        class ObservingClient:
            def create_post(_self, **_kwargs: object) -> str:
                # Una conexión independiente demuestra que el claim no sólo está
                # pendiente en la transacción del motor: ya es durable antes del POST.
                observer = StateStore(self.db_path)
                try:
                    current = observer.draft(draft_id)
                    observed["state"] = current["state"]
                    observed["started"] = current["publish_started_at"]
                    observed["second_claim"] = observer.claim_next_approved()
                finally:
                    observer.close()
                return "urn:li:share:atomic"

        with mock.patch.object(Engine, "client", return_value=ObservingClient()):
            self.assertTrue(self.engine.publish_next())

        self.assertEqual(observed["state"], "publishing")
        self.assertIsNotNone(observed["started"])
        self.assertIsNone(observed["second_claim"])
        self.assertEqual(self.store.draft(draft_id)["state"], "published")

    def test_two_engines_cannot_race_the_daily_limit_or_publication_gap(self) -> None:
        first_id = self.approve_draft()
        second_id = self.approve_draft()
        self.enable_real_linkedin()
        self.store.set("max_posts_per_day", 1)
        self.store.set("min_gap_minutes", 120)
        post_started = threading.Event()
        allow_post_to_finish = threading.Event()
        worker_result: list[bool] = []
        worker_error: list[BaseException] = []

        class BlockingClient:
            def create_post(self, **_kwargs: object) -> str:
                post_started.set()
                if not allow_post_to_finish.wait(5):
                    raise RuntimeError("la prueba no liberó el envío")
                return "urn:li:share:primero"

        def first_worker() -> None:
            worker_store = StateStore(self.db_path)
            try:
                worker_engine = Engine(
                    store=worker_store, config=CONFIG, writer=PassiveWriter()
                )
                worker_engine.client = lambda: BlockingClient()  # type: ignore[method-assign]
                worker_result.append(worker_engine.publish_next())
            except BaseException as exc:  # conserva el fallo para el hilo de prueba
                worker_error.append(exc)
            finally:
                worker_store.close()

        thread = threading.Thread(target=first_worker)
        thread.start()
        self.assertTrue(post_started.wait(5))

        competing_store = StateStore(self.db_path)
        competing_client = RecordingClient()
        try:
            competing_engine = Engine(
                store=competing_store, config=CONFIG, writer=PassiveWriter()
            )
            competing_engine.client = lambda: competing_client  # type: ignore[method-assign]
            # No espera a que termine el POST ni cruza la compuerta en paralelo.
            self.assertFalse(competing_engine.publish_next())
            self.assertEqual(competing_client.calls, 0)
        finally:
            allow_post_to_finish.set()
            thread.join(5)
            competing_store.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(worker_error, [])
        self.assertEqual(worker_result, [True])
        first = self.store.draft(first_id)
        second = self.store.draft(second_id)
        self.assertEqual(
            sorted((str(first["state"]), str(second["state"]))),
            ["approved", "published"],
        )
        self.assertEqual(self.store.count_published_since(0), 1)

        # Una vez libre el cerrojo, el límite ya confirmado en SQLite detiene
        # también un intento posterior del segundo motor.
        after_store = StateStore(self.db_path)
        try:
            after_engine = Engine(store=after_store, config=CONFIG, writer=PassiveWriter())
            after_engine.client = lambda: competing_client  # type: ignore[method-assign]
            self.assertFalse(after_engine.publish_next())
        finally:
            after_store.close()
        self.assertEqual(competing_client.calls, 0)

    def test_a_crash_after_claim_recovers_as_uncertain(self) -> None:
        draft_id = self.approve_draft()
        self.enable_real_linkedin()
        self.crash_during_post(draft_id)

        self.assertEqual(self.store.recover_inflight_publications(), 1)
        recovered = self.store.draft(draft_id)
        self.assertEqual(recovered["state"], "uncertain")
        self.assertIn("verifica LinkedIn", str(recovered["last_error"]))

    def test_uncertain_never_retries_until_a_human_explicitly_requeues_it(self) -> None:
        draft_id = self.approve_draft()
        self.enable_real_linkedin()
        self.crash_during_post(draft_id)
        self.store.recover_inflight_publications()

        client = RecordingClient()
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertFalse(self.engine.publish_next())
        self.assertEqual(client.calls, 0)
        self.assertEqual(self.store.draft(draft_id)["state"], "uncertain")

        self.assertTrue(self.store.retry_draft(draft_id))
        self.assertEqual(self.store.draft(draft_id)["state"], "approved")
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertTrue(self.engine.publish_next())
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.store.draft(draft_id)["state"], "published")

    def test_429_honors_retry_after_and_cannot_retry_early(self) -> None:
        draft_id = self.approve_draft()
        self.enable_real_linkedin()
        client = RecordingClient(
            error=LinkedInError("límite", status=429, retry_after=73)
        )
        fixed_now = 1_900_000_000
        self.store.set("linkedin_expires_at", fixed_now + 3_600)

        with mock.patch.object(engine_module.time, "time", return_value=fixed_now):
            with mock.patch.object(Engine, "client", return_value=client):
                self.assertFalse(self.engine.publish_next())

        scheduled = self.store.draft(draft_id)
        self.assertEqual(scheduled["state"], "approved")
        self.assertEqual(scheduled["retry_at"], fixed_now + 73)
        self.assertEqual(scheduled["attempts"], 1)
        self.assertIsNone(self.store.claim_next_approved(now=fixed_now + 72))
        claimed = self.store.claim_next_approved(now=fixed_now + 73)
        self.assertEqual(claimed["id"], draft_id)
        self.assertEqual(client.calls, 1)

    def test_dry_run_preserves_approval_counts_and_publication_gap(self) -> None:
        draft_id = self.approve_draft()
        self.store.set("dry_run", True)

        self.assertTrue(self.engine.publish_next())
        draft = self.store.draft(draft_id)
        self.assertEqual(draft["state"], "approved")
        self.assertIsNotNone(draft["simulated_at"])
        self.assertEqual(self.store.count_published_since(0), 0)
        self.assertIsNone(self.store.last_published_at())
        self.assertTrue(publication_gate(self.store, load_rules(self.store, CONFIG)))

    def test_paused_tick_only_refreshes_liveness(self) -> None:
        fixed_now = 1_900_000_123
        self.store.set("publishing_paused", True)
        self.store.set("engine_heartbeat_at", 1)

        with (
            mock.patch.object(engine_module.time, "time", return_value=fixed_now),
            mock.patch.object(self.engine, "ingest") as ingest,
            mock.patch.object(self.engine, "compose_next") as compose,
            mock.patch.object(self.engine, "warn_token_expiry") as warn,
            mock.patch.object(self.engine, "publish_next") as publish,
        ):
            self.engine.tick()

        self.assertEqual(self.store.get_int("engine_heartbeat_at"), fixed_now)
        self.assertEqual(self.store.get("engine_status"), "pausado")
        ingest.assert_not_called()
        compose.assert_not_called()
        warn.assert_not_called()
        publish.assert_not_called()

    def test_snapshot_distinguishes_recent_stale_and_future_heartbeats(self) -> None:
        now = 1_900_001_000
        self.store.set("engine_status", "activo")
        with mock.patch.object(runtime_module.time, "time", return_value=now):
            self.store.set("engine_heartbeat_at", now - ENGINE_HEARTBEAT_TTL)
            self.assertTrue(snapshot(self.store, CONFIG)["engine"]["alive"])

            self.store.set("engine_heartbeat_at", now - ENGINE_HEARTBEAT_TTL - 1)
            self.assertFalse(snapshot(self.store, CONFIG)["engine"]["alive"])

            self.store.set("engine_heartbeat_at", now + 1)
            self.assertFalse(snapshot(self.store, CONFIG)["engine"]["alive"])

            self.store.set("engine_heartbeat_at", now)
            self.store.set("engine_status", "detenido")
            self.assertFalse(snapshot(self.store, CONFIG)["engine"]["alive"])

    def test_token_expiry_and_author_specific_scope_are_mandatory(self) -> None:
        base = {
            "access_token": "token",
            "expires_at": 2_000,
            "now": 1_000,
        }
        self.assertTrue(
            linkedin_credentials_usable(
                **base,
                author_urn="urn:li:person:abc",
                scope="openid profile w_member_social",
            )
        )
        self.assertFalse(
            linkedin_credentials_usable(
                **base,
                author_urn="urn:li:person:abc",
                scope="openid profile w_organization_social",
            )
        )
        self.assertTrue(
            linkedin_credentials_usable(
                **base,
                author_urn="urn:li:organization:123",
                scope="w_organization_social",
            )
        )
        self.assertFalse(
            linkedin_credentials_usable(
                access_token="token",
                author_urn="urn:li:person:abc",
                expires_at=1_000,
                scope="w_member_social",
                now=1_000,
            )
        )

    def test_item_age_prefers_published_at_over_discovery_time(self) -> None:
        now = int(time.time())
        stale_id = self.store.add_item(
            source_id=None,
            guid="stale-published",
            url="https://example.test/stale",
            title="Vieja aunque recién descubierta",
            summary="",
            published_at=now - 7_200,
        )
        recent_id = self.store.add_item(
            source_id=None,
            guid="recent-published",
            url="https://example.test/recent",
            title="Reciente aunque descubierta antes",
            summary="",
            published_at=now - 60,
        )
        self.store._connection.execute(
            "UPDATE items SET discovered_at = ? WHERE id = ?", (now - 7_200, recent_id)
        )
        self.store._connection.commit()

        selected = self.store.next_item(max_age_seconds=3_600)
        self.assertEqual(selected["id"], recent_id)
        self.assertEqual(self.store.expire_items(max_age_seconds=3_600), 1)
        stale_state = self.store._connection.execute(
            "SELECT state FROM items WHERE id = ?", (stale_id,)
        ).fetchone()["state"]
        self.assertEqual(stale_state, "skipped")


if __name__ == "__main__":
    unittest.main()
