import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    discord_token: str
    channel_id: int
    guild_id: int | None
    database_path: str
    wzstats_url: str
    check_interval_minutes: int
    log_level: str


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    return int(value)


CHANNEL_ID = 1523753006422818876


settings = Settings(
    discord_token=_required_env("DISCORD_TOKEN"),
    channel_id=CHANNEL_ID,
    guild_id=_optional_int_env("GUILD_ID") or 1224678261154386001,
    database_path=os.getenv("DATABASE_PATH", "data/meta.sqlite3"),
    wzstats_url=os.getenv("WZSTATS_URL", "https://wzstats.gg/fr"),
    check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "10")),
    log_level=os.getenv("LOG_LEVEL", "INFO"),
)
