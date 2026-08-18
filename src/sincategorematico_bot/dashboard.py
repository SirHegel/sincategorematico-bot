from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse

from .config import load_config
from .storage import StateStore


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
STATE = Path(os.environ.get("SINCATEGOREMATICO_STATE_PATH", Path.home() / ".local/state/sincategorematico-bot/state.db"))
CONFIG = Path(os.environ.get("SINCATEGOREMATICO_CONFIG_PATH", ROOT / "config.toml"))
SESSION_TOKEN = os.environ.get("SINCATEGOREMATICO_DASHBOARD_TOKEN", "").strip()
HOST, PORT = "127.0.0.1", int(os.environ.get("SINCATEGOREMATICO_DASHBOARD_PORT", "8765"))


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SincategorematicoDashboard"
    failed_logins: dict[str, list[float]] = {}

    def log_message(self, format: str, *args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        supplied = cookie.get("sinc_session")
        return bool(SESSION_TOKEN and supplied and hmac.compare_digest(supplied.value, SESSION_TOKEN))

    def _read_json(self) -> dict[str, object]:
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        return json.loads(self.rfile.read(length) or b"{}")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        return origin in {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            if not self._authenticated():
                self._json({"authenticated": False}, HTTPStatus.UNAUTHORIZED)
                return
            store = StateStore(STATE)
            config = load_config(CONFIG)
            payload = {
                "authenticated": True,
                "paused": store.get_bool("publishing_paused", default=True),
                "owner": store.get_int("owner_user_id") is not None,
                "initialized": store.get_bool("telegram_initialized"),
                "max_posts": config.max_posts_per_day,
                "timezone": config.timezone,
                "activity": store.recent_activity(),
            }
            store.close()
            self._json(payload)
            return
        asset = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js", "/logo.svg": "logo.svg"}.get(path)
        if not asset:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = (WEB / asset).read_bytes()
        content_type = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}[Path(asset).suffix]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._same_origin():
            self._json({"error": "Origen rechazado"}, HTTPStatus.FORBIDDEN)
            return
        path = urlparse(self.path).path
        if path == "/api/login":
            now = time.monotonic()
            attempts = [stamp for stamp in self.failed_logins.get(self.client_address[0], []) if now - stamp < 60]
            if len(attempts) >= 5:
                self._json({"error": "Espera un minuto antes de intentar otra vez"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            supplied = str(self._read_json().get("token", ""))
            if not SESSION_TOKEN or not hmac.compare_digest(supplied, SESSION_TOKEN):
                attempts.append(now)
                self.failed_logins[self.client_address[0]] = attempts
                self._json({"error": "Clave incorrecta"}, HTTPStatus.UNAUTHORIZED)
                return
            self.failed_logins.pop(self.client_address[0], None)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Set-Cookie", f"sinc_session={SESSION_TOKEN}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800")
            self.end_headers()
            return
        if path == "/api/control":
            if not self._authenticated():
                self._json({"error": "Sesión requerida"}, HTTPStatus.UNAUTHORIZED)
                return
            action = str(self._read_json().get("action", ""))
            if action not in {"pause", "resume"}:
                self._json({"error": "Acción inválida"}, HTTPStatus.BAD_REQUEST)
                return
            store = StateStore(STATE)
            paused = action == "pause"
            store.set("publishing_paused", paused)
            store.add_activity("control", f"Publicaciones {'pausadas' if paused else 'reanudadas'} desde panel web")
            store.close()
            self._json({"ok": True, "paused": paused})
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    if not SESSION_TOKEN or len(SESSION_TOKEN) < 24:
        raise SystemExit("Configura SINCATEGOREMATICO_DASHBOARD_TOKEN con al menos 24 caracteres")
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Panel local disponible en http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
