from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import BotConfig
from .linkedin import linkedin_credentials_usable, post_url
from .storage import StateStore
from .writer import DEFAULT_MODEL, MAX_POST_CHARACTERS, EditorialProfile

DEFAULT_SETTINGS: dict[str, str | int | bool] = {
    "approval_required": True,
    "dry_run": True,
    "publish_window_start": "07:30",
    "publish_window_end": "20:30",
    "min_gap_minutes": 120,
    "ingest_interval_minutes": 30,
    "item_max_age_hours": 48,
    "queue_target": 2,
    "ai_model": DEFAULT_MODEL,
    "editorial_topics": "inteligencia artificial aplicada, automatización de procesos y tecnología para empresas",
    "editorial_audience": "profesionales y directivos de habla hispana que deciden sobre tecnología",
    "editorial_tone": "analítico y directo, sin promesas comerciales ni humo",
    "linkedin_visibility": "PUBLIC",
}

DEFAULT_SOURCES: tuple[tuple[str, str], ...] = (
    ("https://feeds.arstechnica.com/arstechnica/technology-lab", "Ars Technica"),
    ("https://feeds.weblogssl.com/xataka2", "Xataka"),
    ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch IA"),
    ("https://www.technologyreview.com/feed/", "MIT Technology Review"),
)

TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# Cada operación costosa renueva el pulso; 6 minutos cubren holgadamente el
# timeout máximo del redactor (240 s), un feed con redirecciones y el intervalo
# normal entre ciclos, sin declarar muerto un motor que sigue trabajando.
ENGINE_HEARTBEAT_TTL = 360


class Gate:
    """Resultado de comprobar si el motor puede publicar ahora."""

    def __init__(self, allowed: bool, reason: str, retry_after: int = 60) -> None:
        self.allowed = allowed
        self.reason = reason
        self.retry_after = retry_after

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class PublishingRules:
    timezone: str
    max_posts_per_day: int
    window_start: clock_time
    window_end: clock_time
    min_gap_minutes: int


def apply_defaults(store: StateStore, config: BotConfig) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        store.set_default(key, value)
    store.set_default("publishing_paused", config.paused_by_default)
    store.set_default("max_posts_per_day", config.max_posts_per_day)
    store.set_default("timezone", config.timezone)
    if not store.sources():
        for url, name in DEFAULT_SOURCES:
            store.add_source(url, name)
    else:
        # La URL por etiqueta usada en la primera versión redirige a un 404.
        for source in store.sources():
            if str(source["url"]) == "https://www.xataka.com/tag/inteligencia-artificial/feed":
                store.update_source(
                    int(source["id"]), url="https://feeds.weblogssl.com/xataka2", name="Xataka"
                )


def parse_clock(raw: str | None, fallback: str) -> clock_time:
    match = TIME_PATTERN.match((raw or "").strip())
    if not match:
        match = TIME_PATTERN.match(fallback)
    assert match is not None
    return clock_time(hour=int(match.group(1)), minute=int(match.group(2)))


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/Bogota")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/Bogota")


def load_rules(store: StateStore, config: BotConfig) -> PublishingRules:
    return PublishingRules(
        timezone=store.get("timezone") or config.timezone,
        max_posts_per_day=max(1, store.get_int("max_posts_per_day") or config.max_posts_per_day),
        window_start=parse_clock(store.get("publish_window_start"), "07:30"),
        window_end=parse_clock(store.get("publish_window_end"), "20:30"),
        min_gap_minutes=max(0, store.get_int("min_gap_minutes") or 0),
    )


def editorial_profile(store: StateStore, config: BotConfig) -> EditorialProfile:
    return EditorialProfile(
        display_name=config.display_name,
        topics=store.get("editorial_topics") or str(DEFAULT_SETTINGS["editorial_topics"]),
        audience=store.get("editorial_audience") or str(DEFAULT_SETTINGS["editorial_audience"]),
        tone=store.get("editorial_tone") or str(DEFAULT_SETTINGS["editorial_tone"]),
        model=store.get("ai_model") or DEFAULT_MODEL,
        max_characters=MAX_POST_CHARACTERS,
    )


def local_now(rules: PublishingRules, *, moment: float | None = None) -> datetime:
    return datetime.fromtimestamp(moment if moment is not None else time.time(), tz=zone(rules.timezone))


def start_of_local_day(rules: PublishingRules, *, moment: float | None = None) -> int:
    now = local_now(rules, moment=moment)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def in_window(rules: PublishingRules, now: datetime) -> bool:
    current = now.time()
    if rules.window_start <= rules.window_end:
        return rules.window_start <= current <= rules.window_end
    return current >= rules.window_start or current <= rules.window_end


