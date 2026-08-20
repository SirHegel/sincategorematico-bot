from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import threading
import time
from typing import BinaryIO

import fcntl

from .config import BotConfig, load_config
from .linkedin import (
    LinkedInClient,
    LinkedInError,
    TokenBundle,
    linkedin_credentials_usable,
    post_url,
    refresh_token,
)
from .runtime import (
    apply_defaults,
    editorial_profile,
    load_rules,
    publication_gate,
)
from .sources import FeedError, fetch_feed
from .storage import StateStore
from .writer import ClaudeWriter, WriterError

LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN = 3 * 24 * 3600
TOKEN_WARNING_MARGIN = 7 * 24 * 3600
MAX_COMPOSE_ATTEMPTS = 3
WRITER_COOLDOWN_SECONDS = 300
LOOP_SECONDS = 60


class EngineAlreadyRunning(RuntimeError):
    """Indica que otro motor conserva el cerrojo del mismo estado."""


def acquire_engine_lock(state_path: Path) -> BinaryIO:
    """Conserva un ``flock`` exclusivo durante toda la vida del motor.

    El cerrojo vive junto a la base de datos. Así dos clones que apunten al
    mismo estado tampoco pueden saltarse entre sí el límite diario o la
    separación mínima entre publicaciones.
    """

    resolved_state = state_path.expanduser().resolve(strict=False)
    resolved_state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = resolved_state.with_name(f".{resolved_state.name}.engine.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        raise EngineAlreadyRunning(
            f"Ya hay un motor usando el estado {resolved_state}"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n".encode("ascii"))
    os.fchmod(handle.fileno(), 0o600)
    return handle


def release_engine_lock(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def acquire_publication_lock(state_path: Path) -> BinaryIO | None:
    """Serializa la compuerta y el POST incluso si alguien evita ``run``.

    El cerrojo principal impide dos procesos del motor. Este segundo cinturón
    protege también invocaciones embebidas o manuales de esta versión: si
    otro envío está en curso, el ciclo actual simplemente vuelve a probar luego.
    """

    lock_path = state_path.with_name(f".{state_path.name}.publish.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    os.fchmod(handle.fileno(), 0o600)
    return handle


def store_tokens(store: StateStore, bundle: TokenBundle) -> None:
    store.set("linkedin_access_token", bundle.access_token)
    store.set("linkedin_expires_at", bundle.expires_at)
    # Una renovación OAuth puede omitir ``scope`` cuando conserva los permisos
    # originales. No borres entonces el alcance ya verificado.
    if bundle.scope.strip():
        store.set("linkedin_scope", bundle.scope)
    if bundle.refresh_token:
        store.set("linkedin_refresh_token", bundle.refresh_token)
    if bundle.refresh_expires_at:
        store.set("linkedin_refresh_expires_at", bundle.refresh_expires_at)


def linkedin_ready(store: StateStore) -> bool:
    return not store.get_bool("linkedin_auth_blocked") and linkedin_credentials_usable(
        access_token=store.get("linkedin_access_token") or "",
        author_urn=store.get("linkedin_author_urn") or "",
        expires_at=store.get_int("linkedin_expires_at") or 0,
        scope=store.get("linkedin_scope") or "",
    )


class Engine:
    def __init__(self, *, store: StateStore, config: BotConfig, writer: ClaudeWriter) -> None:
        self.store = store
        self.config = config
        self.writer = writer
        self._last_ingest = 0.0
        self._warned_token = False
        self._writer_ready_at = 0.0

    def heartbeat(self) -> None:
        self.store.set("engine_heartbeat_at", int(time.time()))

    # -- ingesta ----------------------------------------------------------

    def ingest_due(self) -> bool:
        interval = max(5, self.store.get_int("ingest_interval_minutes") or 30) * 60
        return time.monotonic() - self._last_ingest >= interval or self._last_ingest == 0.0

    def ingest(self) -> int:
        self._last_ingest = time.monotonic()
        discovered = 0
        for source in self.store.sources(only_enabled=True):
            self.heartbeat()
            source_id = int(source["id"])
            try:
                result = fetch_feed(
                    str(source["url"]),
                    etag=source["etag"] and str(source["etag"]),
                    modified=source["modified"] and str(source["modified"]),
                )
            except FeedError as exc:
                LOGGER.warning("Fuente %s sin datos: %s", source["name"], exc)
                self.store.mark_source_fetched(source_id, f"error: {exc}")
                continue

            if result.not_modified:
                self.store.mark_source_fetched(
                    source_id, "sin cambios", etag=result.etag, modified=result.modified
                )
                continue

            fresh = 0
            for entry in result.entries:
                created = self.store.add_item(
                    source_id=source_id,
                    guid=entry.guid,
                    url=entry.url,
                    title=entry.title,
                    summary=entry.summary,
                    published_at=entry.published_at,
                )
                fresh += 1 if created else 0
            discovered += fresh
            self.store.mark_source_fetched(
                source_id, f"{len(result.entries)} entradas · {fresh} nuevas",
                etag=result.etag, modified=result.modified,
            )

        max_age = max(1, self.store.get_int("item_max_age_hours") or 48) * 3600
        self.store.expire_items(max_age_seconds=max_age)
        self.store.prune_items()
        if discovered:
            LOGGER.info("Ingesta: %s noticias nuevas", discovered)
            self.store.add_activity("ingesta", f"{discovered} noticias nuevas en las fuentes")
        return discovered

    # -- redacción --------------------------------------------------------

    def queue_depth(self) -> int:
        counts = self.store.count_by_state()
        return counts.get("pending", 0) + counts.get("approved", 0)

    def pause_writer(self, reason: str) -> None:
        """Aparta la redacción un rato para no insistir contra una CLI caída."""
        self._writer_ready_at = time.monotonic() + WRITER_COOLDOWN_SECONDS
        LOGGER.warning("Redacción en espera %s s: %s", WRITER_COOLDOWN_SECONDS, reason)

    def compose_next(self) -> bool:
        if time.monotonic() < self._writer_ready_at:
            return False
        target = max(1, self.store.get_int("queue_target") or 2)
        if self.queue_depth() >= target:
            return False

        if not self.writer.available():
            self.pause_writer("la CLI de Claude no está disponible")
            return False

        max_age = max(1, self.store.get_int("item_max_age_hours") or 48) * 3600
        item = self.store.next_item(max_age_seconds=max_age)
        if item is None:
            return False

        item_id = int(item["id"])
        link = str(item["url"]) or None
        profile = editorial_profile(self.store, self.config)
        self.heartbeat()
        try:
            draft = self.writer.compose(
                profile=profile,
                title=str(item["title"]),
                summary=str(item["summary"]),
                link=link or "(sin enlace: encargo directo)",
                source=str(item["source_name"] or "encargo del propietario"),
            )
        except WriterError as exc:
            attempts = self.store.bump_item_attempts(item_id)
            self.pause_writer(str(exc))
            if attempts >= MAX_COMPOSE_ATTEMPTS:
                self.store.set_item_state(item_id, "skipped")
                self.store.add_activity(
                    "redaccion", f"Noticia abandonada tras {attempts} intentos: {exc}"
                )
            else:
                LOGGER.warning("No se pudo redactar «%s»: %s", item["title"], exc)
            return False

        self.store.set_item_state(item_id, "drafted")
        if draft.discarded:
            LOGGER.info("Noticia descartada por el redactor: %s", draft.reason)
            self.store.add_activity("redaccion", f"Noticia descartada: {draft.reason}")
            return True

        draft_id = self.store.add_draft(
            item_id=item_id,
            body=draft.body,
            link=link,
            title=str(item["title"]),
        )
        if not self.store.get_bool("approval_required", default=True):
            self.store.set_draft_state(draft_id, "approved")
        self.store.add_activity("redaccion", f"Borrador #{draft_id}: {str(item['title'])[:120]}")
        LOGGER.info("Borrador %s creado", draft_id)
        return True

    # -- publicación ------------------------------------------------------

    def client(self) -> LinkedInClient:
        expires_at = self.store.get_int("linkedin_expires_at") or 0
        stored_refresh = self.store.get("linkedin_refresh_token")
        client_id = os.environ.get("SINCATEGOREMATICO_LINKEDIN_CLIENT_ID", "")
        client_secret = os.environ.get("SINCATEGOREMATICO_LINKEDIN_CLIENT_SECRET", "")

        if stored_refresh and client_id and client_secret and expires_at - time.time() < TOKEN_REFRESH_MARGIN:
            try:
                store_tokens(self.store, refresh_token(
                    token=stored_refresh, client_id=client_id, client_secret=client_secret
                ))
                LOGGER.info("Token de LinkedIn renovado")
                self.store.add_activity("linkedin", "Token de acceso renovado automáticamente")
            except LinkedInError as exc:
                LOGGER.warning("No se pudo renovar el token de LinkedIn: %s", exc)

        token = self.store.get("linkedin_access_token") or ""
        return LinkedInClient(token)

    def warn_token_expiry(self) -> None:
        if self.store.get_bool("linkedin_auth_blocked"):
            return
        expires_at = self.store.get_int("linkedin_expires_at")
        if not expires_at:
            return
        remaining = expires_at - int(time.time())
        if remaining < TOKEN_WARNING_MARGIN and not self._warned_token:
            self._warned_token = True
            days = max(0, remaining // 86400)
            self.store.set("linkedin_token_warning", f"El acceso a LinkedIn caduca en {days} días")
            self.store.add_activity(
                "linkedin", f"El acceso a LinkedIn caduca en {days} días; vuelve a autorizar"
            )
        elif remaining >= TOKEN_WARNING_MARGIN:
            self._warned_token = False
            self.store.delete("linkedin_token_warning")

    def publish_next(self) -> bool:
        publication_lock = acquire_publication_lock(self.store.path)
        if publication_lock is None:
            LOGGER.info("Otro motor está evaluando o enviando una publicación")
            return False
        try:
            return self._publish_next_locked()
        finally:
            release_engine_lock(publication_lock)

    def _publish_next_locked(self) -> bool:
        rules = load_rules(self.store, self.config)
        gate = publication_gate(self.store, rules)
        if not gate:
            return False

        dry_run = self.store.get_bool("dry_run", default=True)

        if dry_run:
            # Una simulación valida la cola pero no la consume ni altera los
            # límites/separación de publicaciones reales.
            draft = self.store.next_approved_for_simulation()
            if draft is None:
                return False
            draft_id = int(draft["id"])
            self.store.mark_draft_simulated(draft_id)
            self.store.add_activity(
                "publicacion", f"Simulación verificada para el borrador #{draft_id}; sigue aprobado"
            )
            LOGGER.info("Borrador %s verificado en modo simulación", draft_id)
            return True

        candidates = self.store.drafts_by_state("approved", limit=1)
        if not candidates:
            return False

        client: LinkedInClient | None = None
        # Un access token vencido no debe impedir renovar primero. La
        # validación estricta se repite después del intento y sigue bloqueando
        # cuando no existe refresh token o faltan las credenciales de la app.
        if not linkedin_ready(self.store) and self.store.get("linkedin_refresh_token"):
            try:
                client = self.client()
            except LinkedInError as exc:
                LOGGER.warning("No fue posible preparar el cliente de LinkedIn: %s", exc)

        if not linkedin_ready(self.store):
            aviso = "LinkedIn no está vinculado, el token caducó o faltan permisos"
            if self.store.get("linkedin_not_ready_warning") != aviso:
                self.store.set("linkedin_not_ready_warning", aviso)
                self.store.add_activity("publicacion", aviso + "; el borrador sigue aprobado")
            return False
        self.store.delete("linkedin_not_ready_warning")

        # Reserva durable ANTES del POST. Un crash posterior queda como
        # incierto y exige verificación humana, nunca un reintento duplicado.
        draft = self.store.claim_next_approved()
        if draft is None:
            return False
        draft_id = int(draft["id"])

        try:
            self.heartbeat()
            if client is None:
                client = self.client()
            urn = client.create_post(
                author_urn=str(self.store.get("linkedin_author_urn")),
                commentary=str(draft["body"]),
                link=str(draft["link"]) if draft["link"] else None,
                link_title=str(draft["title"]),
                visibility=self.store.get("linkedin_visibility") or "PUBLIC",
            )
        except LinkedInError as exc:
            LOGGER.error("Fallo al publicar el borrador %s: %s", draft_id, exc)
            if exc.is_authentication_problem:
                self.store.set_draft_state(draft_id, "failed", error=str(exc))
                self.store.set("linkedin_auth_blocked", True)
                self.store.set("linkedin_expires_at", 0)
                self.store.set(
                    "linkedin_token_warning",
                    "LinkedIn rechazó la autorización; vuelve a vincular y reintenta el borrador",
                )
                self.store.delete("linkedin_token_warning_sent")
                self.store.add_activity("publicacion", f"Borrador #{draft_id} requiere reautorización: {exc}")
            elif exc.status == 429 and not exc.ambiguous:
                attempts = int(draft.get("attempts") or 0) + 1
                delay = exc.retry_after or min(6 * 3600, 60 * (2 ** min(attempts, 8)))
                retry_at = int(time.time()) + delay
                self.store.schedule_draft_retry(
                    draft_id, retry_at=retry_at, error=str(exc)
                )
                self.store.set(
                    "linkedin_retry_after_until",
                    max(self.store.get_int("linkedin_retry_after_until") or 0, retry_at),
                )
                self.store.add_activity(
                    "publicacion", f"LinkedIn limitó el envío; borrador #{draft_id} reintenta en {delay}s"
                )
            elif exc.ambiguous or exc.status is None or (exc.status and exc.status >= 500):
                self.store.mark_draft_uncertain(
                    draft_id,
                    f"Resultado incierto: {exc}. Verifica LinkedIn antes de usar reintentar.",
                )
                self.store.add_activity(
                    "publicacion", f"Borrador #{draft_id} con resultado incierto; no se reintentará solo"
                )
            else:
                self.store.set_draft_state(draft_id, "failed", error=str(exc))
                self.store.add_activity("publicacion", f"Borrador #{draft_id} falló: {exc}")
            return False
        except Exception as exc:
            # Una excepción no clasificada puede ocurrir después de que
            # LinkedIn haya recibido el POST. Nunca se reintenta a ciegas.
            LOGGER.exception("Resultado desconocido al publicar el borrador %s", draft_id)
            self.store.mark_draft_uncertain(
                draft_id,
                f"Resultado incierto por un error no previsto: {exc}. "
                "Verifica LinkedIn antes de usar reintentar.",
            )
            self.store.add_activity(
                "publicacion",
                f"Borrador #{draft_id} con resultado incierto; requiere verificación",
            )
            return False

        if not isinstance(urn, str) or not urn.strip():
            self.store.mark_draft_uncertain(
                draft_id,
                "LinkedIn aceptó el envío pero no devolvió el URN; verifica antes de reintentar",
            )
            self.store.add_activity(
                "publicacion", f"Borrador #{draft_id} enviado sin identificador; requiere verificación"
            )
            return False
        urn = urn.strip()
        self.store.mark_draft_published(draft_id, urn)
        self.store.add_activity(
            "publicacion", f"Publicado en LinkedIn: {post_url(urn) or f'borrador #{draft_id}'}"
        )
        LOGGER.info("Borrador %s publicado como %s", draft_id, urn or "sin URN")
        return True

    # -- ciclo ------------------------------------------------------------

    def tick(self) -> None:
        self.heartbeat()
        if self.store.get_bool("publishing_paused", default=True):
            self.store.set("engine_status", "pausado")
            return
        self.store.set("engine_status", "activo")
        if self.ingest_due():
            self.ingest()
        self.compose_next()
        self.warn_token_expiry()
        self.publish_next()


def run() -> None:
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(
        os.environ.get("SINCATEGOREMATICO_CONFIG_PATH", project_root / "config.toml")
    )
    state_path = Path(
        os.environ.get(
            "SINCATEGOREMATICO_STATE_PATH",
            str(Path.home() / ".local/state/sincategorematico-bot/state.db"),
        )
    )
    engine_lock = acquire_engine_lock(state_path)
    try:
        _run_locked(config_path=config_path, state_path=state_path)
    finally:
        release_engine_lock(engine_lock)


def _run_locked(*, config_path: Path, state_path: Path) -> None:
    config = load_config(config_path)
    store = StateStore(state_path)
    apply_defaults(store, config)
    recovered = store.recover_inflight_publications()
    if recovered:
        store.add_activity(
            "publicacion",
            f"{recovered} envío(s) interrumpido(s) requieren verificación antes de reintentar",
        )
    store.set("engine_pid", os.getpid())
    store.set("engine_status", "iniciando")
    store.set("engine_heartbeat_at", int(time.time()))
    engine = Engine(store=store, config=config, writer=ClaudeWriter())
    if not engine.writer.available():
        LOGGER.warning("La CLI de Claude no está disponible: no se redactarán borradores")
        store.add_activity("motor", "La CLI de Claude no está disponible para redactar")

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        LOGGER.info("Señal %s recibida; deteniendo el motor", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    LOGGER.info("Motor editorial iniciado")
    store.add_activity("motor", "Motor editorial iniciado")
    try:
        while not stop_event.is_set():
            try:
                engine.tick()
            except Exception:
                LOGGER.exception("Error no previsto en el ciclo del motor")
            stop_event.wait(LOOP_SECONDS)
    finally:
        store.add_activity("motor", "Motor editorial detenido")
        store.set("engine_status", "detenido")
        store.set("engine_pid", 0)
        store.close()
        LOGGER.info("Motor detenido")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SINCATEGOREMATICO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run()
    except Exception:
        LOGGER.exception("El motor terminó por un error fatal")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
