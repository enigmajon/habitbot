"""
DB-driven 30-second scheduler loop (Section 8).

Processes due rows in `scheduled_jobs` (`WHERE run_at <= now AND sent_at IS NULL`),
marks them `sent_at = now` in the same transaction as the send result, respects
Telegram's ~25 messages/sec global limit, retries on `TelegramRetryAfter`, and disables
jobs if a user blocked the bot (`TelegramForbiddenError`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.charts.weekly_charts import (
    render_weekly_stacked_chart,
    render_weekly_targets_chart,
)
from bot.models.models import (
    GoalPeriod,
    PomodoroPhase,
    ScheduledJob,
    ScheduledJobKind,
    SessionSource,
    TimerMode,
    User,
    utc_now,
)
from bot.repositories import (
    ActivityRepository,
    JobAndUndoRepository,
    PlanRepository,
    SessionRepository,
    SummaryAndStreakRepository,
    UserRepository,
)
from bot.services.core_services import (
    PlannerService,
    ScoringAndStreakService,
    TimerService,
    compute_weekly_needed_per_day,
)
from bot.services.time_utils import (
    format_duration,
    get_zoneinfo,
    local_day_bounds_utc,
    local_today,
    local_week_bounds,
    t,
    to_local,
)

logger = logging.getLogger(__name__)


def _next_utc_for_local_time(
    local_t: time, tz_name: str, now_utc: Optional[datetime] = None
) -> datetime:
    now = now_utc or utc_now()
    tz = get_zoneinfo(tz_name)
    now_local = now.astimezone(tz)
    candidate_local = datetime.combine(now_local.date(), local_t, tzinfo=tz)
    if candidate_local <= now_local:
        candidate_local += timedelta(days=1)
    return candidate_local.astimezone(timezone.utc)


async def ensure_daily_jobs_for_user(
    db: AsyncSession, user: User, now_utc: Optional[datetime] = None
) -> None:
    """
    Schedule or reschedule the user's recurring daily/weekly jobs in `scheduled_jobs`
    whenever settings or timezone change (Section 8).
    """
    if user.is_blocked:
        return
    now = now_utc or utc_now()
    jobs = JobAndUndoRepository(db)

    # 1. Morning digest
    if user.morning_digest_enabled and user.morning_digest_time is not None:
        run_at = _next_utc_for_local_time(user.morning_digest_time, user.timezone, now)
        await jobs.schedule_job(
            user.id,
            ScheduledJobKind.MORNING_DIGEST.value,
            run_at,
            {},
            replace_pending_of_same_kind=True,
        )
    else:
        await jobs.cancel_pending_jobs(user.id, ScheduledJobKind.MORNING_DIGEST.value)

    # 2. Daily summary
    sum_run_at = _next_utc_for_local_time(user.summary_time, user.timezone, now)
    await jobs.schedule_job(
        user.id,
        ScheduledJobKind.DAY_SUMMARY.value,
        sum_run_at,
        {},
        replace_pending_of_same_kind=True,
    )

    # 3. Idle nudge
    if user.idle_nudge_enabled:
        nudge_run_at = _next_utc_for_local_time(user.idle_nudge_time, user.timezone, now)
        await jobs.schedule_job(
            user.id,
            ScheduledJobKind.IDLE_NUDGE.value,
            nudge_run_at,
            {},
            replace_pending_of_same_kind=True,
        )
    else:
        await jobs.cancel_pending_jobs(user.id, ScheduledJobKind.IDLE_NUDGE.value)

    # 4. Weekly report (Sunday at summary_time)
    tz = get_zoneinfo(user.timezone)
    now_local = now.astimezone(tz)
    days_until_sunday = (6 - now_local.weekday()) % 7
    sun_date = now_local.date() + timedelta(days=days_until_sunday)
    sun_local = datetime.combine(sun_date, user.summary_time, tzinfo=tz)
    if sun_local <= now_local:
        sun_local += timedelta(days=7)
    await jobs.schedule_job(
        user.id,
        ScheduledJobKind.WEEKLY_REPORT.value,
        sun_local.astimezone(timezone.utc),
        {},
        replace_pending_of_same_kind=True,
    )


class SchedulerRunner:
    """Runs the 30-second DB polling loop and dispatches due reminders and reports."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker,
        interval_seconds: int = 30,
        rate_limit_per_sec: int = 25,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.interval_seconds = interval_seconds
        self.min_delay = 1.0 / max(1, rate_limit_per_sec)
        self._stop_event = asyncio.Event()
        self._in_flight = False

    async def start(self) -> None:
        logger.info("SchedulerRunner started interval=%ss", self.interval_seconds)
        while not self._stop_event.is_set():
            try:
                self._in_flight = True
                await self.run_once()
            except Exception:
                logger.error("Error in SchedulerRunner tick", exc_info=True)
            finally:
                self._in_flight = False

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """Graceful shutdown: signal loop stop and wait for in-flight batch (Section 11)."""
        self._stop_event.set()
        while self._in_flight:
            await asyncio.sleep(0.1)
        logger.info("SchedulerRunner stopped cleanly.")

    async def run_once(self, now_utc: Optional[datetime] = None) -> int:
        now = now_utc or utc_now()
        processed = 0
        async with self.session_factory() as db:
            job_repo = JobAndUndoRepository(db)
            due_jobs = await job_repo.fetch_due_jobs(now, batch_size=50)

            for job in due_jobs:
                user = job.user
                if user is None or user.is_blocked:
                    job.sent_at = now
                    await db.commit()
                    continue

                try:
                    await self._dispatch_job(db, user, job, now)
                    job.sent_at = now
                    await db.commit()
                    processed += 1
                    await asyncio.sleep(self.min_delay)
                except TelegramRetryAfter as retry_exc:
                    await db.rollback()
                    logger.warning("Telegram RetryAfter=%ss", retry_exc.retry_after)
                    await asyncio.sleep(retry_exc.retry_after)
                except TelegramForbiddenError:
                    await db.rollback()
                    user_repo = UserRepository(db)
                    await user_repo.mark_blocked(user.id, True)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.error(
                        "Failed job id=%s kind=%s", job.id, job.kind, exc_info=True
                    )
        return processed

    async def _dispatch_job(
        self, db: AsyncSession, user: User, job: ScheduledJob, now: datetime
    ) -> None:
        kind = job.kind
        if kind == ScheduledJobKind.MORNING_DIGEST.value:
            await self._send_morning_digest(db, user, now)
            await ensure_daily_jobs_for_user(db, user, now)

        elif kind == ScheduledJobKind.DAY_SUMMARY.value:
            today_d = local_today(user.timezone, now)
            scoring = ScoringAndStreakService(db)
            eval_res, _ = await scoring.finalize_day_and_update_streaks(user, today_d)
            score_str = f"{eval_res.score:.0f}%" if eval_res.score is not None else t(user, "today_no_score")
            text = t(
                user,
                "summary_header",
                date=str(today_d),
                total_dur=format_duration(eval_res.total_minutes, user),
                score_str=score_str,
                band=eval_res.band_emoji,
            )
            await self.bot.send_message(user.telegram_id, text, parse_mode="HTML")
            await ensure_daily_jobs_for_user(db, user, now)

        elif kind == ScheduledJobKind.IDLE_NUDGE.value:
            today_d = local_today(user.timezone, now)
            timer_svc = TimerService(db)
            active = await timer_svc.sessions.get_active_timer(user.id)
            totals = await timer_svc.get_local_date_activity_minutes(user, today_d)
            summary = await SummaryAndStreakRepository(db).get_daily_summary(user.id, today_d)
            is_rest = summary.is_rest_day if summary else False

            if active is None and sum(totals.values()) <= 0 and not is_rest:
                b = InlineKeyboardBuilder()
                b.row(
                    InlineKeyboardButton(
                        text=t(user, "idle_btn_start"), callback_data="nav:start"
                    ),
                    InlineKeyboardButton(
                        text=t(user, "idle_btn_rest"), callback_data="tdy:rest"
                    ),
                )
                b.row(
                    InlineKeyboardButton(
                        text=t(user, "idle_btn_not_today"), callback_data="idle:mute"
                    )
                )
                await self.bot.send_message(
                    user.telegram_id,
                    t(user, "idle_nudge_msg"),
                    reply_markup=b.as_markup(),
                )
            await ensure_daily_jobs_for_user(db, user, now)

        elif kind == ScheduledJobKind.PLAN_PRE_REMINDER.value:
            occ_id = job.payload_json.get("occurrence_id")
            if occ_id:
                occ = await PlanRepository(db).get_occurrence(user.id, int(occ_id))
                if occ and not occ.pre_reminder_sent:
                    occ.pre_reminder_sent = True
                    title = occ.plan.title or (
                        occ.plan.activity.name if occ.plan.activity else "Plan"
                    )
                    b = InlineKeyboardBuilder()
                    if occ.plan.activity_id:
                        b.row(
                            InlineKeyboardButton(
                                text=t(user, "plan_btn_start_now"),
                                callback_data=f"pln:start:{occ.id}",
                            )
                        )
                    await self.bot.send_message(
                        user.telegram_id,
                        t(
                            user,
                            "pre_reminder_msg",
                            mins=occ.plan.pre_reminder_minutes,
                            title=title,
                            start_time=occ.plan.start_time.strftime("%H:%M"),
                        ),
                        parse_mode="HTML",
                        reply_markup=b.as_markup() if occ.plan.activity_id else None,
                    )

        elif kind == ScheduledJobKind.LONG_TIMER_CHECK.value:
            await self._handle_long_timer_check(db, user, now)

        elif kind == ScheduledJobKind.POMODORO_PHASE_END.value:
            await self._handle_pomodoro_phase_end(db, user, now)

        elif kind == ScheduledJobKind.WEEKLY_REPORT.value:
            await send_weekly_report(self.bot, db, user, now)
            await ensure_daily_jobs_for_user(db, user, now)

    async def _send_morning_digest(
        self, db: AsyncSession, user: User, now: datetime
    ) -> None:
        today_d = local_today(user.timezone, now)
        yesterday_d = today_d - timedelta(days=1)

        await PlannerService(db).refresh_occurrences_for_user(user, horizon_days=14)
        scoring = ScoringAndStreakService(db)
        y_eval = await scoring.evaluate_day(user, yesterday_d)
        overall_streak = await SummaryAndStreakRepository(db).get_or_create_streak(
            user.id, None
        )

        y_score_str = (
            f"{y_eval.score:.0f}% {y_eval.band_emoji}"
            if y_eval.score is not None
            else "—"
        )
        lines = [
            t(
                user,
                "digest_header",
                date=str(today_d),
                yesterday_score=y_score_str,
                streak=overall_streak.current,
            )
        ]

        occs = await PlanRepository(db).list_occurrences_for_dates(
            user.id, today_d, today_d
        )
        if occs:
            lines.append(t(user, "digest_plans_header"))
            for occ in occs:
                label = occ.plan.title or (
                    occ.plan.activity.name if occ.plan.activity else "Plan"
                )
                st_str = occ.plan.start_time.strftime("%H:%M")
                lines.append(f"• <code>{st_str}</code> — <b>{label}</b>")
        else:
            lines.append(t(user, "digest_no_plans"))

        await self.bot.send_message(
            user.telegram_id, "
".join(lines), parse_mode="HTML"
        )

    async def _handle_long_timer_check(
        self, db: AsyncSession, user: User, now: datetime
    ) -> None:
        sess_repo = SessionRepository(db)
        active = await sess_repo.get_active_timer(user.id)
        if active is None:
            return

        elapsed_sec = (now - active.started_at).total_seconds()
        elapsed_str = format_duration(elapsed_sec, user, is_seconds=True)
        act_name = active.activity.name if active.activity else "Activity"

        b = InlineKeyboardBuilder()
        if elapsed_sec >= 16 * 3600:
            active.flagged_over_cap = True
            b.row(
                InlineKeyboardButton(
                    text=t(user, "long_timer_btn_stop_now"), callback_data="tmr:stop"
                ),
                InlineKeyboardButton(
                    text=t(user, "long_timer_btn_stopped_earlier"),
                    callback_data=f"tmr:earlier:{active.id}",
                ),
            )
            await self.bot.send_message(
                user.telegram_id,
                t(user, "long_timer_hard_cap", activity=act_name),
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            return

        active.last_long_timer_prompt_at = now
        b.row(
            InlineKeyboardButton(
                text=t(user, "long_timer_btn_continue"),
                callback_data=f"tmr:cont:{active.id}",
            ),
            InlineKeyboardButton(
                text=t(user, "long_timer_btn_stop_now"), callback_data="tmr:stop"
            ),
        )
        b.row(
            InlineKeyboardButton(
                text=t(user, "long_timer_btn_stopped_earlier"),
                callback_data=f"tmr:earlier:{active.id}",
            )
        )
        await self.bot.send_message(
            user.telegram_id,
            t(user, "long_timer_ask", activity=act_name, elapsed=elapsed_str),
            parse_mode="HTML",
            reply_markup=b.as_markup(),
        )
        # Re-ask in 1 hour if unanswered (Section 6.4)
        await JobAndUndoRepository(db).schedule_job(
            user.id,
            ScheduledJobKind.LONG_TIMER_CHECK.value,
            now + timedelta(hours=1),
            {"session_id": active.id},
            replace_pending_of_same_kind=True,
        )

    async def _handle_pomodoro_phase_end(
        self, db: AsyncSession, user: User, now: datetime
    ) -> None:
        sess_repo = SessionRepository(db)
        active = await sess_repo.get_active_timer(user.id)
        if active is None or active.mode != TimerMode.POMODORO.value:
            return

        act_name = active.activity.name if active.activity else "Activity"
        b = InlineKeyboardBuilder()
        b.row(
            InlineKeyboardButton(
                text=t(user, "pomo_btn_next"), callback_data=f"pomo:next:{active.id}"
            ),
            InlineKeyboardButton(
                text=t(user, "pomo_btn_skip_break"),
                callback_data=f"pomo:skip:{active.id}",
            ),
            InlineKeyboardButton(
                text=t(user, "pomo_btn_finish"), callback_data="tmr:stop"
            ),
        )
        cycle = max(1, active.cycle_count)
        tomatoes = "🍅" * min(12, cycle)
        break_min = (
            user.pomodoro_long_break_min
            if cycle % 4 == 0
            else user.pomodoro_break_min
        )
        await self.bot.send_message(
            user.telegram_id,
            t(
                user,
                "pomo_work_done",
                work_min=user.pomodoro_work_min,
                cycle=cycle,
                tomatoes=tomatoes,
                break_min=break_min,
            ),
            parse_mode="HTML",
            reply_markup=b.as_markup(),
        )


async def send_weekly_report(
    bot: Bot, db: AsyncSession, user: User, now_utc: Optional[datetime] = None
) -> None:
    """Build and send the weekly summary text and 2 Matplotlib chart photos (Section 6.8)."""
    now = now_utc or utc_now()
    today_d = local_today(user.timezone, now)
    monday, sunday = local_week_bounds(today_d)
    prev_monday = monday - timedelta(days=7)

    timer_svc = TimerService(db)
    scoring_svc = ScoringAndStreakService(db)
    acts = await ActivityRepository(db).list_activities(user.id, include_archived=True)
    act_map = {a.id: a for a in acts}

    days = [monday + timedelta(days=i) for i in range(7)]
    daily_totals: dict[date, float] = {}
    act_daily_mins: dict[str, list[float]] = {}
    act_week_totals: dict[int, float] = {}
    scores: list[float] = []

    for idx, d in enumerate(days):
        by_act = await timer_svc.get_local_date_activity_minutes(user, d)
        daily_totals[d] = sum(by_act.values())
        for aid, mins in by_act.items():
            act_obj = act_map.get(aid)
            name = act_obj.name if act_obj else f"#{aid}"
            if name not in act_daily_mins:
                act_daily_mins[name] = [0.0] * 7
            act_daily_mins[name][idx] += mins
            act_week_totals[aid] = act_week_totals.get(aid, 0.0) + mins

        if d <= today_d:
            ev = await scoring_svc.evaluate_day(user, d)
            if ev.score is not None:
                scores.append(ev.score)

    prev_week_total = 0.0
    for i in range(7):
        d_prev = prev_monday + timedelta(days=i)
        by_act_prev = await timer_svc.get_local_date_activity_minutes(user, d_prev)
        prev_week_total += sum(by_act_prev.values())

    this_week_total = sum(daily_totals.values())
    diff_mins = this_week_total - prev_week_total
    sign = "+" if diff_mins >= 0 else "-"
    diff_str = f"{sign}{format_duration(abs(diff_mins), user)}"

    best_d = max(daily_totals.items(), key=lambda kv: kv[1])[0]
    weak_d = min(daily_totals.items(), key=lambda kv: kv[1])[0]
    avg_score_str = f"{(sum(scores) / len(scores)):.0f}%" if scores else "—"

    overall_streak = await SummaryAndStreakRepository(db).get_or_create_streak(
        user.id, None
    )

    text = t(
        user,
        "week_report_header",
        start_date=str(monday),
        end_date=str(sunday),
        total_dur=format_duration(this_week_total, user),
        diff_str=diff_str,
        avg_score=avg_score_str,
        best_day=f"{best_d} ({format_duration(daily_totals[best_d], user)})",
        weakest_day=f"{weak_d} ({format_duration(daily_totals[weak_d], user)})",
        streak=overall_streak.current,
        tokens=user.freeze_tokens,
        mood_section="",
    )

    await bot.send_message(user.telegram_id, text, parse_mode="HTML")

    # Render & send chart 1: Stacked daily bar chart
    png1 = render_weekly_stacked_chart(days, act_daily_mins, lang=user.language)
    await bot.send_photo(
        user.telegram_id,
        BufferedInputFile(png1, filename="weekly_daily_stacked.png"),
    )

    # Render & send chart 2: Weekly totals vs weekly targets
    target_items: list[tuple[str, float, float]] = []
    for a in acts:
        wk_goal = next(
            (g.target_minutes for g in a.goals if g.period == GoalPeriod.WEEK.value),
            None,
        )
        actual_m = act_week_totals.get(a.id, 0.0)
        if wk_goal or actual_m > 0:
            target_items.append((a.name, actual_m, float(wk_goal or 0)))

    png2 = render_weekly_targets_chart(target_items, lang=user.language)
    await bot.send_photo(
        user.telegram_id,
        BufferedInputFile(png2, filename="weekly_targets.png"),
    )