def seconds_until_window(rules: PublishingRules, now: datetime) -> int:
    candidate = now.replace(
        hour=rules.window_start.hour, minute=rules.window_start.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return max(60, int((candidate - now).total_seconds()))


def publication_gate(
    store: StateStore, rules: PublishingRules, *, moment: float | None = None
) -> Gate:
    if store.get_bool("publishing_paused", default=True):
        return Gate(False, "el flujo está en pausa", 120)

    if not store.get_bool("dry_run", default=True):
        if store.get_bool("linkedin_auth_blocked"):
            return Gate(False, "LinkedIn requiere volver a autorizar la cuenta", 300)
        states = store.count_by_state()
        uncertain = states.get("uncertain", 0)
        inflight = states.get("publishing", 0)
        if uncertain or inflight:
            total = uncertain + inflight
            return Gate(
                False,
                f"hay {total} envío(s) sin conciliar; verifica LinkedIn antes de continuar",
                300,
            )
        now_epoch = int(time.time() if moment is None else moment)
        cooldown_until = store.get_int("linkedin_retry_after_until") or 0
        if cooldown_until > now_epoch:
            pending = cooldown_until - now_epoch
            return Gate(
                False,
                f"LinkedIn pidió esperar {pending} s antes de otro envío",
                min(pending, 1800),
            )

    now = local_now(rules, moment=moment)
    if not in_window(rules, now):
        wait = seconds_until_window(rules, now)
        return Gate(
            False,
            f"fuera de la franja {rules.window_start:%H:%M}–{rules.window_end:%H:%M}",
            min(wait, 1800),
        )

    published_today = store.count_published_since(start_of_local_day(rules, moment=moment))
    if published_today >= rules.max_posts_per_day:
        return Gate(False, f"se alcanzó el límite diario de {rules.max_posts_per_day}", 1800)

    last = store.last_published_at()
    if last is not None and rules.min_gap_minutes:
        elapsed = max(0, int((moment if moment is not None else time.time()) - last))
        pending = rules.min_gap_minutes * 60 - elapsed
        if pending > 0:
            return Gate(
                False,
                f"faltan {pending // 60} min para respetar la separación mínima",
                min(pending, 1800),
            )

    return Gate(True, "listo para publicar")


def linkedin_summary(store: StateStore) -> dict[str, object]:
    author = store.get("linkedin_author_urn") or ""
    expires_at = store.get_int("linkedin_expires_at") or 0
    return {
        "linked": not store.get_bool("linkedin_auth_blocked") and linkedin_credentials_usable(
            access_token=store.get("linkedin_access_token") or "",
            author_urn=author,
            expires_at=expires_at,
            scope=store.get("linkedin_scope") or "",
        ),
        "author": author,
        "kind": "página de empresa" if ":organization:" in author else "perfil personal",
        "days_left": max(0, (expires_at - int(time.time())) // 86400) if expires_at else 0,
        "visibility": store.get("linkedin_visibility") or "PUBLIC",
        "warning": store.get("linkedin_token_warning") or "",
    }


def draft_summary(draft: dict[str, object]) -> dict[str, object]:
    body = str(draft["body"])
    return {
        "id": int(draft["id"]),
        "title": str(draft["title"]),
        "state": str(draft["state"]),
        "origin": str(draft["origin"]),
        "created_at": int(draft["created_at"]),
        "link": str(draft["link"]) if draft["link"] else "",
        "preview": body if len(body) <= 900 else body[:900].rstrip() + "…",
        "url": post_url(str(draft["post_urn"])) if draft["post_urn"] else "",
        "error": str(draft["last_error"]) if draft["last_error"] else "",
        "retry_at": int(draft["retry_at"]) if draft.get("retry_at") else 0,
        "simulated_at": int(draft["simulated_at"]) if draft.get("simulated_at") else 0,
    }


def snapshot(store: StateStore, config: BotConfig) -> dict[str, object]:
    """Retrato del estado compartido por el panel web y la aplicación de escritorio."""
    rules = load_rules(store, config)
    gate = publication_gate(store, rules)
    drafts = store.count_by_state()
    now = int(time.time())
    heartbeat = store.get_int("engine_heartbeat_at") or 0
    engine_status = store.get("engine_status") or "detenido"
    engine_alive = bool(
        engine_status in {"iniciando", "activo", "pausado"}
        and heartbeat
        and 0 <= now - heartbeat <= ENGINE_HEARTBEAT_TTL
    )
    return {
        "authenticated": True,
        "paused": store.get_bool("publishing_paused", default=True),
        "owner": store.get_int("owner_user_id") is not None,
        "initialized": store.get_bool("telegram_initialized"),
        "max_posts": rules.max_posts_per_day,
        "timezone": rules.timezone,
        "window": f"{rules.window_start:%H:%M}–{rules.window_end:%H:%M}",
        "min_gap_minutes": rules.min_gap_minutes,
        "approval_required": store.get_bool("approval_required", default=True),
        "dry_run": store.get_bool("dry_run", default=True),
        "gate": gate.reason,
        "can_publish_now": bool(gate),
        "counts": {
            "pending": drafts.get("pending", 0),
            "approved": drafts.get("approved", 0),
            "published": drafts.get("published", 0),
            "failed": drafts.get("failed", 0),
            "uncertain": drafts.get("uncertain", 0),
            "publishing": drafts.get("publishing", 0),
            "rejected": drafts.get("rejected", 0),
            "news": store.count_items_by_state().get("new", 0),
        },
        "linkedin": linkedin_summary(store),
        "engine": {
            "alive": engine_alive,
            "heartbeat_at": heartbeat,
            "pid": store.get_int("engine_pid") or 0,
            "status": engine_status,
        },
        "sources": [
            {
                "id": int(source["id"]),
                "name": str(source["name"]),
                "enabled": bool(source["enabled"]),
                "status": str(source["last_status"] or "sin consultar"),
            }
            for source in store.sources()
        ],
        "drafts": [draft_summary(draft) for draft in store.recent_drafts(12)],
        "activity": store.recent_activity(30),
    }
