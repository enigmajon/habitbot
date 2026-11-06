"""
Database models and schema definitions for the Telegram Habit & Time Tracker Bot.

Architectural choices & ambiguity resolutions:
1. Active Timers vs. Sessions:
   Section 4 Rule 5 specifies: "One active timer per user. Enforce with a partial
   unique index (WHERE ended_at IS NULL) and a transaction, so double-taps cannot
   create two timers."
   Section 5 specifies: "active_timers (or the null-ended_at row in sessions):
   user_id, activity_id, started_at, mode (stopwatch / pomodoro), pomodoro_phase,
   phase_ends_at, cycle_count, last_long_timer_prompt_at."
   We store active timer state directly on the `sessions` table (where `ended_at IS NULL`)
   and enforce the single active timer constraint via a partial unique index
   `ix_sessions_one_active_per_user` on `sessions(user_id) WHERE ended_at IS NULL AND deleted_at IS NULL`.
   Both SQLite (3.8.0+) and PostgreSQL natively support ` sqlite_where` / `postgresql_where`
   partial unique indexes in SQLAlchemy 2.x without any code changes!
2. Database Compatibility (PostgreSQL + SQLite):
   All datetime columns use timezone-aware UTC timestamps stored cleanly via a custom
   `UTCDateTime` TypeDecorator so both asyncpg (PostgreSQL) and aiosqlite (SQLite)
   always return timezone-aware `datetime` objects in UTC (`timezone.utc`).
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Ensures all datetimes are stored as naive UTC in DB and loaded as aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    """Return current UTC datetime with tzinfo=timezone.utc."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Language(StrEnum):
    UZ = "uz"
    EN = "en"


class ActivityKind(StrEnum):
    BUILD = "build"
    LIMIT = "limit"


class GoalPeriod(StrEnum):
    DAY = "day"
    WEEK = "week"


class SessionSource(StrEnum):
    TIMER = "timer"
    MANUAL = "manual"
    POMODORO = "pomodoro"


class TimerMode(StrEnum):
    STOPWATCH = "stopwatch"
    POMODORO = "pomodoro"


class PomodoroPhase(StrEnum):
    WORK = "work"
    SHORT_BREAK = "break"
    LONG_BREAK = "long_break"


class RepeatRule(StrEnum):
    NONE = "none"
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    CUSTOM = "custom"


