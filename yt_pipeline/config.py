"""Configuration loaded from environment variables (or a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


class ConfigError(ValueError):
    """Raised when a required setting is missing or invalid."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def parse_channels(raw: str | None) -> list[str]:
    """Split 'UCxxx, @handle , UCyyy' into a clean, de-duplicated list."""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if part:
            seen.setdefault(part, None)
    return list(seen)


@dataclass(frozen=True)
class Settings:
    youtube_api_key: str
    database_url: str
    channels: list[str] = field(default_factory=list)
    lookback_days: int = 28              # how far back stats are refreshed each day
    fetch_playlists: bool = True         # map videos to playlists (costs extra quota)
    max_playlists_per_channel: int = 50
    max_pages_per_playlist: int = 10     # 50 items per page
    max_upload_pages: int = 100          # safety cap when walking the uploads playlist
    raw_retention_days: int = 90         # raw JSON kept this long (0 = forever)
    log_level: str = "INFO"

    def __repr__(self) -> str:  # never print secrets, even by accident
        return (f"Settings(channels={self.channels}, lookback_days={self.lookback_days}, "
                f"youtube_api_key=***, database_url=***)")

    @classmethod
    def from_env(cls, require_api: bool = True) -> "Settings":
        api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        db_url = os.getenv("DATABASE_URL", "").strip()
        channels = parse_channels(os.getenv("YOUTUBE_CHANNELS"))

        if not db_url:
            raise ConfigError("DATABASE_URL is not set (see .env.example)")
        if require_api and not api_key:
            raise ConfigError("YOUTUBE_API_KEY is not set (see .env.example)")
        if require_api and not channels:
            raise ConfigError("YOUTUBE_CHANNELS is empty - add at least one channel ID or @handle")

        return cls(
            youtube_api_key=api_key,
            database_url=db_url,
            channels=channels,
            lookback_days=_int("LOOKBACK_DAYS", 28),
            fetch_playlists=_bool(os.getenv("FETCH_PLAYLISTS"), True),
            max_playlists_per_channel=_int("MAX_PLAYLISTS_PER_CHANNEL", 50),
            max_pages_per_playlist=_int("MAX_PAGES_PER_PLAYLIST", 10),
            max_upload_pages=_int("MAX_UPLOAD_PAGES", 100),
            raw_retention_days=_int("RAW_RETENTION_DAYS", 90, minimum=0),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
