from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import time
import unittest

from sincategorematico_bot.app import BotApplication, Identity, parse_command
from sincategorematico_bot.config import BotConfig
from sincategorematico_bot.runtime import ENGINE_HEARTBEAT_TTL
from sincategorematico_bot.storage import StateStore


class FakeAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> dict[str, object]:
        self.messages.append((chat_id, text))
        return {}


def private_update(user_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


def group_update(user_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": -999, "type": "group"},
            "text": text,
        },
    }


class BotApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary_directory.name) / "state.db")
        self.api = FakeAPI()
        self.claim_code = "codigo-prueba-seguro"
        self.application = BotApplication(
            api=self.api,  # type: ignore[arg-type]
            store=self.store,
            config=BotConfig(
                display_name="Prueba",
                timezone="America/Bogota",
                poll_timeout_seconds=25,
                max_retry_seconds=60,
                paused_by_default=True,
                max_posts_per_day=4,
            ),
            claim_sha256=hashlib.sha256(self.claim_code.encode()).hexdigest(),
            claim_expires_at=int(
                (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
            ),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_parse_command_removes_bot_suffix(self) -> None:
        self.assertEqual(parse_command(" /STATUS@ExampleBot "), ("status", ""))

    def test_claim_binds_both_user_and_chat(self) -> None:
        self.application.handle_update(private_update(123, f"/claim {self.claim_code}"))
        self.assertEqual(self.application.owner(), Identity(user_id=123, chat_id=123))
        self.assertIn("Vinculación completada", self.api.messages[-1][1])

    def test_wrong_claim_does_not_bind_owner(self) -> None:
        self.application.handle_update(private_update(123, "/claim incorrecto"))
        self.assertIsNone(self.application.owner())
        self.assertIn("incorrecto", self.api.messages[-1][1])

    def test_claim_is_ignored_outside_private_chat(self) -> None:
        self.application.handle_update(group_update(123, f"/claim {self.claim_code}"))
        self.assertIsNone(self.application.owner())
        self.assertEqual(self.api.messages, [])

    def test_non_owner_cannot_change_pause_state(self) -> None:
        self.application.handle_update(private_update(123, f"/claim {self.claim_code}"))
        self.application.handle_update(private_update(456, "/resume"))
        self.assertTrue(self.store.get_bool("publishing_paused"))
        self.assertIn("no autorizado", self.api.messages[-1][1].lower())

    def test_owner_can_resume_and_check_status(self) -> None:
        self.application.handle_update(private_update(123, f"/claim {self.claim_code}"))
        self.application.handle_update(private_update(123, "/resume"))
        self.application.handle_update(private_update(123, "/status"))
        self.assertFalse(self.store.get_bool("publishing_paused"))
        self.assertIn("Estado: en línea", self.api.messages[-1][1])

    def test_status_reports_stale_engine_heartbeat_and_uncertain_posts(self) -> None:
        self.application.handle_update(private_update(123, f"/claim {self.claim_code}"))
        draft_id = self.store.add_draft(
            item_id=None, body="Texto", link=None, title="Incierto"
        )
        self.store.set_draft_state(draft_id, "uncertain")
        self.store.set("engine_status", "activo")
        self.store.set(
            "engine_heartbeat_at", int(time.time()) - ENGINE_HEARTBEAT_TTL - 10
        )

        self.application.handle_update(private_update(123, "/status"))

        status = self.api.messages[-1][1]
        self.assertIn("SIN PULSO", status)
        self.assertIn("inciertos: 1", status)

    def test_owner_can_confirm_an_uncertain_post_without_retrying_it(self) -> None:
        self.application.handle_update(private_update(123, f"/claim {self.claim_code}"))
        draft_id = self.store.add_draft(
            item_id=None, body="Texto", link=None, title="Incierto"
        )
        self.store.set_draft_state(draft_id, "uncertain")

        self.application.handle_update(
            private_update(
                123,
                f"/confirmar {draft_id} "
                "https://www.linkedin.com/feed/update/urn:li:activity:123456789/",
            )
        )

        draft = self.store.draft(draft_id)
        self.assertEqual(draft["state"], "published")
        self.assertEqual(draft["post_urn"], "urn:li:activity:123456789")
        self.assertIn("sin repetir el POST", self.api.messages[-1][1])


if __name__ == "__main__":
    unittest.main()
