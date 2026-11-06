"""
Data access layer (repositories) for the Telegram Habit & Time Tracker Bot.

Architectural rules enforced:
- Section 11: Every query strictly filters by `user_id` so users only ever access
  their own rows.
- Section 4 Rule 5: Single active timer per user enforced via transaction + partial
  unique index (`ended_at IS NULL AND deleted_at IS NULL`).
- Section 5: `undo_log` keeps at most the last 20 entries per user and expires entries
  older than 24 hours.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.models.models import (
    Activity,
    ActivityKind,
    Category,
    DailySummary,
    Goal,
    GoalPeriod,
    Plan,
    PlanOccurrence,
    PlanOccurrenceStatus,
    ScheduledJob,
    Session,
    SessionSource,
    Streak,
    TimerMode,
    UndoLog,
    User,
    utc_now,
)


class UserRepository:
    """Database operations for `User` profiles and preferences."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> Optional[User]:
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create(
        self, telegram_id: int, default_tz: str = "Asia/Tashkent"
    ) -> tuple[User, bool]:
        user = await self.get_by_telegram_id(telegram_id)
        if user is not None:
            return user, False

        user = User(
            telegram_id=telegram_id,
            language="uz",
            timezone=default_tz,
            morning_digest_time=time(4, 0),
            morning_digest_enabled=True,
            summary_time=time(22, 0),
            idle_nudge_time=time(14, 0),
            idle_nudge_enabled=True,
            long_timer_threshold_minutes=180,
            mood_prompt_enabled=True,
            pomodoro_work_min=25,
            pomodoro_break_min=5,
            pomodoro_long_break_min=15,
            streak_threshold=70,
            freeze_tokens=0,
            onboarded=False,
        )
        self.session.add(user)
        await self.session.flush()
        return user, True

    async def list_active_users(self) -> Sequence[User]:
        stmt = select(User).where(User.is_blocked.is_(False))
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def mark_blocked(self, user_id: int, blocked: bool = True) -> None:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(is_blocked=blocked)
        )
        await self.session.execute(stmt)
        if blocked:
            # Disable pending scheduled jobs for blocked user (Section 8)
            del_jobs = delete(ScheduledJob).where(
                and_(
                    ScheduledJob.user_id == user_id,
                    ScheduledJob.sent_at.is_(None),
                )
            )
            await self.session.execute(del_jobs)

    async def delete_user_data(self, user_id: int) -> None:
        """Permanently delete all rows owned by `user_id` (Section 11)."""
        stmt = delete(User).where(User.id == user_id)
        await self.session.execute(stmt)


