#!/usr/bin/env python3
"""Vincula una cuenta de LinkedIn con el bot mediante OAuth de 3 patas."""
from __future__ import annotations

import argparse
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from configure_token import ENV_PATH, read_environment, write_environment  # noqa: E402
from sincategorematico_bot.config import load_config  # noqa: E402
from sincategorematico_bot.linkedin import (  # noqa: E402
    LinkedInClient,
    LinkedInError,
    MEMBER_SCOPES,
    ORGANIZATION_SCOPES,
    authorization_url,
    exchange_code,
)
from sincategorematico_bot.runtime import apply_defaults  # noqa: E402
from sincategorematico_bot.storage import StateStore  # noqa: E402

CLIENT_ID_KEY = "SINCATEGOREMATICO_LINKEDIN_CLIENT_ID"
CLIENT_SECRET_KEY = "SINCATEGOREMATICO_LINKEDIN_CLIENT_SECRET"
DEFAULT_PORT = 8770
STATE_PATH = Path(
    os.environ.get(
        "SINCATEGOREMATICO_STATE_PATH",
        str(Path.home() / ".local/state/sincategorematico-bot/state.db"),
    )
)

PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Sincategoremático</title></head><body style="font-family:system-ui;background:#080a15;color:#f5f6ff;
display:grid;place-items:center;height:100vh;margin:0"><div style="text-align:center">
<h1 style="color:#9b91ff">{titulo}</h1><p>{mensaje}</p></div></body></html>"""


def force_safe_linkedin_state(store: StateStore) -> None:
    """Una nueva vinculación nunca reactiva una configuración real anterior."""

    store.set("publishing_paused", True)
    store.set("dry_run", True)


def prepare_linkedin_reauthorization() -> None:
    """Pausa el motor antes incluso de abrir el flujo OAuth en el navegador."""

    store = StateStore(STATE_PATH)
    try:
        apply_defaults(store, load_config(PROJECT_ROOT / "config.toml"))
        force_safe_linkedin_state(store)
        store.add_activity(
            "seguridad", "Vinculación de LinkedIn iniciada; motor pausado y en simulación"
        )
    finally:
        store.close()


class CallbackHandler(BaseHTTPRequestHandler):
    received: dict[str, str] = {}
    expected_state: str = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        received = {key: values[0] for key, values in query.items()}
        if "code" not in received and "error" not in received:
            self.send_error(400, "Falta el resultado de LinkedIn")
            return
        if not secrets.compare_digest(received.get("state", ""), self.expected_state):
            self.send_error(401, "El parámetro state no coincide")
            return
        CallbackHandler.received = received
        ok = "code" in received
        message = (
            "Ya puedes cerrar esta pestaña y volver a la terminal."
            if ok
            else received.get("error_description", "LinkedIn no devolvió un código.")
        )
        body = PAGE.format(
            titulo="Autorización recibida" if ok else "Autorización cancelada",
            mensaje=html.escape(message),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def ask(prompt: str, *, current: str = "", secret: bool = False) -> str:
    suffix = f" [{'guardado' if secret and current else current}]" if current else ""
    if secret:
        import getpass

        value = getpass.getpass(f"{prompt}{suffix}: ").strip()
    else:
        value = input(f"{prompt}{suffix}: ").strip()
    return value or current


def wait_for_callback(port: int, state: str, *, timeout: int = 300) -> dict[str, str]:
    CallbackHandler.received = {}
    CallbackHandler.expected_state = state
    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    print(f"Esperando la respuesta de LinkedIn en http://localhost:{port}/callback …")
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while not CallbackHandler.received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not CallbackHandler.received:
        return {"error": "callback_timeout", "error_description": "La autorización no llegó en 5 minutos"}
    return dict(CallbackHandler.received)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organizacion", metavar="URN_O_ID", help="publicar como página de empresa")
    parser.add_argument("--puerto", type=int, default=DEFAULT_PORT, help="puerto del callback local")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="no levantar servidor local; pegarás a mano la URL de retorno",
    )
    arguments = parser.parse_args()

    try:
        prepare_linkedin_reauthorization()
    except Exception as exc:
        print(f"No fue posible poner el motor en modo seguro: {exc}", file=sys.stderr)
        return 2

    values = read_environment(ENV_PATH)
    client_id = ask("Client ID de la app de LinkedIn", current=values.get(CLIENT_ID_KEY, ""))
    client_secret = ask(
        "Client Secret", current=values.get(CLIENT_SECRET_KEY, ""), secret=True
    )
    if not client_id or not client_secret:
        print("Faltan las credenciales de la aplicación.", file=sys.stderr)
        return 2

    if arguments.manual:
        redirect_uri = ask("URL de redirección registrada en el portal")
        if not redirect_uri:
            print("Se necesita la URL de redirección exacta.", file=sys.stderr)
            return 2
    else:
        redirect_uri = f"http://localhost:{arguments.puerto}/callback"
        print(f"\nRegistra esta URL de redirección en la app de LinkedIn:\n  {redirect_uri}\n")

    scopes = ORGANIZATION_SCOPES if arguments.organizacion else MEMBER_SCOPES
    state = secrets.token_urlsafe(24)
    url = authorization_url(
        client_id=client_id, redirect_uri=redirect_uri, scopes=scopes, state=state
    )
    print("Abre esta dirección y autoriza la aplicación:\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    if arguments.manual:
        returned = ask("Pega aquí la URL completa a la que te redirigió LinkedIn")
        payload = {k: v[0] for k, v in parse_qs(urlsplit(returned).query).items()}
    else:
        payload = wait_for_callback(arguments.puerto, state)

    if payload.get("error"):
        print(f"LinkedIn devolvió un error: {payload.get('error_description', payload['error'])}", file=sys.stderr)
        return 3
    if payload.get("state") != state:
        print("El parámetro state no coincide; se aborta por seguridad.", file=sys.stderr)
        return 4
    code = payload.get("code", "")
    if not code:
        print("No se recibió el código de autorización.", file=sys.stderr)
        return 3

    try:
        bundle = exchange_code(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
        client = LinkedInClient(bundle.access_token)
        if arguments.organizacion:
            raw = arguments.organizacion.strip()
            author = raw if raw.startswith("urn:li:organization:") else f"urn:li:organization:{raw}"
            profile_name = author
        else:
            author = client.member_urn()
            profile_name = str(client.userinfo().get("name", author))
    except LinkedInError as exc:
        print(f"LinkedIn rechazó la vinculación: {exc}", file=sys.stderr)
        return 5

    values[CLIENT_ID_KEY] = client_id
    values[CLIENT_SECRET_KEY] = client_secret
    write_environment(ENV_PATH, values)

    store = StateStore(STATE_PATH)
    apply_defaults(store, load_config(PROJECT_ROOT / "config.toml"))
    store.set("linkedin_access_token", bundle.access_token)
    store.set("linkedin_expires_at", bundle.expires_at)
    store.set("linkedin_scope", bundle.scope)
    store.set("linkedin_author_urn", author)
    force_safe_linkedin_state(store)
    store.delete("linkedin_auth_blocked")
    store.delete("linkedin_token_warning")
    store.delete("linkedin_token_warning_sent")
    if bundle.refresh_token:
        store.set("linkedin_refresh_token", bundle.refresh_token)
    else:
        store.delete("linkedin_refresh_token")
    if bundle.refresh_expires_at:
        store.set("linkedin_refresh_expires_at", bundle.refresh_expires_at)
    else:
        store.delete("linkedin_refresh_expires_at")
    store.add_activity("linkedin", f"Cuenta vinculada: {profile_name}")
    store.close()

    # Un motor ya activo leyó el entorno anterior al arrancar. try-restart no
    # inicia servicios aún no instalados, pero sí aplica las credenciales nuevas.
    subprocess.run(
        ["systemctl", "--user", "try-restart", "sincategorematico-engine.service"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    days = max(0, (bundle.expires_at - int(__import__("time").time())) // 86400)
    print(f"\nCuenta vinculada: {profile_name}")
    print(f"Autor de las publicaciones: {author}")
    print(f"El acceso caduca en {days} días.")
    if not bundle.refresh_token:
        print("La app no tiene renovación automática: repite este paso antes de que caduque.")
    print("\nQuedó en pausa y simulación. Cuando hayas revisado todo, envía al bot:")
    print("  /resume")
    print("  /publicacion real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
