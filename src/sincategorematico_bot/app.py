from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import time
from typing import Any

from .config import BotConfig
from .storage import StateStore
from .telegram_api import TelegramAPI


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    user_id: int
    chat_id: int


def parse_command(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, argument = stripped.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    if not command:
        return None
    return command, argument.strip()


class BotApplication:
    def __init__(
        self,
        *,
        api: TelegramAPI,
        store: StateStore,
        config: BotConfig,
        claim_sha256: str | None,
        claim_expires_at: int | None,
    ) -> None:
        self.api = api
        self.store = store
        self.config = config
        self.claim_sha256 = claim_sha256
        self.claim_expires_at = claim_expires_at
        self.started_at = time.monotonic()
        self._rate_windows: dict[int, deque[float]] = defaultdict(deque)

        if self.store.get("publishing_paused") is None:
            self.store.set("publishing_paused", config.paused_by_default)

    def owner(self) -> Identity | None:
        user_id = self.store.get_int("owner_user_id")
        chat_id = self.store.get_int("owner_chat_id")
        if user_id is None or chat_id is None:
            return None
        return Identity(user_id=user_id, chat_id=chat_id)

    def is_owner(self, identity: Identity) -> bool:
        return identity == self.owner()

    def is_rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        if user_id not in self._rate_windows and len(self._rate_windows) >= 1024:
            return True
        window = self._rate_windows[user_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= 12:
            return True
        window.append(now)
        return False

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        if chat.get("type") != "private" or not isinstance(text, str):
            return

        try:
            identity = Identity(user_id=int(sender["id"]), chat_id=int(chat["id"]))
        except (KeyError, TypeError, ValueError):
            return

        parsed = parse_command(text)
        if parsed is None:
            return
        command, argument = parsed

        if self.is_rate_limited(identity.user_id):
            self.api.send_message(identity.chat_id, "Demasiados comandos. Intenta de nuevo en un minuto.")
            return

        if command == "claim":
            self._handle_claim(identity, argument)
            return

        owner = self.owner()
        if owner is None:
            self.api.send_message(
                identity.chat_id,
                "El bot está activo, pero aún no tiene propietario. Usa el código local de vinculación con /claim.",
            )
            return
        if not self.is_owner(identity):
            self.api.send_message(identity.chat_id, "Acceso no autorizado.")
            return

        handlers = {
            "start": self._handle_start,
            "help": self._handle_help,
            "status": self._handle_status,
            "pause": self._handle_pause,
            "resume": self._handle_resume,
        }
        handler = handlers.get(command)
        if handler is None:
            self.api.send_message(identity.chat_id, "Comando desconocido. Usa /help.")
            return
        LOGGER.info("Comando autorizado: %s", command)
        handler(identity)

    def _handle_claim(self, identity: Identity, candidate: str) -> None:
        if self.owner() is not None:
            self.api.send_message(identity.chat_id, "El propietario ya fue vinculado.")
            return
        if not self.claim_sha256 or not self.claim_expires_at:
            self.api.send_message(identity.chat_id, "La vinculación no está habilitada.")
            return
        if int(datetime.now(timezone.utc).timestamp()) > self.claim_expires_at:
            self.api.send_message(identity.chat_id, "El código de vinculación expiró.")
            return

        candidate_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(candidate_hash, self.claim_sha256):
            self.api.send_message(identity.chat_id, "Código de vinculación incorrecto.")
            return

        self.store.set("owner_user_id", identity.user_id)
        self.store.set("owner_chat_id", identity.chat_id)
        self.store.set("claim_consumed_at", int(time.time()))
        self.store.add_activity("security", "Propietario de Telegram vinculado")
        LOGGER.info("Propietario vinculado correctamente")
        self.api.send_message(
            identity.chat_id,
            "Vinculación completada. Eres el propietario del bot. Usa /status para comprobarlo.",
        )

    def _handle_start(self, identity: Identity) -> None:
        self.api.send_message(
            identity.chat_id,
            f"{self.config.display_name} está conectado y funcionando. Usa /status para ver el estado.",
        )

    def _handle_help(self, identity: Identity) -> None:
        self.api.send_message(
            identity.chat_id,
            "Comandos disponibles:\n"
            "/status — estado del servicio\n"
            "/pause — pausar publicaciones\n"
            "/resume — reanudar publicaciones\n"
            "/help — mostrar esta ayuda",
        )

    def _handle_status(self, identity: Identity) -> None:
        paused = self.store.get_bool("publishing_paused", default=True)
        daily_limit = self.store.get_int("max_posts_per_day") or self.config.max_posts_per_day
        uptime_seconds = int(time.monotonic() - self.started_at)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.api.send_message(
            identity.chat_id,
            "Estado: en línea\n"
            f"Publicaciones: {'pausadas' if paused else 'habilitadas'}\n"
            f"Tiempo activo: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
            f"Límite diario configurado: {daily_limit}",
        )

    def _handle_pause(self, identity: Identity) -> None:
        self.store.set("publishing_paused", True)
        self.store.add_activity("control", "Publicaciones pausadas desde Telegram")
        self.api.send_message(identity.chat_id, "Publicaciones pausadas.")

    def _handle_resume(self, identity: Identity) -> None:
        self.store.set("publishing_paused", False)
        self.store.add_activity("control", "Publicaciones reanudadas desde Telegram")
        self.api.send_message(identity.chat_id, "Publicaciones habilitadas.")