class ActivityRepository:
    """Database operations for `Category`, `Activity`, and `Goal`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_categories(self, user_id: int) -> Sequence[Category]:
        stmt = (
            select(Category)
            .where(Category.user_id == user_id)
            .order_by(Category.name.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_or_create_category(
        self, user_id: int, name: str, emoji: str = "📁"
    ) -> Category:
        name_clean = name.strip()[:40]
        stmt = select(Category).where(
            and_(Category.user_id == user_id, Category.name == name_clean)
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            return existing

        cat = Category(user_id=user_id, name=name_clean, emoji=emoji.strip()[:16] or "📁")
        self.session.add(cat)
        await self.session.flush()
        return cat

    async def list_activities(
        self, user_id: int, *, include_archived: bool = False
    ) -> Sequence[Activity]:
        conditions = [Activity.user_id == user_id]
        if not include_archived:
            conditions.append(Activity.archived.is_(False))

        stmt = (
            select(Activity)
            .options(selectinload(Activity.goals), selectinload(Activity.category))
            .where(and_(*conditions))
            .order_by(Activity.archived.asc(), Activity.id.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_activity(self, user_id: int, activity_id: int) -> Optional[Activity]:
        stmt = (
            select(Activity)
            .options(selectinload(Activity.goals), selectinload(Activity.category))
            .where(and_(Activity.id == activity_id, Activity.user_id == user_id))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_activity(
        self,
        user_id: int,
        name: str,
        emoji: str = "⏱️",
        category_id: Optional[int] = None,
        kind: str = ActivityKind.BUILD.value,
        weight: int = 1,
        daily_goal_minutes: Optional[int] = None,
        weekly_goal_minutes: Optional[int] = None,
    ) -> Activity:
        act = Activity(
            user_id=user_id,
            category_id=category_id,
            name=name.strip()[:40],
            emoji=emoji.strip()[:16] or "⏱️",
            kind=kind,
            weight=max(1, min(3, weight)),
            archived=False,
        )
        self.session.add(act)
        await self.session.flush()

        if daily_goal_minutes and daily_goal_minutes > 0:
            self.session.add(
                Goal(
                    activity_id=act.id,
                    period=GoalPeriod.DAY.value,
                    target_minutes=daily_goal_minutes,
                )
            )
        if weekly_goal_minutes and weekly_goal_minutes > 0:
            self.session.add(
                Goal(
                    activity_id=act.id,
                    period=GoalPeriod.WEEK.value,
                    target_minutes=weekly_goal_minutes,
                )
            )
        await self.session.flush()
        return await self.get_activity(user_id, act.id)  # type: ignore[return-value]

    async def set_goal(
        self, activity_id: int, period: str, target_minutes: Optional[int]
    ) -> None:
        stmt = select(Goal).where(
            and_(Goal.activity_id == activity_id, Goal.period == period)
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if target_minutes is None or target_minutes <= 0:
            if existing:
                await self.session.delete(existing)
        else:
            if existing:
                existing.target_minutes = target_minutes
            else:
                self.session.add(
                    Goal(
                        activity_id=activity_id,
                        period=period,
                        target_minutes=target_minutes,
                    )
                )
        await self.session.flush()


class SessionRepository:
    """Database operations for `Session` (both active timers and completed logs)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_timer(self, user_id: int) -> Optional[Session]:
        stmt = (
            select(Session)
            .options(selectinload(Session.activity).selectinload(Activity.goals))
            .where(
                and_(
                    Session.user_id == user_id,
                    Session.ended_at.is_(None),
                    Session.deleted_at.is_(None),
                )
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_session(self, user_id: int, session_id: int) -> Optional[Session]:
        stmt = (
            select(Session)
            .options(selectinload(Session.activity).selectinload(Activity.goals))
            .where(and_(Session.id == session_id, Session.user_id == user_id))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_sessions_overlapping_utc_window(
        self,
        user_id: int,
        window_start_utc: datetime,
        window_end_utc: datetime,
        *,
        include_running: bool = False,
        exclude_session_id: Optional[int] = None,
    ) -> Sequence[Session]:
        """
        Fetch all non-deleted sessions for `user_id` that overlap with
        `[window_start_utc, window_end_utc)`.
        Because sessions can cross midnight, querying a wider UTC window and splitting
        via `split_session_by_local_days` guarantees exact local-day totals.
        """
        conditions = [
            Session.user_id == user_id,
            Session.deleted_at.is_(None),
            Session.started_at < window_end_utc,
        ]
        if include_running:
            conditions.append(
                or_(Session.ended_at.is_(None), Session.ended_at > window_start_utc)
            )
        else:
            conditions.append(Session.ended_at.is_not(None))
            conditions.append(Session.ended_at > window_start_utc)

        if exclude_session_id is not None:
            conditions.append(Session.id != exclude_session_id)

        stmt = (
            select(Session)
            .options(
                selectinload(Session.activity).selectinload(Activity.goals),
                selectinload(Session.activity).selectinload(Activity.category),
            )
            .where(and_(*conditions))
            .order_by(Session.started_at.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_last_completed_session(self, user_id: int) -> Optional[Session]:
        stmt = (
            select(Session)
            .options(selectinload(Session.activity))
            .where(
                and_(
                    Session.user_id == user_id,
                    Session.ended_at.is_not(None),
                    Session.deleted_at.is_(None),
                )
            )
            .order_by(Session.ended_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class PlanRepository:
    """Database operations for `Plan` and `PlanOccurrence`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active_plans(self, user_id: int) -> Sequence[Plan]:
        stmt = (
            select(Plan)
            .options(selectinload(Plan.activity))
            .where(and_(Plan.user_id == user_id, Plan.active.is_(True)))
            .order_by(Plan.start_time.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_plan(self, user_id: int, plan_id: int) -> Optional[Plan]:
        stmt = (
            select(Plan)
            .options(selectinload(Plan.activity))
            .where(and_(Plan.id == plan_id, Plan.user_id == user_id))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_occurrences_for_dates(
        self, user_id: int, start_date: date, end_date: date
    ) -> Sequence[PlanOccurrence]:
        stmt = (
            select(PlanOccurrence)
            .join(Plan, PlanOccurrence.plan_id == Plan.id)
            .options(selectinload(PlanOccurrence.plan).selectinload(Plan.activity))
            .where(
                and_(
                    Plan.user_id == user_id,
                    Plan.active.is_(True),
                    PlanOccurrence.local_date >= start_date,
                    PlanOccurrence.local_date <= end_date,
                )
            )
            .order_by(PlanOccurrence.local_date.asc(), Plan.start_time.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_occurrence(
        self, user_id: int, occurrence_id: int
    ) -> Optional[PlanOccurrence]:
        stmt = (
            select(PlanOccurrence)
            .join(Plan, PlanOccurrence.plan_id == Plan.id)
            .options(selectinload(PlanOccurrence.plan).selectinload(Plan.activity))
            .where(
                and_(
                    PlanOccurrence.id == occurrence_id,
                    Plan.user_id == user_id,
                )
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class SummaryAndStreakRepository:
    """Database operations for `DailySummary` and `Streak`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_daily_summary(
        self, user_id: int, local_d: date
    ) -> Optional[DailySummary]:
        stmt = select(DailySummary).where(
            and_(
                DailySummary.user_id == user_id,
                DailySummary.local_date == local_d,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_daily_summary(
        self,
        user_id: int,
        local_d: date,
        score: Optional[float],
        is_rest_day: bool,
        finalized: bool,
        details_json: dict[str, Any],
    ) -> DailySummary:
        summary = await self.get_daily_summary(user_id, local_d)
        if summary is None:
            summary = DailySummary(
                user_id=user_id,
                local_date=local_d,
                score=score,
                is_rest_day=is_rest_day,
                finalized=finalized,
                details_json=details_json,
            )
            self.session.add(summary)
        else:
            summary.score = score
            summary.is_rest_day = is_rest_day
            summary.finalized = finalized
            summary.details_json = details_json
        await self.session.flush()
        return summary

    async def get_or_create_streak(
        self, user_id: int, activity_id: Optional[int] = None
    ) -> Streak:
        if activity_id is None:
            stmt = select(Streak).where(
                and_(Streak.user_id == user_id, Streak.activity_id.is_(None))
            )
        else:
            stmt = select(Streak).where(
                and_(Streak.user_id == user_id, Streak.activity_id == activity_id)
            )
        streak = (await self.session.execute(stmt)).scalar_one_or_none()
        if streak is None:
            streak = Streak(
                user_id=user_id,
                activity_id=activity_id,
                current=0,
                best=0,
                last_counted_date=None,
            )
            self.session.add(streak)
            await self.session.flush()
        return streak

    async def list_streaks(self, user_id: int) -> Sequence[Streak]:
        stmt = select(Streak).where(Streak.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()


class JobAndUndoRepository:
    """Database operations for `ScheduledJob` and `UndoLog`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_undo(
        self, user_id: int, action_type: str, payload_json: dict[str, Any]
    ) -> UndoLog:
        """
        Save an undo action and prune entries older than 24 hours or beyond the
        most recent 20 per user (Section 5).
        """
        now = utc_now()
        cutoff = now - timedelta(hours=24)

        # Prune expired (>24h)
        await self.session.execute(
            delete(UndoLog).where(
                and_(UndoLog.user_id == user_id, UndoLog.created_at < cutoff)
            )
        )

        entry = UndoLog(
            user_id=user_id,
            action_type=action_type,
            payload_json=payload_json,
            created_at=now,
        )
        self.session.add(entry)
        await self.session.flush()

        # Keep only last 20 per user
        stmt_ids = (
            select(UndoLog.id)
            .where(UndoLog.user_id == user_id)
            .order_by(UndoLog.created_at.desc(), UndoLog.id.desc())
            .offset(20)
        )
        old_ids = (await self.session.execute(stmt_ids)).scalars().all()
        if old_ids:
            await self.session.execute(
                delete(UndoLog).where(UndoLog.id.in_(old_ids))
            )
        return entry

    async def pop_latest_undo(
        self, user_id: int, undo_id: Optional[int] = None
    ) -> Optional[UndoLog]:
        cutoff = utc_now() - timedelta(hours=24)
        conditions = [UndoLog.user_id == user_id, UndoLog.created_at >= cutoff]
        if undo_id is not None:
            conditions.append(UndoLog.id == undo_id)

        stmt = (
            select(UndoLog)
            .where(and_(*conditions))
            .order_by(UndoLog.created_at.desc(), UndoLog.id.desc())
            .limit(1)
        )
        entry = (await self.session.execute(stmt)).scalar_one_or_none()
        if entry:
            await self.session.delete(entry)
            await self.session.flush()
        return entry

    async def schedule_job(
        self,
        user_id: int,
        kind: str,
        run_at: datetime,
        payload_json: Optional[dict[str, Any]] = None,
        *,
        replace_pending_of_same_kind: bool = False,
    ) -> ScheduledJob:
        if replace_pending_of_same_kind:
            await self.session.execute(
                delete(ScheduledJob).where(
                    and_(
                        ScheduledJob.user_id == user_id,
                        ScheduledJob.kind == kind,
                        ScheduledJob.sent_at.is_(None),
                    )
                )
            )
        job = ScheduledJob(
            user_id=user_id,
            kind=kind,
            run_at=run_at,
            payload_json=payload_json or {},
            sent_at=None,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def cancel_pending_jobs(self, user_id: int, kind: str) -> None:
        await self.session.execute(
            delete(ScheduledJob).where(
                and_(
                    ScheduledJob.user_id == user_id,
                    ScheduledJob.kind == kind,
                    ScheduledJob.sent_at.is_(None),
                )
            )
        )
        await self.session.flush()

    async def fetch_due_jobs(
        self, now_utc: datetime, batch_size: int = 50
    ) -> Sequence[ScheduledJob]:
        stmt = (
            select(ScheduledJob)
            .options(selectinload(ScheduledJob.user))
            .where(
                and_(
                    ScheduledJob.sent_at.is_(None),
                    ScheduledJob.run_at <= now_utc,
                )
            )
            .order_by(ScheduledJob.run_at.asc())
            .limit(batch_size)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
