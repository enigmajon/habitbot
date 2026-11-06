"""
Configuration and structured logging setup for the Telegram Habit & Time Tracker Bot.

Architectural choices & ambiguity resolutions:
1. Environment variables:
   Loaded via `pydantic-settings` (`BaseSettings`) with `.env` file support.
   Required variables: `BOT_TOKEN`.
   Optional variables with sensible defaults:
   - `DATABASE_URL`: defaults to `sqlite+aiosqlite:///./habit_tracker.db` so the bot
     runs out-of-the-box with zero external database setup (and seamlessly switches to
     PostgreSQL when `postgresql+asyncpg://...` is provided, e.g., on Neon or Supabase).
   - `DEFAULT_TZ`: defaults to `Asia/Tashkent` (UTC+5).
   - `LOG_LEVEL`: defaults to `INFO`.
2. Privacy-safe structured logging (Section 11):
   Configures a structured formatter that never logs secrets (`BOT_TOKEN`, `DATABASE_URL`)
   or raw user message text.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    BOT_TOKEN: str = Field(
        default="9189327303:AAExMtCFUpVZgyWcj1AdOgpUQhjOi_0B56o",
        description="Telegram Bot API token from @BotFather",
    )
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./habit_tracker.db",
        description="SQLAlchemy async database URL (PostgreSQL asyncpg or SQLite aiosqlite)",
    )
    DEFAULT_TZ: str = Field(
        default="Asia/Tashkent",
        description="Default IANA timezone for new users",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    # Scheduler & Rate Limit Tunables
    SCHEDULER_INTERVAL_SECONDS: int = Field(
        default=30,
        description="Interval in seconds for the DB-driven scheduler loop",
    )
    GLOBAL_MSG_RATE_LIMIT: int = Field(
        default=25,
        description="Maximum outgoing Telegram messages per second globally",
    )
    FSM_TIMEOUT_MINUTES: int = Field(
        default=10,
        description="Stale FSM state expiration timeout in minutes",
    )

    @field_validator("DEFAULT_TZ")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid IANA timezone: {v}") from exc
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid LOG_LEVEL: {v}")
        return level

    @field_validator("DATABASE_URL")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        """
        Automatically upgrade standard postgres:// or postgresql:// URLs (common on
        free cloud providers like Neon, Supabase, and Render) to postgresql+asyncpg://
        and sqlite:/// to sqlite+aiosqlite:///.
        """
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        if v.startswith("sqlite://") and "+aiosqlite" not in v:
            return v.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return v


class RedactingFilter(logging.Filter):
    """Filter that masks bot tokens and DB credentials if they ever appear in log args."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) > 4]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                record.msg = msg.replace(secret, "***REDACTED***")
                record.args = ()
        return True


def setup_logging(settings: Settings) -> None:
    """Configure structured key=value logging across the application."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RedactingFilter([settings.BOT_TOKEN, settings.DATABASE_URL]))

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Silence overly verbose third-party loggers in production
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
