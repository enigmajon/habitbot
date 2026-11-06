"""
Telegram command and callback query handlers (Section 6 & Section 7).

Follows Section 1 & Section 7 UI rules:
- Handlers only parse input, call services, and render localized output via `t(user, key)`.
- Navigational callbacks edit the existing message (`safe_edit_or_send`) and fall back
  to sending a fresh message if the message is too old or deleted (Section 9).
- Every callback is answered immediately (`await cb.answer()`) for idempotency (Rule 6).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.keyboards import (
    kb_activity_list_for_timer,
    kb_back_cancel_menu,
    kb_calendar_month,
    kb_language_select,
    kb_main_menu,
    kb_mood_prompt,
    kb_note_prompt,
    kb_running_timer_controls,
    kb_starter_suggestions,
    kb_switch_confirm,
    kb_timezone_confirm,
)
from bot.models.models import (
    ActivityKind,
    GoalPeriod,
    Plan,
    PlanOccurrenceStatus,
    RepeatRule,
    TimerMode,
    User,
    utc_now,
)
from bot.repositories import (
    ActivityRepository,
    PlanRepository,
    SessionRepository,
    SummaryAndStreakRepository,
    UserRepository,
)
from bot.scheduler.runner import ensure_daily_jobs_for_user, send_weekly_report
from bot.services.core_services import (
    ExportService,
    PlannerService,
    ScoringAndStreakService,
    TimerService,
    UndoService,
)
from bot.services.time_utils import (
    format_duration,
    format_progress_bar,
    get_zoneinfo,
    local_day_bounds_utc,
    local_today,
    local_week_bounds,
    parse_duration_minutes,
    t,
    to_local,
)

router = Router(name="main_router")


class BotStates(StatesGroup):
    waiting_timezone = State()
    waiting_act_name = State()
    waiting_act_emoji = State()
    waiting_act_daily_goal = State()
    waiting_act_weekly_goal = State()
    waiting_session_note = State()
    waiting_manual_duration = State()
    waiting_manual_range = State()
    waiting_plan_title = State()
    waiting_plan_time = State()
    waiting_plan_duration = State()


async def safe_edit_or_send(
    target: Message | CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """
    Edit existing message when navigating; fall back to sending a fresh message if too
    old or deleted (Section 7 & Section 9).
    """
    if isinstance(target, CallbackQuery):
        try:
            await target.answer()
        except Exception:
            pass
        if target.message:
            try:
                await target.message.edit_text(
                    text, parse_mode="HTML", reply_markup=reply_markup
                )
                return
            except TelegramBadRequest:
                await target.message.answer(
                    text, parse_mode="HTML", reply_markup=reply_markup
                )
                return
    elif isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=reply_markup)


async def render_main_menu(
    target: Message | CallbackQuery, db: AsyncSession, user: User
) -> None:
    active = await SessionRepository(db).get_active_timer(user.id)
    if active and active.activity:
        elapsed_sec = (utc_now() - active.started_at).total_seconds()
        status_line = t(
            user,
            "menu_status_running",
            emoji=active.activity.emoji,
            activity=active.activity.name,
            elapsed=format_duration(elapsed_sec, user, is_seconds=True),
        )
    else:
        status_line = t(user, "menu_status_idle")

    text = t(user, "menu_title", tz=user.timezone, status_line=status_line)
    await safe_edit_or_send(
        target, text, reply_markup=kb_main_menu(user, active is not None)
    )


# ---------------------------------------------------------------------------
# 1. Onboarding & Commands (/start, /menu, /help)
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(
    message: Message, db: AsyncSession, user: User, state: FSMContext
) -> None:
    await state.clear()
    await ensure_daily_jobs_for_user(db, user)
    await message.answer(
        t(user, "onboard_welcome"), reply_markup=kb_language_select()
    )


@router.callback_query(F.data.startswith("onb:lang:"))
async def cb_onboard_lang(
    cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext
) -> None:
    lang = cb.data.split(":")[-1]
    user.language = "en" if lang == "en" else "uz"
    await safe_edit_or_send(
        cb,
        t(user, "onboard_tz_confirm", tz=user.timezone),
        reply_markup=kb_timezone_confirm(user, user.timezone),
    )


@router.callback_query(F.data == "onb:tz:keep")
async def cb_onboard_tz_keep(
    cb: CallbackQuery, user: User, state: FSMContext
) -> None:
    await state.update_data(selected_starters=["study", "reading", "exercise"])
    await safe_edit_or_send(
        cb,
        t(user, "onboard_suggestions_title"),
        reply_markup=kb_starter_suggestions(
            user, {"study", "reading", "exercise"}
        ),
    )


@router.callback_query(F.data == "onb:tz:change")
async def cb_onboard_tz_change(
    cb: CallbackQuery, user: User, state: FSMContext
) -> None:
    await state.set_state(BotStates.waiting_timezone)
    await safe_edit_or_send(
        cb,
        t(user, "onboard_tz_prompt"),
        reply_markup=kb_back_cancel_menu(user),
    )


@router.message(BotStates.waiting_timezone)
async def msg_timezone_input(
    message: Message, db: AsyncSession, user: User, state: FSMContext
) -> None:
    raw_tz = (message.text or "").strip()
    try:
        ZoneInfo(raw_tz)
    except ZoneInfoNotFoundError:
        await message.answer(t(user, "onboard_tz_invalid"), parse_mode="HTML")
        return

    user.timezone = raw_tz
    await ensure_daily_jobs_for_user(db, user)
    await state.clear()
    if not user.onboarded:
        await state.update_data(selected_starters=["study", "reading", "exercise"])
        await message.answer(
            t(user, "onboard_suggestions_title"),
            parse_mode="HTML",
            reply_markup=kb_starter_suggestions(
                user, {"study", "reading", "exercise"}
            ),
        )
    else:
        await render_main_menu(message, db, user)


@router.callback_query(F.data.startswith("onb:tog:"))
async def cb_onboard_toggle_starter(
    cb: CallbackQuery, user: User, state: FSMContext
) -> None:
    code = cb.data.split(":")[-1]
    data = await state.get_data()
    selected = set(data.get("selected_starters", ["study", "reading", "exercise"]))
    if code in selected:
        selected.remove(code)
    else:
        selected.add(code)
    await state.update_data(selected_starters=list(selected))
    await safe_edit_or_send(
        cb,
        t(user, "onboard_suggestions_title"),
        reply_markup=kb_starter_suggestions(user, selected),
    )


@router.callback_query(F.data == "onb:done")
async def cb_onboard_done(
    cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext
) -> None:
    data = await state.get_data()
    selected = set(data.get("selected_starters", []))
    act_repo = ActivityRepository(db)
    existing = await act_repo.list_activities(user.id, include_archived=True)
    if not existing and selected:
        cat = await act_repo.get_or_create_category(
            user.id, t(user, "cat_default_general"), "📁"
        )
        presets = {
            "study": ("📚", t(user, "suggest_study").replace("📚 ", ""), "build", 2, 120, 600),
            "reading": ("📖", t(user, "suggest_reading").replace("📖 ", ""), "build", 1, 30, 210),
            "exercise": ("🏃", t(user, "suggest_exercise").replace("🏃 ", ""), "build", 2, 45, 240),
            "coding": ("💻", t(user, "suggest_coding").replace("💻 ", ""), "build", 3, 120, 600),
            "sleep": ("😴", t(user, "suggest_sleep").replace("😴 ", ""), "build", 1, 480, None),
            "social": ("📱", "Social Media", "limit", 1, 60, 420),
        }
        for code in selected:
            if code in presets:
                em, nm, knd, w, dg, wg = presets[code]
                await act_repo.create_activity(
                    user.id,
                    name=nm,
                    emoji=em,
                    category_id=cat.id,
                    kind=knd,
                    weight=w,
                    daily_goal_minutes=dg,
                    weekly_goal_minutes=wg,
                )
    user.onboarded = True
    await state.clear()
    await render_main_menu(cb, db, user)


@router.message(Command("menu"))
@router.callback_query(F.data == "nav:menu")
async def nav_main_menu(
    event: Message | CallbackQuery,
    db: AsyncSession,
    user: User,
    state: FSMContext,
) -> None:
    await state.clear()
    await render_main_menu(event, db, user)


@router.message(Command("help"))
async def cmd_help(message: Message, user: User) -> None:
    await message.answer(
        t(user, "help_text"),
        parse_mode="HTML",
        reply_markup=kb_back_cancel_menu(user),
    )


# ---------------------------------------------------------------------------
# 2. Timer & Stopwatch / Pomodoro (/status, ▶️ Start, ⏹ Stop)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "nav:start")
async def cb_nav_start_timer(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    acts = await ActivityRepository(db).list_activities(user.id)
    if not acts:
        await safe_edit_or_send(
            cb,
            t(user, "timer_no_activities"),
            reply_markup=kb_back_cancel_menu(user),
        )
        return
    await safe_edit_or_send(
        cb,
        t(user, "timer_choose_activity"),
        reply_markup=kb_activity_list_for_timer(user, acts, prefix="tmr:go"),
    )


@router.callback_query(F.data.startswith("tmr:go:"))
async def cb_start_stopwatch(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    act_id = int(cb.data.split(":")[-1])
    timer_svc = TimerService(db)
    active = await timer_svc.sessions.get_active_timer(user.id)
    target_act = await ActivityRepository(db).get_activity(user.id, act_id)
    if target_act is None:
        return

    if active is not None:
        if active.activity_id == act_id:
            await cb_show_status(cb, db, user)
            return
        old_name = active.activity.name if active.activity else "Timer"
        await safe_edit_or_send(
            cb,
            t(user, "timer_switch_confirm", old_act=old_name, new_act=target_act.name),
            reply_markup=kb_switch_confirm(user, act_id),
        )
        return

    sess = await timer_svc.start_timer(
        user, act_id, mode=TimerMode.STOPWATCH.value
    )
    st_local = to_local(sess.started_at, user.timezone).strftime("%H:%M")
    await safe_edit_or_send(
        cb,
        t(
            user,
            "timer_started",
            emoji=target_act.emoji,
            activity=target_act.name,
            start_time=st_local,
        ),
        reply_markup=kb_running_timer_controls(user, act_id),
    )


@router.callback_query(F.data.startswith("tmr:swyes:"))
async def cb_confirm_switch_timer(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    new_act_id = int(cb.data.split(":")[-1])
    timer_svc = TimerService(db)
    await timer_svc.stop_active_timer(user)
    sess = await timer_svc.start_timer(
        user, new_act_id, mode=TimerMode.STOPWATCH.value
    )
    st_local = to_local(sess.started_at, user.timezone).strftime("%H:%M")
    await safe_edit_or_send(
        cb,
        t(
            user,
            "timer_started",
            emoji=sess.activity.emoji,
            activity=sess.activity.name,
            start_time=st_local,
        ),
        reply_markup=kb_running_timer_controls(user, new_act_id),
    )


@router.callback_query(F.data.startswith("tmr:pomo:"))
@router.callback_query(F.data.startswith("tmr:topomo:"))
async def cb_start_pomodoro(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    act_id = int(cb.data.split(":")[-1])
    timer_svc = TimerService(db)
    active = await timer_svc.sessions.get_active_timer(user.id)
    if active is not None:
        await timer_svc.stop_active_timer(user)
    sess = await timer_svc.start_timer(
        user, act_id, mode=TimerMode.POMODORO.value
    )
    tomatoes = "🍅" * max(1, sess.cycle_count)
    await safe_edit_or_send(
        cb,
        t(
            user,
            "pomo_started",
            emoji=sess.activity.emoji,
            activity=sess.activity.name,
            work_min=user.pomodoro_work_min,
            cycle=sess.cycle_count,
            tomatoes=tomatoes,
        ),
        reply_markup=kb_running_timer_controls(user, act_id),
    )


@router.callback_query(F.data == "tmr:stop")
async def cb_stop_timer(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    timer_svc = TimerService(db)
    outcome = await timer_svc.stop_active_timer(user)
    if outcome is None:
        await safe_edit_or_send(
            cb, t(user, "status_none"), reply_markup=kb_main_menu(user, False)
        )
        return

    if outcome.discarded_under_30s:
        await safe_edit_or_send(
            cb,
            t(user, "timer_discarded_short", seconds=outcome.duration_seconds),
            reply_markup=kb_main_menu(user, False),
        )
        return

    sess_dur = format_duration(outcome.duration_seconds, user, is_seconds=True)
    today_dur = format_duration(outcome.today_activity_minutes, user)
    act_name = outcome.session.activity.name if outcome.session.activity else "Activity"

    if outcome.daily_goal_minutes:
        goal_dur = format_duration(outcome.daily_goal_minutes, user)
        pct = int(
            round((outcome.today_activity_minutes / outcome.daily_goal_minutes) * 100)
        )
        msg = t(
            user,
            "timer_stopped_with_goal",
            activity=act_name,
            session_dur=sess_dur,
            today_dur=today_dur,
            goal_dur=goal_dur,
            pct=pct,
        )
    else:
        msg = t(
            user,
            "timer_stopped_no_goal",
            activity=act_name,
            session_dur=sess_dur,
            today_dur=today_dur,
        )

        if user.mood_prompt_enabled:
            msg += "\n\n" + t(user, "mood_prompt")
            await safe_edit_or_send(
                cb, msg, reply_markup=kb_mood_prompt(user, outcome.session.id)
            )
        else:
            await safe_edit_or_send(
                cb, msg, reply_markup=kb_note_prompt(user, outcome.session.id)
            )


@router.message(Command("status"))
@router.callback_query(F.data == "nav:status")
async def cb_show_status(
    event: Message | CallbackQuery, db: AsyncSession, user: User
) -> None:
    timer_svc = TimerService(db)
    active = await timer_svc.sessions.get_active_timer(user.id)
    if active is None or active.activity is None:
        await safe_edit_or_send(
            event, t(user, "status_none"), reply_markup=kb_main_menu(user, False)
        )
        return

    now = utc_now()
    elapsed_sec = (now - active.started_at).total_seconds()
    today_d = local_today(user.timezone, now)
    totals = await timer_svc.get_local_date_activity_minutes(user, today_d)
    today_mins = totals.get(active.activity_id, 0.0) + (elapsed_sec / 60.0)

    text = t(
        user,
        "status_running",
        emoji=active.activity.emoji,
        activity=active.activity.name,
        mode=active.mode,
        start_time=to_local(active.started_at, user.timezone).strftime("%H:%M"),
        elapsed=format_duration(elapsed_sec, user, is_seconds=True),
        today_total=format_duration(today_mins, user),
    )
    await safe_edit_or_send(
        event,
        text,
        reply_markup=kb_running_timer_controls(user, active.activity_id),
    )


# ---------------------------------------------------------------------------
# 3. Mood & Session Note Callbacks (Section 6.2 & 6.9)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mood:set:"))
async def cb_set_mood(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    parts = cb.data.split(":")
    session_id, mood_val = int(parts[2]), int(parts[3])
    sess = await SessionRepository(db).get_session(user.id, session_id)
    if sess:
        sess.mood = max(1, min(5, mood_val))
    await safe_edit_or_send(
        cb,
        t(user, "mood_saved_note_prompt", mood=mood_val),
        reply_markup=kb_note_prompt(user, session_id),
    )


@router.callback_query(F.data.startswith("mood:skip:"))
async def cb_skip_mood(
    cb: CallbackQuery, user: User
) -> None:
    session_id = int(cb.data.split(":")[-1])
    await safe_edit_or_send(
        cb,
        t(user, "mood_saved_note_prompt", mood="—"),
        reply_markup=kb_note_prompt(user, session_id),
    )


@router.callback_query(F.data.startswith("note:add:"))
async def cb_add_note_prompt(
    cb: CallbackQuery, user: User, state: FSMContext
) -> None:
    session_id = int(cb.data.split(":")[-1])
    await state.set_state(BotStates.waiting_session_note)
    await state.update_data(note_session_id=session_id)
    await safe_edit_or_send(
        cb,
        t(user, "note_prompt_send"),
        reply_markup=kb_back_cancel_menu(user),
    )


@router.message(BotStates.waiting_session_note)
async def msg_save_session_note(
    message: Message, db: AsyncSession, user: User, state: FSMContext
) -> None:
    raw_note = (message.text or "").strip()
    if len(raw_note) > 500:
        await message.answer(t(user, "note_too_long"))
        return
    data = await state.get_data()
    session_id = data.get("note_session_id")
    if session_id:
        sess = await SessionRepository(db).get_session(user.id, int(session_id))
        if sess:
            sess.note = raw_note
    await state.clear()
    await message.answer(t(user, "note_saved"))
    await render_main_menu(message, db, user)


# ---------------------------------------------------------------------------
# 4. Today Screen, Daily Rating & Rest Day Toggle (/today, 📊 Today)
# ---------------------------------------------------------------------------

@router.message(Command("today"))
@router.callback_query(F.data == "nav:today")
async def nav_today(
    event: Message | CallbackQuery, db: AsyncSession, user: User
) -> None:
    today_d = local_today(user.timezone)
    scoring = ScoringAndStreakService(db)
    ev = await scoring.evaluate_day(user, today_d)

    score_str = (
        f"{ev.score:.0f}% {ev.band_emoji}"
        if ev.score is not None
        else t(user, "today_no_score")
    )
    rest_badge = t(user, "today_rest_badge") if ev.is_rest_day else ""
    lines = [
        t(
            user,
            "today_header",
            date=str(today_d),
            rest_badge=rest_badge,
            total_dur=format_duration(ev.total_minutes, user),
            score_str=score_str,
        ),
        "",
    ]

    for a in ev.activities:
        dur_str = format_duration(a.actual_minutes, user)
        if a.daily_target_minutes:
            bar = format_progress_bar(
                a.actual_minutes,
                a.daily_target_minutes,
                is_limit=(a.kind == ActivityKind.LIMIT.value),
            )
            tgt_str = format_duration(a.daily_target_minutes, user)
            lines.append(f"{a.emoji} <b>{a.name}</b>: {dur_str} / {tgt_str}
<code>{bar}</code>")
        elif a.actual_minutes > 0:
            lines.append(f"{a.emoji} <b>{a.name}</b>: {dur_str}")

    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "today_btn_close_day"), callback_data="tdy:close"
        ),
        InlineKeyboardButton(
            text=t(user, "today_btn_toggle_rest"), callback_data="tdy:rest"
        ),
    )
    b.row(InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu"))
    await safe_edit_or_send(event, "
".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "tdy:rest")
async def cb_toggle_rest_day(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    today_d = local_today(user.timezone)
    sum_repo = SummaryAndStreakRepository(db)
    existing = await sum_repo.get_daily_summary(user.id, today_d)
    new_rest = not (existing.is_rest_day if existing else False)
    await sum_repo.upsert_daily_summary(
        user.id,
        today_d,
        score=None if new_rest else (existing.score if existing else None),
        is_rest_day=new_rest,
        finalized=existing.finalized if existing else False,
        details_json=existing.details_json if existing else {},
    )
    await nav_today(cb, db, user)


@router.callback_query(F.data == "tdy:close")
async def cb_close_day_now(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    today_d = local_today(user.timezone)
    scoring = ScoringAndStreakService(db)
    ev, _ = await scoring.finalize_day_and_update_streaks(user, today_d)
    score_str = f"{ev.score:.0f}%" if ev.score is not None else t(user, "today_no_score")
    text = t(
        user,
        "summary_header",
        date=str(today_d),
        total_dur=format_duration(ev.total_minutes, user),
        score_str=score_str,
        band=ev.band_emoji,
    )
    await safe_edit_or_send(
        cb, text, reply_markup=kb_back_cancel_menu(user, back_cb="nav:today")
    )


# ---------------------------------------------------------------------------
# 5. Stats, Streaks & Weekly Report (/week, 📈 Stats)
# ---------------------------------------------------------------------------

@router.message(Command("week"))
async def cmd_week_report(
    message: Message, db: AsyncSession, user: User
) -> None:
    await send_weekly_report(message.bot, db, user)  # type: ignore[arg-type]


@router.callback_query(F.data == "nav:stats")
async def cb_nav_stats(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    overall = await SummaryAndStreakRepository(db).get_or_create_streak(
        user.id, None
    )
    text = t(
        user,
        "stats_header",
        cur_streak=overall.current,
        best_streak=overall.best,
        tokens=user.freeze_tokens,
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "stats_btn_week_report"), callback_data="sts:week"
        )
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "stats_btn_use_freeze"), callback_data="tdy:rest"
        )
    )
    b.row(InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu"))
    await safe_edit_or_send(cb, text, reply_markup=b.as_markup())


@router.callback_query(F.data == "sts:week")
async def cb_stats_week_report(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    await cb.answer()
    if cb.bot:
        await send_weekly_report(cb.bot, db, user)


# ---------------------------------------------------------------------------
# 6. Undo, Export, Activities, Planner, History & Settings
# ---------------------------------------------------------------------------

@router.message(Command("undo"))
@router.callback_query(F.data == "undo:last")
async def handle_undo(
    event: Message | CallbackQuery, db: AsyncSession, user: User
) -> None:
    action = await UndoService(db).undo_latest(user)
    msg = (
        t(user, "undo_success", action=action)
        if action
        else t(user, "undo_nothing")
    )
    await safe_edit_or_send(
        event, msg, reply_markup=kb_main_menu(user, False)
    )


@router.message(Command("export"))
@router.callback_query(F.data == "nav:export")
async def handle_export_menu(
    event: Message | CallbackQuery, user: User
) -> None:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "export_btn_this_week"), callback_data="exp:week"
        ),
        InlineKeyboardButton(
            text=t(user, "export_btn_this_month"), callback_data="exp:month"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "export_btn_last_month"), callback_data="exp:lmonth"
        ),
        InlineKeyboardButton(
            text=t(user, "export_btn_all_time"), callback_data="exp:all"
        ),
    )
    b.row(InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu"))
    await safe_edit_or_send(
        event, t(user, "export_prompt"), reply_markup=b.as_markup()
    )


@router.callback_query(F.data.startswith("exp:"))
async def cb_run_export(
    cb: CallbackQuery, db: AsyncSession, user: User
) -> None:
    mode = cb.data.split(":")[-1]
    today_d = local_today(user.timezone)
    if mode == "week":
        start_d, end_d = local_week_bounds(today_d)
    elif mode == "month":
        start_d, end_d = today_d.replace(day=1), today_d
    elif mode == "lmonth":
        first_this = today_d.replace(day=1)
        end_d = first_this - timedelta(days=1)
        start_d = end_d.replace(day=1)
    else:
        start_d, end_d = date(2020, 1, 1), today_d

    csv_bytes, count = await ExportService(db).generate_csv_bytes(
        user, start_d, end_d
    )
    await cb.answer()
    if cb.message:
        await cb.message.answer_document(
            BufferedInputFile(csv_bytes, filename=f"habits_{start_d}_{end_d}.csv"),
            caption=t(user, "export_caption", count=count, period=f"{start_d}..{end_d}"),
        )


@router.message(Command("settings"))
@router.callback_query(F.data == "nav:set")
async def nav_settings(
    event: Message | CallbackQuery, user: User
) -> None:
    digest_str = (
        user.morning_digest_time.strftime("%H:%M")
        if user.morning_digest_enabled and user.morning_digest_time
        else "OFF"
    )
    nudge_str = (
        user.idle_nudge_time.strftime("%H:%M")
        if user.idle_nudge_enabled
        else "OFF"
    )
    text = t(
        user,
        "settings_header",
        lang=user.language.upper(),
        tz=user.timezone,
        digest=digest_str,
        summary=user.summary_time.strftime("%H:%M"),
        nudge=nudge_str,
        long_timer=user.long_timer_threshold_minutes,
        mood="ON" if user.mood_prompt_enabled else "OFF",
        streak_thr=user.streak_threshold,
        pomo_w=user.pomodoro_work_min,
        pomo_b=user.pomodoro_break_min,
        pomo_lb=user.pomodoro_long_break_min,
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "settings_btn_lang"), callback_data="set:lang"
        ),
        InlineKeyboardButton(
            text=t(user, "settings_btn_tz"), callback_data="onb:tz:change"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "settings_btn_mood"), callback_data="set:mood"
        ),
        InlineKeyboardButton(
            text=t(user, "settings_btn_rest_day"), callback_data="tdy:rest"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "settings_btn_delete_data"), callback_data="set:del1"
        )
    )
    b.row(InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu"))
    await safe_edit_or_send(event, text, reply_markup=b.as_markup())


@router.callback_query(F.data == "set:lang")
async def cb_toggle_lang(
    cb: CallbackQuery, user: User
) -> None:
    user.language = "en" if user.language == "uz" else "uz"
    await nav_settings(cb, user)


@router.callback_query(F.data == "set:mood")
async def cb_toggle_mood(
    cb: CallbackQuery, user: User
) -> None:
    user.mood_prompt_enabled = not user.mood_prompt_enabled
    await nav_settings(cb, user)


@router.callback_query(F.data == "set:del1")
async def cb_delete_data_step1(
    cb: CallbackQuery, user: User
) -> None:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "delete_btn_step1"), callback_data="set:del2"
        )
    )
    b.row(
        InlineKeyboardButton(text=t(user, "btn_cancel"), callback_data="nav:set")
    )
    await safe_edit_or_send(
        cb, t(user, "delete_confirm_step1"), reply_markup=b.as_markup()
    )


@router.callback_query(F.data == "set:del2")
async def cb_delete_data_step2(
    cb: CallbackQuery, user: User
) -> None:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "delete_btn_step2"), callback_data="set:del_exec"
        )
    )
    b.row(
        InlineKeyboardButton(text=t(user, "btn_cancel"), callback_data="nav:set")
    )
    await safe_edit_or_send(
        cb, t(user, "delete_confirm_step2"), reply_markup=b.as_markup()
    )


@router.callback_query(F.data == "set:del_exec")
async def cb_delete_data_execute(
    cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext
) -> None:
    lang = user.language
    await state.clear()
    await UserRepository(db).delete_user_data(user.id)
    await safe_edit_or_send(cb, t(lang, "delete_done"))
