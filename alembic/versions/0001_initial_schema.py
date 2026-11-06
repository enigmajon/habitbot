"""Initial schema creating all tables and indexes for Telegram Habit & Time Tracker Bot.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-11-05 00:00:00.000000

Compatible with both PostgreSQL (asyncpg) and SQLite (aiosqlite).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. users
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False, server_default="uz"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Tashkent"),
        sa.Column("morning_digest_time", sa.Time(), nullable=True),
        sa.Column("morning_digest_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("summary_time", sa.Time(), nullable=False),
        sa.Column("idle_nudge_time", sa.Time(), nullable=False),
        sa.Column("idle_nudge_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("long_timer_threshold_minutes", sa.Integer(), nullable=False, server_default="180"),
        sa.Column("mood_prompt_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("pomodoro_work_min", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("pomodoro_break_min", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("pomodoro_long_break_min", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("streak_threshold", sa.Integer(), nullable=False, server_default="70"),
        sa.Column("freeze_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("onboarded", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    # 2. categories
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("emoji", sa.String(length=16), nullable=False, server_default="📁"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_categories_user_name"),
    )
    op.create_index("ix_categories_user_id", "categories", ["user_id"], unique=False)

    # 3. activities
    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("emoji", sa.String(length=16), nullable=False, server_default="⏱️"),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="build"),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activities_user_archived", "activities", ["user_id", "archived"], unique=False)

    # 4. goals
    op.create_table(
        "goals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("target_minutes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", "period", name="uq_goals_activity_period"),
    )
    op.create_index("ix_goals_activity_id", "goals", ["activity_id"], unique=False)

    # 5. sessions (includes active_timers columns + partial unique index for 1 active timer per user)
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="timer"),
        sa.Column("mood", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="stopwatch"),
        sa.Column("pomodoro_phase", sa.String(length=16), nullable=True),
        sa.Column("phase_ends_at", sa.DateTime(), nullable=True),
        sa.Column("cycle_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_long_timer_prompt_at", sa.DateTime(), nullable=True),
        sa.Column("long_timer_prompt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("flagged_over_cap", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_user_started_at", "sessions", ["user_id", "started_at"], unique=False)
    op.create_index(
        "ix_sessions_one_active_per_user",
        "sessions",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("ended_at IS NULL AND deleted_at IS NULL"),
        postgresql_where=sa.text("ended_at IS NULL AND deleted_at IS NULL"),
    )

    # 6. plans
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=40), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("repeat_rule", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("repeat_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pre_reminder_minutes", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plans_user_active", "plans", ["user_id", "active"], unique=False)

    # 7. plan_occurrences
    op.create_table(
        "plan_occurrences",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("pre_reminder_sent", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("digest_sent", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "local_date", name="uq_plan_occurrences_plan_date"),
    )
    op.create_index("ix_plan_occurrences_local_date", "plan_occurrences", ["local_date"], unique=False)

    # 8. daily_summaries
    op.create_table(
        "daily_summaries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("is_rest_day", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("finalized", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "local_date", name="uq_daily_summaries_user_date"),
    )
    op.create_index("ix_daily_summaries_user_date", "daily_summaries", ["user_id", "local_date"], unique=False)

    # 9. streaks
    op.create_table(
        "streaks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=True),
        sa.Column("current", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("best", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_counted_date", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_streaks_user_activity", "streaks", ["user_id", "activity_id"], unique=True)

    # 10. undo_log
    op.create_table(
        "undo_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_undo_log_user_created", "undo_log", ["user_id", "created_at"], unique=False)

    # 11. scheduled_jobs
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("run_at", sa.DateTime(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_jobs_pending_run_at",
        "scheduled_jobs",
        ["run_at"],
        unique=False,
        sqlite_where=sa.text("sent_at IS NULL"),
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_index("ix_scheduled_jobs_user_kind", "scheduled_jobs", ["user_id", "kind"], unique=False)

    # 12. fsm_states
    op.create_table(
        "fsm_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=True),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "chat_id", "user_id", name="uq_fsm_bot_chat_user"),
    )


def downgrade() -> None:
    op.drop_table("fsm_states")
    op.drop_index("ix_scheduled_jobs_user_kind", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_pending_run_at", table_name="scheduled_jobs")
    op.drop_table("scheduled_jobs")
    op.drop_index("ix_undo_log_user_created", table_name="undo_log")
    op.drop_table("undo_log")
    op.drop_index("ix_streaks_user_activity", table_name="streaks")
    op.drop_table("streaks")
    op.drop_index("ix_daily_summaries_user_date", table_name="daily_summaries")
    op.drop_table("daily_summaries")
    op.drop_index("ix_plan_occurrences_local_date", table_name="plan_occurrences")
    op.drop_table("plan_occurrences")
    op.drop_index("ix_plans_user_active", table_name="plans")
    op.drop_table("plans")
    op.drop_index("ix_sessions_one_active_per_user", table_name="sessions")
    op.drop_index("ix_sessions_user_started_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_goals_activity_id", table_name="goals")
    op.drop_table("goals")
    op.drop_index("ix_activities_user_archived", table_name="activities")
    op.drop_table("activities")
    op.drop_index("ix_categories_user_id", table_name="categories")
    op.drop_table("categories")
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")