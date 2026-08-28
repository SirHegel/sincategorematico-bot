from __future__ import annotations

from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from sincategorematico_bot import dashboard as dashboard_module
from sincategorematico_bot.dashboard import DashboardHandler
from sincategorematico_bot.storage import StateStore


class DashboardControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "state.db"

    def tearDown(self) -> None:
        DashboardHandler.active_sessions.clear()
        DashboardHandler.failed_logins.clear()
        self.directory.cleanup()

    def test_login_issues_an_opaque_server_session_not_the_master_key(self) -> None:
        master_key = "dashboard-master-key-for-testing-only"
        encoded = json.dumps({"token": master_key}).encode()
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = "/api/login"
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        handler._same_origin = lambda: True
        captured_headers: list[tuple[str, str]] = []
        handler.send_response = lambda _status: None
        handler.send_header = lambda name, value: captured_headers.append((name, value))
        handler.end_headers = lambda: None

        with mock.patch.object(dashboard_module, "SESSION_TOKEN", master_key):
            handler.do_POST()

        cookie_header = dict(captured_headers)["Set-Cookie"]
        self.assertNotIn(master_key, cookie_header)
        self.assertNotIn("\r", cookie_header)
        self.assertNotIn("\n", cookie_header)
        parsed = SimpleCookie(cookie_header)
        session_identifier = parsed["sinc_session"].value
        self.assertGreaterEqual(len(session_identifier), 32)
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("SameSite=Strict", cookie_header)

        subsequent = DashboardHandler.__new__(DashboardHandler)
        subsequent.headers = {"Cookie": f"sinc_session={session_identifier}"}
        self.assertTrue(subsequent._authenticated())

    def test_unknown_and_expired_session_cookies_are_rejected(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.headers = {"Cookie": "sinc_session=unknown"}
        self.assertFalse(handler._authenticated())

        DashboardHandler.active_sessions["expired"] = time.monotonic() - 1
        handler.headers = {"Cookie": "sinc_session=expired"}
        self.assertFalse(handler._authenticated())

    def test_cookie_serializer_rejects_response_splitting_characters(self) -> None:
        with self.assertRaises(CookieError):
            DashboardHandler._session_cookie("fixture\r\nX-Injected: yes")

    def post(self, path: str, payload: dict[str, object]) -> tuple[object, int]:
        encoded = json.dumps(payload).encode()
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        handler._same_origin = lambda: True
        handler._authenticated = lambda: True
        response: dict[str, object] = {}

        def capture(body: object, status: int = 200) -> None:
            response["body"] = body
            response["status"] = int(status)

        handler._json = capture
        handler.send_error = lambda status: capture({"error": "HTTP"}, int(status))
        with mock.patch.object(dashboard_module, "STATE", self.db_path):
            handler.do_POST()
        return response["body"], int(response["status"])

    def test_retry_is_an_explicit_dashboard_action_for_uncertain_drafts(self) -> None:
        store = StateStore(self.db_path)
        draft_id = store.add_draft(
            item_id=None,
            body="Cuerpo",
            link=None,
            title="Publicación incierta",
        )
        store.set_draft_state(draft_id, "publishing")
        store.mark_draft_uncertain(draft_id, "Verifica LinkedIn antes de reintentar")
        store.close()

        body, status = self.post(
            "/api/draft", {"action": "retry", "id": draft_id}
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body, {"ok": True, "state": "approved"})
        store = StateStore(self.db_path)
        try:
            retried = store.draft(draft_id)
            self.assertEqual(retried["state"], "approved")
            self.assertEqual(retried["attempts"], 0)
            self.assertIsNone(retried["last_error"])
            self.assertIsNone(retried["retry_at"])
            self.assertIsNone(retried["publish_started_at"])
        finally:
            store.close()

    def test_confirm_reconciles_an_uncertain_draft_without_requeueing(self) -> None:
        store = StateStore(self.db_path)
        draft_id = store.add_draft(
            item_id=None, body="Cuerpo", link=None, title="Publicación incierta"
        )
        store.set_draft_state(draft_id, "uncertain")
        store.close()

        body, status = self.post(
            "/api/draft",
            {
                "action": "confirm",
                "id": draft_id,
                "reference": "urn:li:activity:123456789",
            },
        )

        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body, {"ok": True, "state": "published"})
        store = StateStore(self.db_path)
        try:
            confirmed = store.draft(draft_id)
            self.assertEqual(confirmed["state"], "published")
            self.assertEqual(confirmed["post_urn"], "urn:li:activity:123456789")
        finally:
            store.close()

    def test_real_mode_is_blocked_for_expired_or_under_scoped_tokens(self) -> None:
        cases = (
            (int(time.time()) - 1, "w_member_social", "caducado"),
            (int(time.time()) + 3_600, "openid profile", "sin permiso"),
        )
        for expires_at, scope, label in cases:
            with self.subTest(label=label):
                store = StateStore(self.db_path)
                store.set("dry_run", True)
                store.set("linkedin_access_token", "token")
                store.set("linkedin_author_urn", "urn:li:person:abc")
                store.set("linkedin_expires_at", expires_at)
                store.set("linkedin_scope", scope)
                store.close()

                body, status = self.post(
                    "/api/settings",
                    {
                        "max_posts": 4,
                        "timezone": "America/Bogota",
                        "window_start": "07:30",
                        "window_end": "20:30",
                        "approval_required": True,
                        "dry_run": False,
                    },
                )

                self.assertEqual(status, HTTPStatus.CONFLICT)
                self.assertIn("Vincula LinkedIn", body["error"])
                store = StateStore(self.db_path)
                try:
                    self.assertTrue(store.get_bool("dry_run", default=True))
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
