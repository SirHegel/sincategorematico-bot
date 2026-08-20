from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from sincategorematico_bot.config import BotConfig
from sincategorematico_bot.runtime import (
    DEFAULT_SOURCES,
    apply_defaults,
    load_rules,
    parse_clock,
    publication_gate,
    snapshot,
)
from sincategorematico_bot.storage import StateStore

BOGOTA = ZoneInfo("America/Bogota")


def moment(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 8, day, hour, minute, tzinfo=BOGOTA).timestamp()


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.directory.name) / "state.db")
        self.config = BotConfig(
            display_name="Prueba",
            timezone="America/Bogota",
            poll_timeout_seconds=25,
            max_retry_seconds=60,
            paused_by_default=True,
            max_posts_per_day=2,
        )
        apply_defaults(self.store, self.config)
        self.store.set("publishing_paused", False)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def rules(self):
        return load_rules(self.store, self.config)

    def test_defaults_seed_the_initial_sources_only_once(self) -> None:
        self.assertEqual(len(self.store.sources()), len(DEFAULT_SOURCES))
        apply_defaults(self.store, self.config)
        self.assertEqual(len(self.store.sources()), len(DEFAULT_SOURCES))

    def test_pause_blocks_publication(self) -> None:
        self.store.set("publishing_paused", True)
        gate = publication_gate(self.store, self.rules(), moment=moment(18, 10))
        self.assertFalse(gate)
        self.assertIn("pausa", gate.reason)

    def test_real_publication_waits_while_an_uncertain_post_is_unreconciled(self) -> None:
        self.store.set("dry_run", False)
        draft_id = self.store.add_draft(
            item_id=None, body="x" * 200, link=None, title="incierto"
        )
        self.store.set_draft_state(draft_id, "uncertain")

        gate = publication_gate(self.store, self.rules(), moment=moment(18, 10))

        self.assertFalse(gate)
        self.assertIn("sin conciliar", gate.reason)

    def test_manual_reconciliation_counts_for_daily_limit_and_gap(self) -> None:
        self.store.set("dry_run", False)
        self.store.set("min_gap_minutes", 120)
        draft_id = self.store.add_draft(
            item_id=None, body="x" * 200, link=None, title="incierto"
        )
        self.store.set_draft_state(draft_id, "uncertain")
        published_at = moment(18, 9)
        with mock.patch(
            "sincategorematico_bot.storage.time.time", return_value=published_at
        ):
            self.assertTrue(
                self.store.reconcile_draft_as_published(
                    draft_id, "urn:li:activity:123456789"
                )
            )

        during_gap = publication_gate(
            self.store, self.rules(), moment=moment(18, 10)
        )
        after_gap = publication_gate(
            self.store, self.rules(), moment=moment(18, 11)
        )
        self.assertFalse(during_gap)
        self.assertIn("separación mínima", during_gap.reason)
        self.assertTrue(after_gap)
        self.assertEqual(self.store.count_published_since(moment(18, 0)), 1)

    def test_outside_the_window_publication_waits(self) -> None:
        gate = publication_gate(self.store, self.rules(), moment=moment(18, 5))
        self.assertFalse(gate)
        self.assertIn("franja", gate.reason)
        self.assertGreater(gate.retry_after, 0)

    def test_inside_the_window_publication_is_allowed(self) -> None:
        self.assertTrue(publication_gate(self.store, self.rules(), moment=moment(18, 10)))

    def test_overnight_window_wraps_around_midnight(self) -> None:
        self.store.set("publish_window_start", "22:00")
        self.store.set("publish_window_end", "02:00")
        self.assertTrue(publication_gate(self.store, self.rules(), moment=moment(18, 23)))
        self.assertTrue(publication_gate(self.store, self.rules(), moment=moment(18, 1)))
        self.assertFalse(publication_gate(self.store, self.rules(), moment=moment(18, 12)))

    def test_daily_limit_stops_the_engine(self) -> None:
        self.store.set("min_gap_minutes", 0)
        with mock.patch(
            "sincategorematico_bot.storage.time.time", return_value=moment(18, 9)
        ):
            for _ in range(2):
                draft_id = self.store.add_draft(item_id=None, body="x" * 200, link=None, title="t")
                self.store.mark_draft_published(draft_id, "urn:li:share:1")
        gate = publication_gate(self.store, self.rules(), moment=moment(18, 10))
        self.assertFalse(gate)
        self.assertIn("límite diario", gate.reason)

    def test_minimum_gap_between_posts_is_respected(self) -> None:
        with mock.patch(
            "sincategorematico_bot.storage.time.time", return_value=moment(18, 9, 30)
        ):
            draft_id = self.store.add_draft(item_id=None, body="x" * 200, link=None, title="t")
            self.store.mark_draft_published(draft_id, "urn:li:share:1")
        gate = publication_gate(self.store, self.rules(), moment=moment(18, 10))
        self.assertFalse(gate)
        self.assertIn("separación mínima", gate.reason)

    def test_broken_clock_settings_fall_back_to_the_default(self) -> None:
        self.assertEqual(parse_clock("25:99", "07:30").hour, 7)
        self.assertEqual(parse_clock(None, "08:15").minute, 15)

    def test_snapshot_exposes_queue_and_linkedin_state(self) -> None:
        draft_id = self.store.add_draft(item_id=None, body="cuerpo " * 40, link="https://x.test", title="Titular")
        data = snapshot(self.store, self.config)
        self.assertEqual(data["counts"]["pending"], 1)
        self.assertEqual(data["drafts"][0]["id"], draft_id)
        self.assertFalse(data["linkedin"]["linked"])
        self.assertTrue(data["dry_run"])

    def test_snapshot_never_exposes_credentials(self) -> None:
        self.store.set("linkedin_access_token", "secreto-de-acceso")
        self.store.set("linkedin_refresh_token", "secreto-de-refresco")
        self.store.set("linkedin_author_urn", "urn:li:person:abc")
        self.store.set("linkedin_expires_at", int(__import__("time").time()) + 3600)
        self.store.set("linkedin_scope", "w_member_social")
        data = snapshot(self.store, self.config)
        self.assertTrue(data["linkedin"]["linked"])
        self.assertNotIn("secreto", repr(data))


if __name__ == "__main__":
    unittest.main()
