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
from .linkedin import normalize_post_reference, post_url
from .runtime import load_rules, publication_gate, snapshot
from .sources import is_safe_feed_url
from .storage import StateStore
from .telegram_api import TelegramAPI, TelegramAPIError

LOGGER = logging.getLogger(__name__)

DRAFT_BUTTONS: list[list[tuple[str, str]]] = [
    [("✅ Publicar", "aprobar"), ("🔁 Rehacer", "rehacer")],
    [("🗑 Descartar", "rechazar")],
]


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


def draft_preview(draft: dict[str, Any], *, full: bool = False) -> str:
    body = str(draft["body"])
    if not full and len(body) > 700:
        body = body[:700].rstrip() + "…"
    link = str(draft["link"]) if draft["link"] else ""
    header = f"Borrador #{draft['id']} · {str(draft['title'])[:90]}"
    parts = [header, "", body]
    if link:
        parts += ["", f"Enlace: {link}"]
    return "\n".join(parts)


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

    # -- identidad --------------------------------------------------------

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
        if len(window) >= 20:
            return True
        window.append(now)
        return False

    # -- entrada ----------------------------------------------------------

    def handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            self._handle_callback(update["callback_query"])
            return

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
            "cola": self._handle_cola,
            "ver": self._handle_ver,
            "tema": self._handle_tema,
            "fuentes": self._handle_fuentes,
            "agregar": self._handle_agregar,
            "quitar": self._handle_quitar,
            "limite": self._handle_limite,
            "franja": self._handle_franja,
            "aprobacion": self._handle_aprobacion,
            "publicacion": self._handle_publicacion,
            "linkedin": self._handle_linkedin,
            "reintentar": self._handle_reintentar,
            "confirmar": self._handle_confirmar,
        }
        handler = handlers.get(command)
        if handler is None:
            self.api.send_message(identity.chat_id, "Comando desconocido. Usa /help.")
            return
        LOGGER.info("Comando autorizado: %s", command)
        handler(identity, argument)

    def _handle_callback(self, query: dict[str, Any]) -> None:
        callback_id = str(query.get("id", ""))
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        try:
            identity = Identity(user_id=int(sender["id"]), chat_id=int(chat["id"]))
            message_id = int(message["message_id"])
        except (KeyError, TypeError, ValueError):
            return

        if not self.is_owner(identity):
            self.api.answer_callback_query(callback_id, "Acceso no autorizado.")
            return

        action, _, raw_id = str(query.get("data", "")).partition(":")
        try:
            draft_id = int(raw_id)
        except ValueError:
            self.api.answer_callback_query(callback_id, "Acción inválida.")
            return

        draft = self.store.draft(draft_id)
        if draft is None:
            self.api.answer_callback_query(callback_id, "Ese borrador ya no existe.")
            return
        if str(draft["state"]) != "pending":
            self.api.answer_callback_query(callback_id, f"El borrador ya está en «{draft['state']}».")
            return

        if action == "aprobar":
            self.store.set_draft_state(draft_id, "approved")
            self.store.add_activity("control", f"Borrador #{draft_id} aprobado desde Telegram")
            note, answer = "✅ Aprobado. Se publicará en la próxima franja disponible.", "Aprobado"
        elif action == "rechazar":
            self.store.set_draft_state(draft_id, "rejected")
            self.store.add_activity("control", f"Borrador #{draft_id} descartado desde Telegram")
            note, answer = "🗑 Descartado.", "Descartado"
        elif action == "rehacer":
            self.store.set_draft_state(draft_id, "discarded")
            if draft["item_id"] is not None:
                self.store.set_item_state(int(draft["item_id"]), "new")
            self.store.add_activity("control", f"Borrador #{draft_id} enviado a reescritura")
            note, answer = "🔁 Se volverá a redactar en el próximo ciclo.", "Rehaciendo"
        else:
            self.api.answer_callback_query(callback_id, "Acción desconocida.")
            return

        self.api.answer_callback_query(callback_id, answer)
        try:
            self.api.edit_message_text(
                identity.chat_id, message_id, f"{draft_preview(draft)}\n\n{note}"
            )
        except TelegramAPIError as exc:
            LOGGER.warning("No se pudo actualizar el mensaje del borrador: %s", exc)

    # -- avisos -----------------------------------------------------------

    def notify_pending_drafts(self) -> None:
        owner = self.owner()
        if owner is None:
            return
        for draft in self.store.unnotified_drafts():
            draft_id = int(draft["id"])
            buttons = [[(label, f"{action}:{draft_id}") for label, action in row] for row in DRAFT_BUTTONS]
            try:
                sent = self.api.send_message(
                    owner.chat_id,
                    f"Nuevo borrador listo para revisar.\n\n{draft_preview(draft)}",
                    buttons=buttons,
                )
            except TelegramAPIError as exc:
                LOGGER.warning("No se pudo avisar del borrador %s: %s", draft_id, exc)
                return
            self.store.mark_draft_notified(draft_id, int(sent.get("message_id", 0)))

    def notify_alerts(self) -> None:
        owner = self.owner()
        if owner is None:
            return
        warning = self.store.get("linkedin_token_warning")
        if warning and self.store.get("linkedin_token_warning_sent") != warning:
            try:
                self.api.send_message(owner.chat_id, f"⚠️ {warning}. Ejecuta configure_linkedin.py para renovarlo.")
            except TelegramAPIError:
                return
            self.store.set("linkedin_token_warning_sent", warning)

    # -- comandos ---------------------------------------------------------

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

    def _handle_start(self, identity: Identity, _argument: str = "") -> None:
        self.api.send_message(
            identity.chat_id,
            f"{self.config.display_name} está conectado.\n"
            "Recibirás cada borrador aquí para aprobarlo antes de que salga a LinkedIn.\n"
            "Usa /status para el estado y /help para la lista de comandos.",
        )

    def _handle_help(self, identity: Identity, _argument: str = "") -> None:
        self.api.send_message(
            identity.chat_id,
            "Operación\n"
            "/status — estado del servicio\n"
            "/cola — borradores en espera\n"
            "/ver <n> — ver un borrador completo\n"
            "/tema <texto> — encargar una publicación\n"
            "/pause · /resume — detener o reanudar todo el motor\n"
            "/reintentar <n> — reponer un envío fallido o incierto tras verificar LinkedIn\n"
            "/confirmar <n> <URN/URL> — conciliar como ya publicado sin repetir el envío\n\n"
            "Fuentes\n"
            "/fuentes — listado con su estado\n"
            "/agregar <url> [nombre] — añadir un RSS\n"
            "/quitar <n> — eliminar una fuente\n\n"
            "Ajustes\n"
            "/limite <n> — piezas por día\n"
            "/franja <hh:mm> <hh:mm> — horario permitido\n"
            "/aprobacion — alternar aprobación manual\n"
            "/publicacion real|simulacion — destino real o de prueba\n"
            "/linkedin — estado de la cuenta vinculada",
        )

    def _handle_status(self, identity: Identity, _argument: str = "") -> None:
        rules = load_rules(self.store, self.config)
        gate = publication_gate(self.store, rules)
        counts = self.store.count_by_state()
        engine = snapshot(self.store, self.config)["engine"]
        heartbeat = int(engine["heartbeat_at"])
        if engine["alive"]:
            pulse = f"{engine['status']} · pulso reciente"
        elif heartbeat:
            age = int(time.time()) - heartbeat
            pulse = (
                f"SIN PULSO · último hace {age // 60} min"
                if age >= 0
                else "SIN PULSO · reloj del estado inconsistente"
            )
        else:
            pulse = "SIN PULSO · el motor no ha iniciado"
        uptime_seconds = int(time.monotonic() - self.started_at)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        approval = "manual" if self.store.get_bool("approval_required", default=True) else "automática"
        destino = "simulación" if self.store.get_bool("dry_run", default=True) else "LinkedIn real"

        self.api.send_message(
            identity.chat_id,
            "Estado: en línea\n"
            f"Motor editorial: {'pausado' if self.store.get_bool('publishing_paused', default=True) else 'habilitado'}\n"
            f"Proceso del motor: {pulse}\n"
            f"Ahora mismo: {gate.reason}\n"
            f"Destino: {destino} · Aprobación: {approval}\n"
            f"Franja: {rules.window_start:%H:%M}–{rules.window_end:%H:%M} ({rules.timezone})\n"
            f"Límite diario: {rules.max_posts_per_day} · separación {rules.min_gap_minutes} min\n"
            f"Cola: {counts.get('pending', 0)} por revisar · {counts.get('approved', 0)} aprobados\n"
            f"Publicados: {counts.get('published', 0)} · fallidos: {counts.get('failed', 0)}"
            f" · inciertos: {counts.get('uncertain', 0)}\n"
            f"Fuentes activas: {len(self.store.sources(only_enabled=True))}\n"
            f"Tiempo activo: {hours:02d}:{minutes:02d}",
        )

    def _handle_pause(self, identity: Identity, _argument: str = "") -> None:
        self.store.set("publishing_paused", True)
        self.store.add_activity("control", "Motor editorial pausado desde Telegram")
        self.api.send_message(identity.chat_id, "Motor editorial pausado: no ingiere, redacta ni publica.")

    def _handle_resume(self, identity: Identity, _argument: str = "") -> None:
        self.store.set("publishing_paused", False)
        self.store.add_activity("control", "Motor editorial reanudado desde Telegram")
        self.api.send_message(identity.chat_id, "Motor editorial habilitado.")

    def _handle_reintentar(self, identity: Identity, argument: str) -> None:
        try:
            draft_id = int(argument.strip())
        except ValueError:
            self.api.send_message(identity.chat_id, "Indica el número: /reintentar 12")
            return
        draft = self.store.draft(draft_id)
        if draft is None or str(draft["state"]) not in {"failed", "uncertain"}:
            self.api.send_message(
                identity.chat_id, "Solo se pueden reintentar borradores fallidos o inciertos."
            )
            return
        if not self.store.retry_draft(draft_id):
            self.api.send_message(identity.chat_id, "No fue posible reponer ese borrador.")
            return
        self.store.add_activity(
            "control", f"Borrador #{draft_id} repuesto manualmente tras verificación"
        )
        self.api.send_message(
            identity.chat_id,
            f"Borrador #{draft_id} repuesto. Se intentará una vez cuando la compuerta lo permita.",
        )

    def _handle_confirmar(self, identity: Identity, argument: str) -> None:
        raw_id, separator, reference = argument.strip().partition(" ")
        try:
            draft_id = int(raw_id)
        except ValueError:
            draft_id = 0
        urn = normalize_post_reference(reference) if separator else None
        if draft_id <= 0 or urn is None:
            self.api.send_message(
                identity.chat_id,
                "Uso: /confirmar 12 <URN o URL de la publicación de LinkedIn>",
            )
            return
        if not self.store.reconcile_draft_as_published(draft_id, urn):
            self.api.send_message(
                identity.chat_id,
                "Solo un borrador con resultado incierto se puede confirmar de este modo.",
            )
            return
        self.store.add_activity(
            "control", f"Borrador #{draft_id} conciliado como publicado desde Telegram"
        )
        self.api.send_message(
            identity.chat_id,
            f"Borrador #{draft_id} confirmado sin repetir el POST. Los límites ya cuentan esa publicación.",
        )

    def _handle_cola(self, identity: Identity, _argument: str = "") -> None:
        pending = self.store.drafts_by_state("pending", limit=10)
        approved = self.store.drafts_by_state("approved", limit=10)
        if not pending and not approved:
            self.api.send_message(identity.chat_id, "La cola está vacía.")
            return
        lines = []
        if pending:
            lines.append("Por revisar:")
            lines += [f"  #{d['id']} · {str(d['title'])[:70]}" for d in pending]
        if approved:
            lines.append("Aprobados, esperando franja:")
            lines += [f"  #{d['id']} · {str(d['title'])[:70]}" for d in approved]
        lines.append("\nUsa /ver <n> para leer uno completo.")
        self.api.send_message(identity.chat_id, "\n".join(lines))

    def _handle_ver(self, identity: Identity, argument: str) -> None:
        try:
            draft_id = int(argument.strip())
        except ValueError:
            self.api.send_message(identity.chat_id, "Indica el número: /ver 12")
            return
        draft = self.store.draft(draft_id)
        if draft is None:
            self.api.send_message(identity.chat_id, "No existe ese borrador.")
            return
        text = draft_preview(draft, full=True)
        if str(draft["state"]) == "published" and draft["post_urn"]:
            text += f"\n\nPublicado: {post_url(str(draft['post_urn']))}"
        elif str(draft["state"]) != "pending":
            text += f"\n\nEstado: {draft['state']}"
        buttons = (
            [[(label, f"{action}:{draft_id}") for label, action in row] for row in DRAFT_BUTTONS]
            if str(draft["state"]) == "pending"
            else None
        )
        self.api.send_message(identity.chat_id, text, buttons=buttons)

    def _handle_tema(self, identity: Identity, argument: str) -> None:
        topic = argument.strip()
        if len(topic) < 10:
            self.api.send_message(identity.chat_id, "Describe el tema con algo más de detalle: /tema <texto>")
            return
        guid = hashlib.sha256(f"manual:{topic}:{time.time()}".encode()).hexdigest()
        item_id = self.store.add_item(
            source_id=None,
            guid=guid,
            url="",
            title=topic[:300],
            summary="Encargo directo del propietario; no proviene de un feed.",
            published_at=int(time.time()),
        )
        if item_id is None:
            self.api.send_message(identity.chat_id, "Ese tema ya estaba encargado.")
            return
        self.store.add_activity("redaccion", f"Tema encargado: {topic[:120]}")
        self.api.send_message(
            identity.chat_id, "Encargado. El borrador llegará aquí en cuanto esté redactado."
        )

    def _handle_fuentes(self, identity: Identity, _argument: str = "") -> None:
        sources = self.store.sources()
        if not sources:
            self.api.send_message(identity.chat_id, "No hay fuentes configuradas. Usa /agregar <url>.")
            return
        lines = ["Fuentes de noticias:"]
        for source in sources:
            mark = "•" if source["enabled"] else "×"
            status = str(source["last_status"] or "sin consultar")
            lines.append(f"{mark} #{source['id']} {source['name']}\n    {status}")
        lines.append("\n/quitar <n> elimina una fuente.")
        self.api.send_message(identity.chat_id, "\n".join(lines))

    def _handle_agregar(self, identity: Identity, argument: str) -> None:
        parts = argument.split(maxsplit=1)
        if not parts:
            self.api.send_message(identity.chat_id, "Uso: /agregar <url> [nombre]")
            return
        url = parts[0].strip()
        if not is_safe_feed_url(url):
            self.api.send_message(identity.chat_id, "Esa dirección no es un feed público válido.")
            return
        name = parts[1].strip() if len(parts) > 1 else url.split("//", 1)[-1].split("/", 1)[0]
        source_id = self.store.add_source(url, name)
        if source_id is None:
            self.api.send_message(identity.chat_id, "Esa fuente ya estaba en la lista.")
            return
        self.store.add_activity("fuentes", f"Fuente añadida: {name}")
        self.api.send_message(identity.chat_id, f"Fuente #{source_id} añadida: {name}")

    def _handle_quitar(self, identity: Identity, argument: str) -> None:
        try:
            source_id = int(argument.strip())
        except ValueError:
            self.api.send_message(identity.chat_id, "Indica el número: /quitar 3")
            return
        if not self.store.remove_source(source_id):
            self.api.send_message(identity.chat_id, "No existe esa fuente.")
            return
        self.store.add_activity("fuentes", f"Fuente #{source_id} eliminada")
        self.api.send_message(identity.chat_id, "Fuente eliminada.")

    def _handle_limite(self, identity: Identity, argument: str) -> None:
        try:
            limit = int(argument.strip())
        except ValueError:
            self.api.send_message(identity.chat_id, "Indica un número: /limite 3")
            return
        if not 1 <= limit <= 50:
            self.api.send_message(identity.chat_id, "El límite debe estar entre 1 y 50.")
            return
        self.store.set("max_posts_per_day", limit)
        self.store.add_activity("settings", f"Límite diario fijado en {limit}")
        self.api.send_message(identity.chat_id, f"Límite diario: {limit} piezas.")

    def _handle_franja(self, identity: Identity, argument: str) -> None:
        parts = argument.split()
        from .runtime import TIME_PATTERN

        if len(parts) != 2 or not all(TIME_PATTERN.match(part) for part in parts):
            self.api.send_message(identity.chat_id, "Uso: /franja 07:30 20:30")
            return
        self.store.set("publish_window_start", parts[0])
        self.store.set("publish_window_end", parts[1])
        self.store.add_activity("settings", f"Franja horaria {parts[0]}–{parts[1]}")
        self.api.send_message(identity.chat_id, f"Franja de publicación: {parts[0]}–{parts[1]}.")

    def _handle_aprobacion(self, identity: Identity, _argument: str = "") -> None:
        manual = not self.store.get_bool("approval_required", default=True)
        self.store.set("approval_required", manual)
        self.store.add_activity("settings", f"Aprobación {'manual' if manual else 'automática'}")
        self.api.send_message(
            identity.chat_id,
            "Aprobación manual: cada borrador esperará tu visto bueno."
            if manual
            else "Aprobación automática: los borradores se publicarán sin preguntar.",
        )

    def _handle_publicacion(self, identity: Identity, argument: str) -> None:
        choice = argument.strip().lower()
        if choice not in {"real", "simulacion", "simulación"}:
            actual = "simulación" if self.store.get_bool("dry_run", default=True) else "real"
            self.api.send_message(
                identity.chat_id, f"Destino actual: {actual}.\nUso: /publicacion real | /publicacion simulacion"
            )
            return
        if choice == "real":
            from .engine import linkedin_ready

            if not linkedin_ready(self.store):
                self.api.send_message(
                    identity.chat_id,
                    "Todavía no hay cuenta de LinkedIn vinculada. Ejecuta configure_linkedin.py en el equipo.",
                )
                return
            self.store.set("dry_run", False)
            self.store.add_activity("settings", "Publicación real activada")
            self.api.send_message(identity.chat_id, "Las publicaciones aprobadas saldrán a LinkedIn.")
            return
        self.store.set("dry_run", True)
        self.store.add_activity("settings", "Publicación en simulación")
        self.api.send_message(identity.chat_id, "Modo simulación: nada se enviará a LinkedIn.")

    def _handle_linkedin(self, identity: Identity, _argument: str = "") -> None:
        from .engine import linkedin_ready

        if not linkedin_ready(self.store):
            self.api.send_message(
                identity.chat_id,
                "LinkedIn no está vinculado.\n"
                "En el equipo: python3 scripts/configure_linkedin.py",
            )
            return
        expires_at = self.store.get_int("linkedin_expires_at") or 0
        days = max(0, (expires_at - int(time.time())) // 86400)
        author = str(self.store.get("linkedin_author_urn"))
        kind = "página de empresa" if ":organization:" in author else "perfil personal"
        self.api.send_message(
            identity.chat_id,
            f"LinkedIn vinculado ({kind}).\n"
            f"Autor: {author}\n"
            f"El acceso caduca en {days} días.\n"
            f"Visibilidad: {self.store.get('linkedin_visibility') or 'PUBLIC'}",
        )