class PlanOccurrenceStatus(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    DONE = "done"
    SKIPPED = "skipped"


class ScheduledJobKind(StrEnum):
    MORNING_DIGEST = "morning_digest"
    PLAN_PRE_REMINDER = "plan_pre_reminder"
    DAY_SUMMARY = "day_summary"
    IDLE_NUDGE = "idle_nudge"
    WEEKLY_REPORT = "weekly_report"
    LONG_TIMER_CHECK = "long_timer_check"
    POMODORO_PHASE_END = "pomodoro_phase_end"


class UndoActionType(StrEnum):
    CREATE_SESSION = "create_session"
    STOP_TIMER = "stop_timer"
    DELETE_SESSION = "delete_session"
    EDIT_SESSION = "edit_session"


# ---------------------------------------------------------------------------
# Declarative Base
# ---------------------------------------------------------------------------

class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all SQLAlchemy 2.x ORM models."""
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(5), nullable=False, default=Language.UZ.value)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tashkent")

    # Schedule preferences (stored as HH:MM local time strings or Time objects; Time used here)
    morning_digest_time: Mapped[Optional[time]] = mapped_column(
        Time, nullable=True, default=time(4, 0)
    )
    morning_digest_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    summary_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(22, 0))
    idle_nudge_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(14, 0))
    idle_nudge_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Timer & Pomodoro settings
    long_timer_threshold_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    mood_prompt_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pomodoro_work_min: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    pomodoro_break_min: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    pomodoro_long_break_min: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    # Gamification & Streaks
    streak_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=70)
    freeze_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Bot delivery state
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    onboarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)

    # Relationships
    categories: Mapped[list["Category"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    activities: Mapped[list["Activity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    plans: Mapped[list["Plan"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    daily_summaries: Mapped[list["DailySummary"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    streaks: Mapped[list["Streak"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    undo_logs: Mapped[list["UndoLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    scheduled_jobs: Mapped[list["ScheduledJob"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_categories_user_name"),
        Index("ix_categories_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False, default="📁")

    user: Mapped["User"] = relationship(back_populates="categories")
    activities: Mapped[list["Activity"]] = relationship(back_populates="category")


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (
        Index("ix_activities_user_archived", "user_id", "archived"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False, default="⏱️")
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ActivityKind.BUILD.value
    )
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 1 to 3
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)

    user: Mapped["User"] = relationship(back_populates="activities")
    category: Mapped[Optional["Category"]] = relationship(back_populates="activities")
    goals: Mapped[list["Goal"]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )
    plans: Mapped[list["Plan"]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (
        UniqueConstraint("activity_id", "period", name="uq_goals_activity_period"),
        Index("ix_goals_activity_id", "activity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("activities.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[str] = mapped_column(String(16), nullable=False)  # 'day' or 'week'
    target_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    activity: Mapped["Activity"] = relationship(back_populates="goals")


class Session(Base):
    """
    Stores both completed time sessions and the single currently running timer per user
    (where `ended_at IS NULL`).
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_started_at", "user_id", "started_at"),
        # Rule 5: One active timer per user enforced via partial unique index
        Index(
            "ix_sessions_one_active_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL AND deleted_at IS NULL"),
            postgresql_where=text("ended_at IS NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    activity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("activities.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SessionSource.TIMER.value
    )
    mood: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1 to 5
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    # Active timer / Pomodoro state columns (populated while ended_at IS NULL)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TimerMode.STOPWATCH.value
    )
    pomodoro_phase: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    phase_ends_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    cycle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_long_timer_prompt_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    long_timer_Prompt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flagged_over_cap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped["User"] = relationship(back_populates="sessions")
    activity: Mapped["Activity"] = relationship(back_populates="sessions")


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        Index("ix_plans_user_active", "user_id", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    activity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("activities.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)  # Start/anchor local date
    start_time: Mapped[time] = mapped_column(Time, nullable=False)  # Local start time
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    repeat_rule: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RepeatRule.NONE.value
    )
    repeat_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # Bitmask: Mon=1<<0 (1) ... Sun=1<<6 (64)
    pre_reminder_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped["User"] = relationship(back_populates="plans")
    activity: Mapped[Optional["Activity"]] = relationship(back_populates="plans")
    occurrences: Mapped[list["PlanOccurrence"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class PlanOccurrence(Base):
    __tablename__ = "plan_occurrences"
    __table_args__ = (
        UniqueConstraint("plan_id", "local_date", name="uq_plan_occurrences_plan_date"),
        Index("ix_plan_occurrences_local_date", "local_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PlanOccurrenceStatus.PENDING.value
    )
    pre_reminder_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    digest_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    plan: Mapped["Plan"] = relationship(back_populates="occurrences")


class DailySummary(Base):
    __tablename__ = "daily_summaries"
    __table_args__ = (
        UniqueConstraint("user_id", "local_date", name="uq_daily_summaries_user_date"),
        Index("ix_daily_summaries_user_date", "user_id", "local_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_rest_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    finalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    user: Mapped["User"] = relationship(back_populates="daily_summaries")


class Streak(Base):
    __tablename__ = "streaks"
    __table_args__ = (
        Index("ix_streaks_user_activity", "user_id", "activity_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    activity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("activities.id", ondelete="CASCADE"), nullable=True
    )  # NULL = overall streak
    current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_counted_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    user: Mapped["User"] = relationship(back_populates="streaks")


class UndoLog(Base):
    __tablename__ = "undo_log"
    __table_args__ = (
        Index("ix_undo_log_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)

    user: Mapped["User"] = relationship(back_populates="undo_logs")


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        # Index on (run_at) for pending jobs as required by Section 5
        Index(
            "ix_scheduled_jobs_pending_run_at",
            "run_at",
            sqlite_where=text("sent_at IS NULL"),
            postgresql_where=text("sent_at IS NULL"),
        ),
        Index("ix_scheduled_jobs_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="scheduled_jobs")


class FSMStateRecord(Base):
    """
    Database-backed finite-state machine storage for aiogram 3.x with a 10-minute
    staleness timeout (Section 7 UI rules).
    """

    __tablename__ = "fsm_states"
    __table_args__ = (
        UniqueConstraint("bot_id", "chat_id", "user_id", name="uq_fsm_bot_chat_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Engine & Session Factory Helpers
# ---------------------------------------------------------------------------

def create_db_engine(database_url: str, echo: bool = False):
    """Create an async SQLAlchemy engine supporting both PostgreSQL and SQLite."""
    return create_async_engine(database_url, echo=echo, future=True)


def create_session_factory(engine) -> async_sessionmaker:
    """Create an async session factory with expire_on_commit=False."""
    return async_sessionmaker(engine, expire_on_commit=False)
