from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class BotConfig:
    display_name: str
    timezone: str
    poll_timeout_seconds: int
    max_retry_seconds: int
    paused_by_default: bool
    max_posts_per_day: int


def load_config(path: Path) -> BotConfig:
    with path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    bot = raw.get("bot", {})
    publishing = raw.get("publishing", {})
    poll_timeout = int(bot.get("poll_timeout_seconds", 25))
    max_retry = int(bot.get("max_retry_seconds", 60))
    max_posts = int(publishing.get("max_posts_per_day", 4))

    if not 5 <= poll_timeout <= 50:
        raise ValueError("poll_timeout_seconds debe estar entre 5 y 50")
    if not 5 <= max_retry <= 300:
        raise ValueError("max_retry_seconds debe estar entre 5 y 300")
    if not 1 <= max_posts <= 50:
        raise ValueError("max_posts_per_day debe estar entre 1 y 50")

    return BotConfig(
        display_name=str(bot.get("display_name", "Sincategoremático")),
        timezone=str(bot.get("timezone", "America/Bogota")),
        poll_timeout_seconds=poll_timeout,
        max_retry_seconds=max_retry,
        paused_by_default=bool(publishing.get("paused_by_default", True)),
        max_posts_per_day=max_posts,
    )
