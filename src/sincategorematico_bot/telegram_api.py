from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str, *, network_timeout: int = 35) -> None:
        if not token or ":" not in token:
            raise ValueError("Token de Telegram inválido")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._network_timeout = network_timeout

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> Any:
        encoded = urlencode(payload or {}).encode("utf-8")
        request = Request(
            f"{self._base_url}/{method}",
            data=encoded,
            headers={"User-Agent": "sincategorematico-bot/0.1"},
            method="POST",
        )
        try:
            with urlopen(
                request,
                timeout=timeout or self._network_timeout,
            ) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            description = f"HTTP {exc.code}"
            retry_after = None
            try:
                error = json.loads(body)
                description = str(error.get("description", description))
                retry_after = error.get("parameters", {}).get("retry_after")
            except (json.JSONDecodeError, AttributeError):
                pass
            raise TelegramAPIError(
                f"Telegram rechazó la solicitud: {description}",
                error_code=exc.code,
                retry_after=int(retry_after) if retry_after is not None else None,
            ) from None
        except URLError as exc:
            raise TelegramAPIError(
                f"No fue posible conectar con Telegram: {exc.reason}"
            ) from None
        except TimeoutError:
            raise TelegramAPIError(
                "La conexión con Telegram agotó el tiempo de espera"
            ) from None

        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            raise TelegramAPIError("Telegram devolvió una respuesta inválida") from None

        if not result.get("ok"):
            parameters = result.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            raise TelegramAPIError(
                f"Telegram rechazó la solicitud: {result.get('description', 'error desconocido')}",
                error_code=result.get("error_code"),
                retry_after=int(retry_after) if retry_after is not None else None,
            )
        return result.get("result")

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def delete_webhook(self, *, drop_pending_updates: bool) -> bool:
        return bool(
            self.call(
                "deleteWebhook",
                {"drop_pending_updates": str(drop_pending_updates).lower()},
            )
        )

    def set_commands(self) -> bool:
        commands = [
            {"command": "start", "description": "Abrir el panel del bot"},
            {"command": "status", "description": "Ver el estado del servicio"},
            {"command": "pause", "description": "Pausar publicaciones"},
            {"command": "resume", "description": "Reanudar publicaciones"},
            {"command": "help", "description": "Mostrar ayuda"},
        ]
        return bool(self.call("setMyCommands", {"commands": json.dumps(commands)}))

    def get_updates(self, *, offset: int, poll_timeout: int) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=poll_timeout + 10,
        )
        return list(result or [])

    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        return self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            },
        )
