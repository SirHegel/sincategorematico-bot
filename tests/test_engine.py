from __future__ import annotations

import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from sincategorematico_bot import engine as engine_module
from sincategorematico_bot.config import BotConfig
from sincategorematico_bot.engine import (
    Engine,
    EngineAlreadyRunning,
    acquire_engine_lock,
    linkedin_ready,
    release_engine_lock,
)
from sincategorematico_bot.linkedin import LinkedInClient, LinkedInError, TokenBundle
from sincategorematico_bot.sources import FeedEntry, FeedError, FeedResult
from sincategorematico_bot.runtime import apply_defaults
from sincategorematico_bot.storage import StateStore
from sincategorematico_bot.writer import Draft, WriterAccountsUnavailable, WriterError


def setUpModule() -> None:
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:
    logging.disable(logging.NOTSET)


CONFIG = BotConfig(
    display_name="Prueba",
    timezone="America/Bogota",
    poll_timeout_seconds=25,
    max_retry_seconds=60,
    paused_by_default=False,
    max_posts_per_day=4,
)


def feed(*titles: str) -> FeedResult:
    return FeedResult(
        entries=[
            FeedEntry(
                guid=f"guid-{title}",
                url=f"https://ejemplo.test/{title}",
                title=title,
                summary=f"Resumen de {title}",
                published_at=None,
            )
            for title in titles
        ],
        etag='"abc"',
        modified=None,
    )


class StubWriter:
    def __init__(self, draft: Draft | None = None, error: Exception | None = None) -> None:
        self.draft = draft or Draft(body="Cuerpo del borrador. " * 10, discarded=False, reason="")
        self.error = error
        self.calls = 0

    def available(self) -> bool:
        return True

    def compose(self, **_kwargs: object) -> Draft:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.draft


class StubClient:
    def __init__(self, error: LinkedInError | None = None) -> None:
        self.error = error
        self.posts: list[dict[str, object]] = []
        self.calls = 0

    def create_post(self, **kwargs: object) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.posts.append(kwargs)
        return "urn:li:share:7000"


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.directory.name) / "state.db")
        apply_defaults(self.store, CONFIG)
        for source in self.store.sources():
            self.store.remove_source(int(source["id"]))
        self.source_id = self.store.add_source("https://ejemplo.test/feed", "Ejemplo")
        self.writer = StubWriter()
        self.engine = Engine(store=self.store, config=CONFIG, writer=self.writer)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def make_ready_to_publish(self) -> int:
        self.store.set("publishing_paused", False)
        self.store.set("publish_window_start", "00:00")
        self.store.set("publish_window_end", "23:59")
        self.store.set("min_gap_minutes", 0)
        draft_id = self.store.add_draft(item_id=None, body="Cuerpo", link=None, title="Titular")
        self.store.set_draft_state(draft_id, "approved")
        return draft_id

    def link_personal_account(self) -> None:
        self.store.set("linkedin_access_token", "token")
        self.store.set("linkedin_author_urn", "urn:li:person:abc")
        self.store.set("linkedin_expires_at", int(time.time()) + 3600)
        self.store.set("linkedin_scope", "openid profile w_member_social")


class IngestTests(EngineTestCase):
    def test_new_entries_are_stored_once(self) -> None:
        with mock.patch.object(engine_module, "fetch_feed", return_value=feed("uno", "dos")):
            self.assertEqual(self.engine.ingest(), 2)
            self.assertEqual(self.engine.ingest(), 0)
        self.assertEqual(self.store.count_items_by_state()["new"], 2)

    def test_a_failing_source_is_recorded_and_skipped(self) -> None:
        with mock.patch.object(engine_module, "fetch_feed", side_effect=FeedError("HTTP 500")):
            self.assertEqual(self.engine.ingest(), 0)
        self.assertIn("error", str(self.store.sources()[0]["last_status"]))

    def test_unchanged_feeds_keep_their_validators(self) -> None:
        result = FeedResult(entries=[], etag='"nuevo"', modified="ayer", not_modified=True)
        with mock.patch.object(engine_module, "fetch_feed", return_value=result):
            self.engine.ingest()
        self.assertEqual(self.store.sources()[0]["etag"], '"nuevo"')
        self.assertEqual(self.store.sources()[0]["last_status"], "sin cambios")


