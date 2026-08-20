from __future__ import annotations

import logging
import os
from pathlib import Path
import random
import signal
import threading
import time

from .app import BotApplication
from .config import load_config
from .runtime import apply_defaults
from .storage import StateStore
from .telegram_api import TelegramAPI, TelegramAPIError


LOGGER = logging.getLogger(__name__)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable requerida {name}")
    return value


def run() -> None:
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(
        os.environ.get(
            "SINCATEGOREMATICO_CONFIG_PATH",
            str(project_root / "config.toml"),
        )
    )
    state_path = Path(
        os.environ.get(
            "SINCATEGOREMATICO_STATE_PATH",
            str(Path.home() / ".local/state/sincategorematico-bot/state.db"),
        )
    )
    config = load_config(config_path)
    token = required_env("SINCATEGOREMATICO_TELEGRAM_TOKEN")
    claim_sha256 = os.environ.get("SINCATEGOREMATICO_CLAIM_SHA256") or None
    claim_expires_raw = os.environ.get("SINCATEGOREMATICO_CLAIM_EXPIRES_AT")
    claim_expires_at = int(claim_expires_raw) if claim_expires_raw else None

    store = StateStore(state_path)
    apply_defaults(store, config)
    api = TelegramAPI(token, network_timeout=config.poll_timeout_seconds + 10)
    application = BotApplication(
        api=api,
        store=store,
        config=config,
        claim_sha256=claim_sha256,
        claim_expires_at=claim_expires_at,
    )
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Señal %s recibida; deteniendo el bot", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    first_initialization = not store.get_bool("telegram_initialized")
    api.delete_webhook(drop_pending_updates=first_initialization)
    api.set_commands()
    identity = api.get_me()
    store.set("telegram_initialized", True)
    LOGGER.info("Bot conectado como @%s", identity.get("username", "desconocido"))

    offset = store.get_int("next_update_offset") or 0
    retry_delay = 1.0
    try:
        while not stop_event.is_set():
            try:
                updates = api.get_updates(
                    offset=offset,
                    poll_timeout=config.poll_timeout_seconds,
                )
                retry_delay = 1.0
                for update in updates:
                    update_id = int(update["update_id"])
                    application.handle_update(update)
                    offset = max(offset, update_id + 1)
                    store.set("next_update_offset", offset)
                application.notify_pending_drafts()
                application.notify_alerts()
            except TelegramAPIError as exc:
                if exc.error_code == 401:
                    LOGGER.error("Token de Telegram inválido o revocado")
                    raise
                if exc.error_code == 409:
                    LOGGER.error("Hay otra instancia del bot usando long polling")
                    raise
                wait_seconds = exc.retry_after or min(
                    config.max_retry_seconds,
                    retry_delay + random.uniform(0, retry_delay / 4),
                )
                LOGGER.warning("Error temporal de Telegram; reintento en %.1f s", wait_seconds)
                stop_event.wait(wait_seconds)
                retry_delay = min(config.max_retry_seconds, retry_delay * 2)
            except (KeyError, TypeError, ValueError):
                LOGGER.exception("Actualización de Telegram inválida")
    finally:
        store.close()
        LOGGER.info("Bot detenido")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SINCATEGOREMATICO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run()
    except Exception:
        LOGGER.exception("El bot terminó por un error fatal")
        raise SystemExit(1) from None
