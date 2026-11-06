"""
Core deterministic business logic services for the Telegram Habit & Time Tracker Bot.

Implements:
- Timer & Pomodoro Service (Section 6.2, 6.4, 6.5b, Section 9):
  - Start/stop/switch stopwatch & Pomodoro work/break intervals.
  - Discard sessions under 30 seconds with a friendly notice (Section 9).
  - Forgot-to-stop checks (`long_timer_threshold_minutes`, +1h re-asks, 16h hard cap).
  - Archiving an activity stops its running timer first (Section 9).
- Daily Scoring & Weekly Pace Service (Section 6.5, 6.6):
  - Exact build vs. limit daily rating formula:
      build: ratio = min(actual / target, 1)
      limit: ratio = 1 if actual <= max, else max(0, 1 - (actual - max) / max)
      contribution = ratio * weight
      score = sum(contribution) / sum(weight) * 100
  - Score bands: >=90 🔥, 70-89 ✅, 50-69 😐, <50 💤.
  - Weekly goal remaining pace (`needed per remaining day`).
- Streak & Freeze Token Service (Section 6.7):
  - Per-activity and overall streaks.
  - Earn 1 freeze token every 7-day overall streak (max 2 held).
  - Auto-use freeze token on first missed day; rest days preserve streaks without spending tokens.
  - Recalculate finalized past days and affected streaks when past sessions are edited (Section 6.6 & 9).
- Planner / Calendar Service (Section 6.4b):
  - Repeat rules (`none`, `daily`, `weekdays`, `custom` weekday bitmask) and 14-day occurrence generation.
- Export Service (Section 6.12):
  - UTF-8 with BOM CSV export in user's local timezone.
- Undo Service (Section 6.10):
  - Revert `create_session`, `stop_timer`, `delete_session`, and `edit_session` within 24h.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from bot.models.models import (
    Activity,
    ActivityKind,
    DailySummary,
    GoalPeriod,
    Plan,
    PlanOccurrence,
    PlanOccurrenceStatus,
    PomodoroPhase,
    RepeatRule,
    ScheduledJobKind,
    Session,
    SessionSource,
    TimerMode,
    UndoActionType,
    User,
    utc_now,
)
from bot.repositories import (
    ActivityRepository,
    JobAndUndoRepository,
    PlanRepository,
    SessionRepository,
    SummaryAndStreakRepository,
)
from bot.services.time_utils import (
    format_duration,
    format_progress_bar,
    get_zoneinfo,
    local_day_bounds_utc,
    local_today,
    local_week_bounds,
    split_session_by_local_days,
    to_local,
)


# ---------------------------------------------------------------------------
# 1. Pure Deterministic Scoring & Weekly Pace Formulas (Section 6.5 & 6.6)
# ---------------------------------------------------------------------------

@dataclass
class ActivityDayStat:
    activity_id: int
    name: str
    emoji: str
    category_name: str
    kind: str  # 'build' or 'limit'
    weight: int
    actual_minutes: float
    daily_target_minutes: Optional[int]
    weekly_target_minutes: Optional[int]
    ratio: Optional[float]
    weighted_contribution: Optional[float]


@dataclass
class DailyEvaluation:
    local_date: date
    is_rest_day: bool
    total_minutes: float
    score: Optional[float]
    band_emoji: str
    mood_average: Optional[float]
    activities: list[ActivityDayStat]
    planned_done: list[str]
    planned_missed: list[str]


def compute_activity_ratio(
    kind: str, actual_minutes: float, target_minutes: int
) -> float:
    """
    Compute per-activity daily goal ratio according to Section 6.6:
      build: ratio = min(actual / target, 1)
      limit: ratio = 1 if actual <= max, else max(0, 1 - (actual - max) / max)
    """
    if target_minutes <= 0:
        return 1.0
    if kind == ActivityKind.LIMIT.value:
        if actual_minutes <= target_minutes:
            return 1.0
        return max(0.0, 1.0 - (actual_minutes - target_minutes) / float(target_minutes))
    return min(actual_minutes / float(target_minutes), 1.0)


def compute_daily_score(
    items: Sequence[tuple[str, float, Optional[int], int]]
) -> Optional[float]:
    """
    Calculate the daily score (0..100) from `(kind, actual_minutes, daily_target_minutes, weight)`.
    Activities without a daily goal (`daily_target_minutes is None`) do not affect the score.
    Returns `None` if no activity has a daily goal (Section 6.6).
    """
    total_weight = 0
    total_contribution = 0.0

    for kind, actual_minutes, target_minutes, weight in items:
        if target_minutes is None or target_minutes <= 0:
            continue
        ratio = compute_activity_ratio(kind, actual_minutes, target_minutes)
        w = max(1, min(3, int(weight)))
        total_contribution += ratio * w
        total_weight += w

    if total_weight == 0:
        return None
    return round((total_contribution / total_weight) * 100.0, 1)


def get_score_band(score: Optional[float], is_rest_day: bool = False) -> str:
    """
    Return the rating band emoji from Section 6.6:
    90 and above 🔥, 70 to 89 ✅, 50 to 69 😐, below 50 💤.
    """
    if is_rest_day:
        return "🛋️"
    if score is None:
        return ""
    if score >= 90:
        return "🔥"
    if score >= 70:
        return "✅"
    if score >= 50:
        return "😐"
    return "💤"


def compute_weekly_needed_per_day(
    actual_week_minutes: float,
    weekly_target_minutes: int,
    current_local_date: date,
) -> tuple[int, bool]:
    """
    Compute remaining minutes needed per remaining day of the week (including today),
    and whether the activity is behind linear pace (Section 6.5).
    Weeks run Monday (weekday=0) to Sunday (weekday=6).
    """
    remaining_minutes = max(0.0, float(weekly_target_minutes) - actual_week_minutes)
    days_elapsed_before_today = current_local_date.weekday()  # 0 on Mon .. 6 on Sun
    remaining_days = max(1, 7 - days_elapsed_before_today)
    needed_per_day = int(round(remaining_minutes / remaining_days))

    # Behind pace if by start of today (or current progress) actual is less than expected pro-rata
    expected_by_now = (weekly_target_minutes / 7.0) * days_elapsed_before_today
    behind_pace = actual_week_minutes + 1e-6 < expected_by_now
    return needed_per_day, behind_pace


# ---------------------------------------------------------------------------
# 2. Streak & Freeze Token Pure Logic + Service (Section 6.7)
# ---------------------------------------------------------------------------

@dataclass
class StreakTransitionResult:
    new_current: int
    new_best: int
    new_freeze_tokens: int
    used_freeze_token: bool
    earned_freeze_token: bool


def evaluate_overall_streak_day(
    *,
    prev_current: int,
    prev_best: int,
    freeze_tokens: int,
    score: Optional[float],
    streak_threshold: int,
    is_rest_day: bool,
) -> StreakTransitionResult:
    """
    Pure state transition for a single finalized day's overall streak (Section 6.7):
    - Rest day: does not break streak, does not spend a freeze token, does not increment.
    - Score >= streak_threshold: increments current streak by 1; every 7-day multiple
      earns 1 freeze token (up to max 2 held).
    - Score < streak_threshold (or None when user had daily goals that weren't met):
      if freeze_tokens > 0, auto-uses 1 token and preserves `prev_current`;
      otherwise resets `current` to 0.
    """
    if is_rest_day:
        return StreakTransitionResult(
            new_current=prev_current,
            new_best=max(prev_best, prev_current),
            new_freeze_tokens=freeze_tokens,
            used_freeze_token=False,
            earned_freeze_token=False,
        )

    if score is not None and score >= streak_threshold:
        cur = prev_current + 1
        best = max(prev_best, cur)
        earned = (cur > 0 and cur % 7 == 0 and freeze_tokens < 2)
        tokens = min(2, freeze_tokens + (1 if earned else 0))
        return StreakTransitionResult(
            new_current=cur,
            new_best=best,
            new_freeze_tokens=tokens,
            used_freeze_token=False,
            earned_freeze_token=earned,
        )

    # Missed day
    if score is None:
        # No daily goals configured at all -> neutral day, preserve streak
        return StreakTransitionResult(
            new_current=prev_current,
            new_best=max(prev_best, prev_current),
            new_freeze_tokens=freeze_tokens,
            used_freeze_token=False,
            earned_freeze_token=False,
        )

    if freeze_tokens > 0 and prev_current > 0:
        return StreakTransitionResult(
            new_current=prev_current,
            new_best=max(prev_best, prev_current),
            new_freeze_tokens=freeze_tokens - 1,
            used_freeze_token=True,
            earned_freeze_token=False,
        )

    return StreakTransitionResult(
        new_current=0,
        new_best=prev_best,
        new_freeze_tokens=freeze_tokens,
        used_freeze_token=False,
        earned_freeze_token=False,
    )


# ---------------------------------------------------------------------------
# 3. Timer & Session Service (Section 6.2, 6.3, 6.4, 6.5b, 6.10)
# ---------------------------------------------------------------------------

@dataclass
class StopTimerOutcome:
    session: Session
    discarded_under_30s: bool
    duration_seconds: int
    today_activity_minutes: float
    daily_goal_minutes: Optional[int]


class TimerService:
    """Manages active timers, Pomodoro phases, manual sessions, and daily totals."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.sessions = SessionRepository(db)
        self.activities = ActivityRepository(db)
        self.jobs = JobAndUndoRepository(db)

    async def start_timer(
        self,
        user: User,
        activity_id: int,
        *,
        mode: str = TimerMode.STOPWATCH.value,
        now_utc: Optional[datetime] = None,
    ) -> Session:
        """
        Start a new stopwatch or Pomodoro timer for `user`.
        Caller should check `get_active_timer` first if asking confirmation on switch;
        if called directly while one is running, raises ValueError.
        """
        now = now_utc or utc_now()
        existing = await self.sessions.get_active_timer(user.id)
        if existing is not None:
            raise ValueError("User already has an active timer running")

        activity = await self.activities.get_activity(user.id, activity_id)
        if activity is None or activity.archived:
            raise ValueError("Activity not found or archived")

        pomo_phase = None
        phase_ends_at = None
        cycle_count = 0

        if mode == TimerMode.POMODORO.value:
            pomo_phase = PomodoroPhase.WORK.value
            phase_ends_at = now + timedelta(minutes=user.pomodoro_work_min)
            cycle_count = (await self.count_today_pomodoro_cycles(user, now)) + 1

        active_session = Session(
            user_id=user.id,
            activity_id=activity.id,
            started_at=now,
            ended_at=None,
            source=(
                SessionSource.POMODORO.value
                if mode == TimerMode.POMODORO.value
                else SessionSource.TIMER.value
            ),
            mode=mode,
            pomodoro_phase=pomo_phase,
            phase_ends_at=phase_ends_at,
            cycle_count=cycle_count,
        )
        self.db.add(active_session)
        await self.db.flush()

        # Schedule long_timer_check job (Section 6.4 & Section 8)
        check_at = now + timedelta(minutes=user.long_timer_threshold_minutes)
        await self.jobs.schedule_job(
            user.id,
            ScheduledJobKind.LONG_TIMER_CHECK.value,
            check_at,
            {"session_id": active_session.id},
            replace_pending_of_same_kind=True,
        )

        if mode == TimerMode.POMODORO.value and phase_ends_at is not None:
            await self.jobs.schedule_job(
                user.id,
                ScheduledJobKind.POMODORO_PHASE_END.value,
                phase_ends_at,
                {"session_id": active_session.id, "phase": pomo_phase},
                replace_pending_of_same_kind=True,
            )

        return await self.sessions.get_session(user.id, active_session.id)  # type: ignore[return-value]

    async def stop_active_timer(
        self,
        user: User,
        *,
        custom_end_utc: Optional[datetime] = None,
        record_undo: bool = True,
    ) -> Optional[StopTimerOutcome]:
        """
        Stop the user's currently running timer, discarding sessions shorter than
        30 seconds (Section 9) and computing today's total for that activity.
        """
        active = await self.sessions.get_active_timer(user.id)
        if active is None:
            return None

        now = custom_end_utc or utc_now()
        if now < active.started_at:
            now = active.started_at

        duration_seconds = int((now - active.started_at).total_seconds())
        activity = active.activity

        # Cancel pending timer jobs
        await self.jobs.cancel_pending_jobs(
            user.id, ScheduledJobKind.LONG_TIMER_CHECK.value
        )
        await self.jobs.cancel_pending_jobs(
            user.id, ScheduledJobKind.POMODORO_PHASE_END.value
        )

        # Section 9: Zero-duration and under-1-minute sessions: discard shorter than 30s
        if duration_seconds < 30:
            await self.db.delete(active)
            await self.db.flush()
            return StopTimerOutcome(
                session=active,
                discarded_under_30s=True,
                duration_seconds=duration_seconds,
                today_activity_minutes=0.0,
                daily_goal_minutes=None,
            )

        active.ended_at = now
        active.pomodoro_phase = None
        active.phase_ends_at = None
        await self.db.flush()

        if record_undo:
            await self.jobs.record_undo(
                user.id,
                UndoActionType.STOP_TIMER.value,
                {"session_id": active.id},
            )

        today_d = local_today(user.timezone, now)
        totals_by_act = await self.get_local_date_activity_minutes(user, today_d)
        today_mins = totals_by_act.get(activity.id, 0.0)

        daily_goal = None
        for g in activity.goals:
            if g.period == GoalPeriod.DAY.value:
                daily_goal = g.target_minutes
                break

        return StopTimerOutcome(
            session=active,
            discarded_under_30s=False,
            duration_seconds=duration_seconds,
            today_activity_minutes=today_mins,
            daily_goal_minutes=daily_goal,
        )

    async def count_today_pomodoro_cycles(
        self, user: User, now_utc: Optional[datetime] = None
    ) -> int:
        today_d = local_today(user.timezone, now_utc)
        start_utc, end_utc = local_day_bounds_utc(today_d, user.timezone)
        rows = await self.sessions.list_sessions_overlapping_utc_window(
            user.id, start_utc, end_utc
        )
        return sum(1 for r in rows if r.source == SessionSource.POMODORO.value)

    async def get_local_date_activity_minutes(
        self, user: User, target_date: date
    ) -> dict[int, float]:
        """
        Return `{activity_id: minutes}` for `target_date` in the user's timezone,
        splitting any sessions that cross midnight (Section 4 Rule 4).
        """
        start_utc, end_utc = local_day_bounds_utc(target_date, user.timezone)
        # Query a padded window (+/- 1 day) to catch sessions crossing midnight
        rows = await self.sessions.list_sessions_overlapping_utc_window(
            user.id,
            start_utc - timedelta(hours=16),
            end_utc + timedelta(hours=16),
        )
        totals: dict[int, float] = {}
        for sess in rows:
            if sess.ended_at is None:
                continue
            slices = split_session_by_local_days(
                sess.started_at,
                sess.ended_at,
                user.timezone,
                session_id=sess.id,
                activity_id=sess.activity_id,
            )
            for sl in slices:
                if sl.local_date == target_date:
                    totals[sl.activity_id] = totals.get(sl.activity_id, 0.0) + sl.minutes
        return totals

    async def add_manual_session(
        self,
        user: User,
        activity_id: int,
        started_at_utc: datetime,
        ended_at_utc: datetime,
        *,
        now_utc: Optional[datetime] = None,
    ) -> Session:
        """
        Validate and insert a manual session (Section 6.3):
        - Reject end before start
        - Reject future end times
        - Reject sessions over 16 hours
        """
        now = now_utc or utc_now()
        if ended_at_utc <= started_at_utc:
            raise ValueError("manual_err_end_before_start")
        if ended_at_utc > now + timedelta(minutes=1):
            raise ValueError("manual_err_future")
        duration_sec = (ended_at_utc - started_at_utc).total_seconds()
        if duration_sec > 16 * 3600:
            raise ValueError("manual_err_over_16h")

        sess = Session(
            user_id=user.id,
            activity_id=activity_id,
            started_at=started_at_utc,
            ended_at=ended_at_utc,
            source=SessionSource.MANUAL.value,
            mode=TimerMode.STOPWATCH.value,
        )
        self.db.add(sess)
        await self.db.flush()

        await self.jobs.record_undo(
            user.id,
            UndoActionType.CREATE_SESSION.value,
            {"session_id": sess.id},
        )
        return await self.sessions.get_session(user.id, sess.id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 4. Scoring, Summary & Streak Service (Section 6.6, 6.7, 6.8)
# ---------------------------------------------------------------------------

class ScoringAndStreakService:
    """Computes daily evaluations, finalizes daily summaries, and updates streaks."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.activities = ActivityRepository(db)
        self.sessions = SessionRepository(db)
        self.plans = PlanRepository(db)
        self.summaries = SummaryAndStreakRepository(db)
        self.timer_svc = TimerService(db)

    async def evaluate_day(
        self, user: User, target_date: date
    ) -> DailyEvaluation:
        existing_summary = await self.summaries.get_daily_summary(user.id, target_date)
        is_rest_day = existing_summary.is_rest_day if existing_summary else False

        all_activities = await self.activities.list_activities(
            user.id, include_archived=True
        )
        totals_by_act = await self.timer_svc.get_local_date_activity_minutes(
            user, target_date
        )

        # Mood average for sessions on target_date
        day_start_utc, day_end_utc = local_day_bounds_utc(target_date, user.timezone)
        day_sessions = await self.sessions.list_sessions_overlapping_utc_window(
            user.id, day_start_utc, day_end_utc
        )
        moods = [s.mood for s in day_sessions if s.mood is not None]
        mood_avg = round(sum(moods) / len(moods), 1) if moods else None

        score_inputs: list[tuple[str, float, Optional[int], int]] = []
        act_stats: list[ActivityDayStat] = []

        for act in all_activities:
            actual_min = totals_by_act.get(act.id, 0.0)
            if act.archived and actual_min <= 0:
                continue

            daily_target = next(
                (g.target_minutes for g in act.goals if g.period == GoalPeriod.DAY.value),
                None,
            )
            weekly_target = next(
                (g.target_minutes for g in act.goals if g.period == GoalPeriod.WEEK.value),
                None,
            )

            ratio = None
            contrib = None
            if daily_target is not None and daily_target > 0:
                ratio = compute_activity_ratio(act.kind, actual_min, daily_target)
                contrib = ratio * act.weight
                score_inputs.append((act.kind, actual_min, daily_target, act.weight))

            act_stats.append(
                ActivityDayStat(
                    activity_id=act.id,
                    name=act.name,
                    emoji=act.emoji,
                    category_name=act.category.name if act.category else "—",
                    kind=act.kind,
                    weight=act.weight,
                    actual_minutes=round(actual_min, 1),
                    daily_target_minutes=daily_target,
                    weekly_target_minutes=weekly_target,
                    ratio=round(ratio, 3) if ratio is not None else None,
                    weighted_contribution=round(contrib, 3) if contrib is not None else None,
                )
            )

        score = None if is_rest_day else compute_daily_score(score_inputs)
        band = get_score_band(score, is_rest_day=is_rest_day)

        # Check planned occurrences for target_date
        occurrences = await self.plans.list_occurrences_for_dates(
            user.id, target_date, target_date
        )
        planned_done: list[str] = []
        planned_missed: list[str] = []
        for occ in occurrences:
            label = occ.plan.title or (occ.plan.activity.name if occ.plan.activity else "Plan")
            act_logged = (
                occ.plan.activity_id is not None
                and totals_by_act.get(occ.plan.activity_id, 0.0) > 0
            )
            if occ.status in (
                PlanOccurrenceStatus.STARTED.value,
                PlanOccurrenceStatus.DONE.value,
            ) or act_logged:
                planned_done.append(label)
            elif occ.status != PlanOccurrenceStatus.SKIPPED.value:
                planned_missed.append(label)

        total_minutes = sum(s.actual_minutes for s in act_stats)
        return DailyEvaluation(
            local_date=target_date,
            is_rest_day=is_rest_day,
            total_minutes=total_minutes,
            score=score,
            band_emoji=band,
            mood_average=mood_avg,
            activities=act_stats,
            planned_done=planned_done,
            planned_missed=planned_missed,
        )

    async def finalize_day_and_update_streaks(
        self, user: User, target_date: date
    ) -> tuple[DailyEvaluation, StreakTransitionResult]:
        eval_res = await self.evaluate_day(user, target_date)
        details = {
            "total_minutes": eval_res.total_minutes,
            "mood_average": eval_res.mood_average,
            "activities": [
                {
                    "activity_id": a.activity_id,
                    "name": a.name,
                    "actual_minutes": a.actual_minutes,
                    "daily_target_minutes": a.daily_target_minutes,
                    "ratio": a.ratio,
                    "weighted_contribution": a.weighted_contribution,
                }
                for a in eval_res.activities
            ],
        }
        await self.summaries.upsert_daily_summary(
            user_id=user.id,
            local_d=target_date,
            score=eval_res.score,
            is_rest_day=eval_res.is_rest_day,
            finalized=True,
            details_json=details,
        )

        # Update per-activity streaks
        for a in eval_res.activities:
            if a.daily_target_minutes is None:
                continue
            act_streak = await self.summaries.get_or_create_streak(
                user.id, a.activity_id
            )
            if eval_res.is_rest_day:
                act_streak.last_counted_date = target_date
                continue
            met = (
                a.actual_minutes <= a.daily_target_minutes
                if a.kind == ActivityKind.LIMIT.value
                else a.actual_minutes >= a.daily_target_minutes
            )
            if met:
                act_streak.current += 1
                act_streak.best = max(act_streak.best, act_streak.current)
            else:
                act_streak.current = 0
            act_streak.last_counted_date = target_date

        # Update overall streak & freeze tokens
        overall = await self.summaries.get_or_create_streak(user.id, None)
        transition = evaluate_overall_streak_day(
            prev_current=overall.current,
            prev_best=overall.best,
            freeze_tokens=user.freeze_tokens,
            score=eval_res.score,
            streak_threshold=user.streak_threshold,
            is_rest_day=eval_res.is_rest_day,
        )
        overall.current = transition.new_current
        overall.best = transition.new_best
        overall.last_counted_date = target_date
        user.freeze_tokens = transition.new_freeze_tokens
        await self.db.flush()
        return eval_res, transition


# ---------------------------------------------------------------------------
# 5. Planner / Calendar Service (Section 6.4b)
# ---------------------------------------------------------------------------

def plan_matches_date(plan: Plan, candidate: date) -> bool:
    """Return True if `plan` occurs on `candidate` according to its repeat_rule."""
    if candidate < plan.date or not plan.active:
        return False
    if plan.repeat_rule == RepeatRule.NONE.value:
        return candidate == plan.date
    if plan.repeat_rule == RepeatRule.DAILY.value:
        return True
    if plan.repeat_rule == RepeatRule.WEEKDAYS.value:
        return candidate.weekday() < 5  # Mon(0)..Fri(4)
    if plan.repeat_rule == RepeatRule.CUSTOM.value:
        return bool(plan.repeat_days & (1 << candidate.weekday()))
    return False


class PlannerService:
    """Generates 14-day plan occurrences and schedules pre-reminders."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.plans = PlanRepository(db)
        self.jobs = JobAndUndoRepository(db)

    async def refresh_occurrences_for_user(
        self, user: User, horizon_days: int = 14
    ) -> int:
        """
        Generate `plan_occurrences` for the next `horizon_days` (default 14) and
        schedule `plan_pre_reminder` jobs where configured (Section 5 & 6.4b).
        """
        today_d = local_today(user.timezone)
        end_d = today_d + timedelta(days=horizon_days)
        active_plans = await self.plans.list_active_plans(user.id)
        existing_occs = await self.plans.list_occurrences_for_dates(
            user.id, today_d, end_d
        )
        existing_keys = {(o.plan_id, o.local_date) for o in existing_occs}

        created_count = 0
        tz = get_zoneinfo(user.timezone)
        now_utc = utc_now()

        for plan in active_plans:
            cur = today_d
            while cur <= end_d:
                if plan_matches_date(plan, cur) and (plan.id, cur) not in existing_keys:
                    occ = PlanOccurrence(
                        plan_id=plan.id,
                        local_date=cur,
                        status=PlanOccurrenceStatus.PENDING.value,
                    )
                    self.db.add(occ)
                    await self.db.flush()
                    created_count += 1

                    if plan.pre_reminder_minutes > 0:
                        start_local = datetime.combine(cur, plan.start_time, tzinfo=tz)
                        remind_utc = (
                            start_local - timedelta(minutes=plan.pre_reminder_minutes)
                        ).astimezone(timezone.utc)
                        if remind_utc > now_utc:
                            await self.jobs.schedule_job(
                                user.id,
                                ScheduledJobKind.PLAN_PRE_REMINDER.value,
                                remind_utc,
                                {"occurrence_id": occ.id, "plan_id": plan.id},
                            )
                cur += timedelta(days=1)

        await self.db.flush()
        return created_count


# ---------------------------------------------------------------------------
# 6. CSV Export Service (Section 6.12)
# ---------------------------------------------------------------------------

class ExportService:
    """Generates UTF-8 with BOM CSV exports in the user's timezone."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.sessions = SessionRepository(db)

    async def generate_csv_bytes(
        self, user: User, start_date: date, end_date: date
    ) -> tuple[bytes, int]:
        """
        Return `(utf8_bom_csv_bytes, row_count)` with columns required by Section 6.12:
        `date, start, end, activity, category, minutes, source, mood, note`.
        Minutes are formatted as integers in CSV per Section 4 Rule 7.
        """
        start_utc, _ = local_day_bounds_utc(start_date, user.timezone)
        _, end_utc = local_day_bounds_utc(end_date, user.timezone)

        rows = await self.sessions.list_sessions_overlapping_utc_window(
            user.id, start_utc, end_utc
        )

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "date",
                "start",
                "end",
                "activity",
                "category",
                "minutes",
                "source",
                "mood",
                "note",
            ]
        )

        count = 0
        for s in rows:
            if s.ended_at is None:
                continue
            st_local = to_local(s.started_at, user.timezone)
            en_local = to_local(s.ended_at, user.timezone)
            mins = int(round((s.ended_at - s.started_at).total_seconds() / 60.0))
            cat_name = (
                s.activity.category.name
                if (s.activity and s.activity.category)
                else ""
            )
            act_name = s.activity.name if s.activity else ""
            writer.writerow(
                [
                    st_local.strftime("%Y-%m-%d"),
                    st_local.strftime("%H:%M"),
                    en_local.strftime("%H:%M"),
                    act_name,
                    cat_name,
                    mins,
                    s.source,
                    s.mood if s.mood is not None else "",
                    s.note or "",
                ]
            )
            count += 1

        # Prepend UTF-8 BOM (\ufeff) so Excel opens Uzbek characters cleanly (Section 6.12)
        csv_text = "\ufeff" + buf.getvalue()
        return csv_text.encode("utf-8"), count


# ---------------------------------------------------------------------------
# 7. Undo Service (Section 6.10)
# ---------------------------------------------------------------------------

class UndoService:
    """Reverts the most recent undoable action within 24 hours."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.jobs = JobAndUndoRepository(db)
        self.sessions = SessionRepository(db)

    async def undo_latest(
        self, user: User, undo_id: Optional[int] = None
    ) -> Optional[str]:
        entry = await self.jobs.pop_latest_undo(user.id, undo_id=undo_id)
        if entry is None:
            return None

        payload = entry.payload_json
        session_id = payload.get("session_id")
        if session_id is None:
            return entry.action_type

        sess = await self.sessions.get_session(user.id, int(session_id))
        if sess is None:
            return entry.action_type

        if entry.action_type in (
            UndoActionType.CREATE_SESSION.value,
            UndoActionType.STOP_TIMER.value,
        ):
            sess.deleted_at = utc_now()
        elif entry.action_type == UndoActionType.DELETE_SESSION.value:
            sess.deleted_at = None
        elif entry.action_type == UndoActionType.EDIT_SESSION.value:
            if "prev_activity_id" in payload:
                sess.activity_id = payload["prev_activity_id"]
            if "prev_started_at" in payload:
                sess.started_at = datetime.fromisoformat(payload["prev_started_at"])
            if "prev_ended_at" in payload and payload["prev_ended_at"]:
                sess.ended_at = datetime.fromisoformat(payload["prev_ended_at"])
            sess.mood = payload.get("prev_mood")
            sess.note = payload.get("prev_note")

        await self.db.flush()
        return entry.action_type