class ComposeTests(EngineTestCase):
    def add_news(self) -> None:
        with mock.patch.object(engine_module, "fetch_feed", return_value=feed("uno")):
            self.engine.ingest()

    def test_a_draft_waits_for_approval_by_default(self) -> None:
        self.add_news()
        self.assertTrue(self.engine.compose_next())
        drafts = self.store.drafts_by_state("pending")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(self.store.count_items_by_state()["drafted"], 1)

    def test_without_manual_approval_the_draft_is_ready(self) -> None:
        self.store.set("approval_required", False)
        self.add_news()
        self.engine.compose_next()
        self.assertEqual(len(self.store.drafts_by_state("approved")), 1)

    def test_a_discarded_news_item_creates_no_draft(self) -> None:
        self.engine.writer = StubWriter(Draft(body="", discarded=True, reason="no encaja"))
        self.add_news()
        self.assertTrue(self.engine.compose_next())
        self.assertEqual(self.store.count_by_state(), {})

    def test_the_queue_target_limits_the_writing(self) -> None:
        with mock.patch.object(engine_module, "fetch_feed", return_value=feed("uno", "dos", "tres")):
            self.engine.ingest()
        self.store.set("queue_target", 2)
        self.assertTrue(self.engine.compose_next())
        self.assertTrue(self.engine.compose_next())
        self.assertFalse(self.engine.compose_next())
        self.assertEqual(self.writer.calls, 2)

    def test_a_writer_failure_pauses_instead_of_burning_the_news(self) -> None:
        self.engine.writer = StubWriter(error=WriterError("la CLI no respondió"))
        self.add_news()
        self.assertFalse(self.engine.compose_next())
        self.assertEqual(self.store.count_items_by_state()["new"], 1)
        # La espera evita insistir contra una CLI caída en cada ciclo.
        self.assertFalse(self.engine.compose_next())
        self.assertEqual(self.engine.writer.calls, 1)

    def test_unavailable_accounts_pause_without_consuming_the_news_attempts(self) -> None:
        self.engine.writer = StubWriter(
            error=WriterAccountsUnavailable(
                "todas las cuentas esperan recarga",
                retry_after_seconds=120,
            )
        )
        self.add_news()

        for _ in range(engine_module.MAX_COMPOSE_ATTEMPTS + 1):
            self.engine._writer_ready_at = 0.0
            self.assertFalse(self.engine.compose_next())

        item = self.store.next_item(max_age_seconds=48 * 3600)
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "new")
        self.assertEqual(item["attempts"], 0)
        self.assertEqual(
            self.engine.writer.calls,
            engine_module.MAX_COMPOSE_ATTEMPTS + 1,
        )

    def test_the_news_is_dropped_after_repeated_failures(self) -> None:
        self.engine.writer = StubWriter(error=WriterError("la CLI no respondió"))
        self.add_news()
        for _ in range(engine_module.MAX_COMPOSE_ATTEMPTS):
            self.engine._writer_ready_at = 0.0
            self.engine.compose_next()
        self.assertEqual(self.store.count_items_by_state()["skipped"], 1)


class PublishTests(EngineTestCase):
    def test_simulation_preserves_the_real_queue_and_limits(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.assertTrue(self.engine.publish_next())
        draft = self.store.draft(draft_id)
        self.assertEqual(draft["state"], "approved")
        self.assertIsNone(draft["post_urn"])
        self.assertIsNotNone(draft["simulated_at"])
        self.assertEqual(self.store.count_published_since(0), 0)
        self.assertFalse(self.engine.publish_next())

    def test_simulation_advances_past_an_already_simulated_draft(self) -> None:
        first_id = self.make_ready_to_publish()
        second_id = self.store.add_draft(
            item_id=None, body="Segundo cuerpo", link=None, title="Segundo titular"
        )
        self.store.set_draft_state(second_id, "approved")

        self.assertTrue(self.engine.publish_next())
        self.assertTrue(self.engine.publish_next())
        self.assertFalse(self.engine.publish_next())

        self.assertIsNotNone(self.store.draft(first_id)["simulated_at"])
        self.assertIsNotNone(self.store.draft(second_id)["simulated_at"])

    def test_real_mode_requires_a_linked_account(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.store.set("dry_run", False)
        self.assertFalse(self.engine.publish_next())
        self.assertEqual(self.store.draft(draft_id)["state"], "approved")

    def test_real_mode_sends_the_post_and_saves_the_urn(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.store.set("dry_run", False)
        self.link_personal_account()
        self.assertTrue(linkedin_ready(self.store))
        client = StubClient()
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertTrue(self.engine.publish_next())
        self.assertEqual(client.posts[0]["author_urn"], "urn:li:person:abc")
        self.assertEqual(self.store.draft(draft_id)["post_urn"], "urn:li:share:7000")

    def test_an_expired_access_token_is_refreshed_before_readiness_is_rejected(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.store.set("dry_run", False)
        self.store.set("linkedin_access_token", "token-vencido")
        self.store.set("linkedin_author_urn", "urn:li:person:abc")
        self.store.set("linkedin_expires_at", int(time.time()) - 1)
        self.store.set("linkedin_scope", "openid profile w_member_social")
        self.store.set("linkedin_refresh_token", "refresh-valido")
        bundle = TokenBundle(
            access_token="token-renovado",
            expires_at=int(time.time()) + 3600,
            refresh_token=None,
            refresh_expires_at=None,
            # LinkedIn puede omitir scope al conservar el alcance original.
            scope="",
        )

        with (
            mock.patch.dict(
                engine_module.os.environ,
                {
                    "SINCATEGOREMATICO_LINKEDIN_CLIENT_ID": "cliente",
                    "SINCATEGOREMATICO_LINKEDIN_CLIENT_SECRET": "secreto",
                },
            ),
            mock.patch.object(engine_module, "refresh_token", return_value=bundle) as refresh,
            mock.patch.object(
                LinkedInClient, "create_post", return_value="urn:li:share:renovado"
            ),
        ):
            self.assertTrue(self.engine.publish_next())

        refresh.assert_called_once()
        self.assertEqual(self.store.draft(draft_id)["state"], "published")
        self.assertEqual(self.store.get("linkedin_access_token"), "token-renovado")
        self.assertEqual(self.store.get("linkedin_scope"), "openid profile w_member_social")

    def test_an_expired_access_token_without_refresh_stays_strictly_blocked(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.store.set("dry_run", False)
        self.store.set("linkedin_access_token", "token-vencido")
        self.store.set("linkedin_author_urn", "urn:li:person:abc")
        self.store.set("linkedin_expires_at", int(time.time()) - 1)
        self.store.set("linkedin_scope", "w_member_social")

        with (
            mock.patch.object(engine_module, "refresh_token") as refresh,
            mock.patch.object(LinkedInClient, "create_post") as create_post,
        ):
            self.assertFalse(self.engine.publish_next())

        refresh.assert_not_called()
        create_post.assert_not_called()
        self.assertEqual(self.store.draft(draft_id)["state"], "approved")

    def test_an_authentication_error_stops_retrying(self) -> None:
        draft_id = self.make_ready_to_publish()
        next_id = self.store.add_draft(
            item_id=None, body="Siguiente", link=None, title="Siguiente"
        )
        self.store.set_draft_state(next_id, "approved")
        self.store.set("dry_run", False)
        self.link_personal_account()
        client = StubClient(LinkedInError("sin permiso", status=403))
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertFalse(self.engine.publish_next())
        draft = self.store.draft(draft_id)
        self.assertEqual(draft["state"], "failed")
        self.assertIn("sin permiso", str(draft["last_error"]))
        self.assertTrue(self.store.get_bool("linkedin_auth_blocked"))
        self.assertFalse(linkedin_ready(self.store))
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertFalse(self.engine.publish_next())
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.store.draft(next_id)["state"], "approved")

    def test_an_ambiguous_server_error_is_never_retried_automatically(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.store.set("dry_run", False)
        self.link_personal_account()
        client = StubClient(LinkedInError("servicio no disponible", status=503, ambiguous=True))
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertFalse(self.engine.publish_next())
        self.assertEqual(self.store.draft(draft_id)["state"], "uncertain")
        self.assertEqual(self.store.draft(draft_id)["attempts"], 1)
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertFalse(self.engine.publish_next())
        self.assertEqual(client.calls, 1)

    def test_an_uncertain_post_blocks_the_next_approved_post(self) -> None:
        first_id = self.make_ready_to_publish()
        second_id = self.store.add_draft(
            item_id=None, body="Segundo", link=None, title="Segundo"
        )
        self.store.set_draft_state(second_id, "approved")
        self.store.set("dry_run", False)
        self.link_personal_account()
        ambiguous = StubClient(
            LinkedInError("respuesta perdida", status=503, ambiguous=True)
        )
        with mock.patch.object(Engine, "client", return_value=ambiguous):
            self.assertFalse(self.engine.publish_next())
        self.assertEqual(self.store.draft(first_id)["state"], "uncertain")

        next_client = StubClient()
        with mock.patch.object(Engine, "client", return_value=next_client):
            self.assertFalse(self.engine.publish_next())

        self.assertEqual(next_client.calls, 0)
        self.assertEqual(self.store.draft(second_id)["state"], "approved")

    def test_an_unexpected_exception_after_claim_becomes_uncertain_immediately(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.store.set("dry_run", False)
        self.link_personal_account()

        class UnexpectedClient:
            def create_post(self, **_kwargs: object) -> str:
                raise RuntimeError("fallo inesperado")

        with mock.patch.object(Engine, "client", return_value=UnexpectedClient()):
            self.assertFalse(self.engine.publish_next())

        draft = self.store.draft(draft_id)
        self.assertEqual(draft["state"], "uncertain")
        self.assertIn("Verifica LinkedIn", str(draft["last_error"]))

    def test_blank_success_identifier_never_counts_as_published(self) -> None:
        draft_id = self.make_ready_to_publish()
        self.store.set("dry_run", False)
        self.link_personal_account()

        class BlankIdentifierClient:
            def create_post(self, **_kwargs: object) -> str:
                return "   "

        with mock.patch.object(Engine, "client", return_value=BlankIdentifierClient()):
            self.assertFalse(self.engine.publish_next())

        self.assertEqual(self.store.draft(draft_id)["state"], "uncertain")
        self.assertEqual(self.store.count_published_since(0), 0)
    def test_rate_limit_uses_retry_after_without_losing_the_draft(self) -> None:
        draft_id = self.make_ready_to_publish()
        next_id = self.store.add_draft(
            item_id=None, body="Siguiente", link=None, title="Siguiente"
        )
        self.store.set_draft_state(next_id, "approved")
        self.store.set("dry_run", False)
        self.link_personal_account()
        client = StubClient(LinkedInError("demasiadas solicitudes", status=429, retry_after=600))
        before = int(time.time())
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertFalse(self.engine.publish_next())
        draft = self.store.draft(draft_id)
        self.assertEqual(draft["state"], "approved")
        self.assertGreaterEqual(int(draft["retry_at"]), before + 600)
        self.assertEqual(
            self.store.get_int("linkedin_retry_after_until"), int(draft["retry_at"])
        )
        with mock.patch.object(Engine, "client", return_value=client):
            self.assertFalse(self.engine.publish_next())
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.store.draft(next_id)["state"], "approved")

    def test_expired_or_under_scoped_credentials_are_not_ready(self) -> None:
        self.store.set("linkedin_access_token", "token")
        self.store.set("linkedin_author_urn", "urn:li:person:abc")
        self.store.set("linkedin_expires_at", int(time.time()) + 3600)
        self.store.set("linkedin_scope", "openid profile")
        self.assertFalse(linkedin_ready(self.store))
        self.store.set("linkedin_scope", "w_member_social")
        self.assertTrue(linkedin_ready(self.store))
        self.store.set("linkedin_expires_at", int(time.time()) - 1)
        self.assertFalse(linkedin_ready(self.store))

    def test_interrupted_inflight_post_becomes_uncertain_on_recovery(self) -> None:
        draft_id = self.make_ready_to_publish()
        claimed = self.store.claim_next_approved()
        self.assertEqual(claimed["id"], draft_id)
        self.assertEqual(self.store.recover_inflight_publications(), 1)
        self.assertEqual(self.store.draft(draft_id)["state"], "uncertain")

    def test_a_paused_engine_publishes_nothing(self) -> None:
        self.make_ready_to_publish()
        self.store.set("publishing_paused", True)
        self.assertFalse(self.engine.publish_next())


class EngineLockTests(unittest.TestCase):
    def test_a_second_engine_cannot_share_the_same_state_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            state_path = Path(raw_directory) / "state.db"
            first = acquire_engine_lock(state_path)
            try:
                with self.assertRaises(EngineAlreadyRunning):
                    acquire_engine_lock(state_path)
            finally:
                release_engine_lock(first)

            replacement = acquire_engine_lock(state_path)
            release_engine_lock(replacement)


if __name__ == "__main__":
    unittest.main()
