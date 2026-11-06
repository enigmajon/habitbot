"""
Inline keyboard builders for the Telegram Habit & Time Tracker Bot.

Enforces Section 4 Rule 6 & Section 7:
- Compact callback data (`action:id`) strictly under 64 bytes.
- Every screen includes a 🏠 Menu button (`nav:menu`), and multi-step flows include
  ⬅️ Back and ✖️ Cancel.
- Inline month-grid calendar picker with ◀ and ▶ navigation (Section 6.4b).
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models.models import Activity, Category, User
from bot.services.time_utils import t


def kb_language_select() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="onb:lang:uz"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="onb:lang:en"),
    )
    return b.as_markup()


def kb_timezone_confirm(user: Any, tz_name: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "onboard_tz_keep", tz=tz_name),
            callback_data="onb:tz:keep",
        )
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "onboard_tz_change"),
            callback_data="onb:tz:change",
        )
    )
    return b.as_markup()


def kb_starter_suggestions(
    user: Any, selected_keys: set[str]
) -> InlineKeyboardMarkup:
    items = [
        ("study", "suggest_study"),
        ("reading", "suggest_reading"),
        ("exercise", "suggest_exercise"),
        ("coding", "suggest_coding"),
        ("sleep", "suggest_sleep"),
        ("social", "suggest_social"),
    ]
    b = InlineKeyboardBuilder()
    for code, i18n_key in items:
        mark = "✅ " if code in selected_keys else "⬜ "
        b.row(
            InlineKeyboardButton(
                text=f"{mark}{t(user, i18n_key)}",
                callback_data=f"onb:tog:{code}",
            )
        )
    b.row(
        InlineKeyboardButton(
            text=t(user, "onboard_btn_done"),
            callback_data="onb:done",
        )
    )
    return b.as_markup()


def kb_main_menu(user: Any, has_active_timer: bool) -> InlineKeyboardMarkup:
    """Main menu keyboard from Section 7."""
    b = InlineKeyboardBuilder()
    stop_or_status = (
        InlineKeyboardButton(
            text=t(user, "menu_btn_stop"), callback_data="tmr:stop"
        )
        if has_active_timer
        else InlineKeyboardButton(
            text=t(user, "menu_btn_status"), callback_data="nav:status"
        )
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "menu_btn_start"), callback_data="nav:start"
        ),
        stop_or_status,
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "menu_btn_add_time"), callback_data="nav:manual"
        ),
        InlineKeyboardButton(
            text=t(user, "menu_btn_today"), callback_data="nav:today"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "menu_btn_plan"), callback_data="nav:plan"
        ),
        InlineKeyboardButton(
            text=t(user, "menu_btn_stats"), callback_data="nav:stats"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "menu_btn_activities"), callback_data="nav:acts"
        ),
        InlineKeyboardButton(
            text=t(user, "menu_btn_history"), callback_data="nav:hist"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text=t(user, "menu_btn_settings"), callback_data="nav:set"
        )
    )
    return b.as_markup()


def kb_back_cancel_menu(
    user: Any, back_cb: str = "nav:menu"
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=t(user, "btn_back"), callback_data=back_cb),
        InlineKeyboardButton(text=t(user, "btn_cancel"), callback_data="nav:menu"),
        InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu"),
    )
    return b.as_markup()


def kb_activity_list_for_timer(
    user: Any, activities: Sequence[Activity], prefix: str = "tmr:go"
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for act in activities:
        b.row(
            InlineKeyboardButton(
                text=f"▶️ {act.emoji} {act.name}",
                callback_data=f"{prefix}:{act.id}",
            ),
            InlineKeyboardButton(
                text="🍅",
                callback_data=f"tmr:pomo:{act.id}",
            ),
        )
    b.row(
        InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu")
    )
    return b.as_markup()


def kb_running_timer_controls(
    user: Any, activity_id: int
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "timer_btn_stop"), callback_data="tmr:stop"
        ),
        InlineKeyboardButton(
            text=t(user, "timer_btn_switch"), callback_data="nav:start"
        ),
        InlineKeyboardButton(
            text=t(user, "timer_btn_pomodoro"),
            callback_data=f"tmr:topomo:{activity_id}",
        ),
    )
    b.row(
        InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu")
    )
    return b.as_markup()


def kb_switch_confirm(user: Any, new_activity_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "btn_yes"), callback_data=f"tmr:swyes:{new_activity_id}"
        ),
        InlineKeyboardButton(
            text=t(user, "btn_cancel"), callback_data="nav:menu"
        ),
    )
    return b.as_markup()


def kb_mood_prompt(
    user: Any, session_id: int, include_undo: bool = True
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        *[
            InlineKeyboardButton(
                text=f"{i}", callback_data=f"mood:set:{session_id}:{i}"
            )
            for i in range(1, 6)
        ]
    )
    row_btns = [
        InlineKeyboardButton(
            text=t(user, "btn_skip"), callback_data=f"mood:skip:{session_id}"
        )
    ]
    if include_undo:
        row_btns.append(
            InlineKeyboardButton(
                text=t(user, "btn_undo"), callback_data="undo:last"
            )
        )
    row_btns.append(
        InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu")
    )
    b.row(*row_btns)
    return b.as_markup()


def kb_note_prompt(user: Any, session_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t(user, "note_btn_add"), callback_data=f"note:add:{session_id}"
        ),
        InlineKeyboardButton(
            text=t(user, "btn_skip"), callback_data="nav:menu"
        ),
    )
    b.row(
        InlineKeyboardButton(text=t(user, "btn_undo"), callback_data="undo:last"),
        InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu"),
    )
    return b.as_markup()


def kb_calendar_month(
    user: Any, year: int, month: int, prefix: str = "cal"
) -> InlineKeyboardMarkup:
    """
    Inline month-grid calendar picker with ◀ and ▶ buttons (Section 6.4b).
    Callback format: `{prefix}:pick:YYYY-MM-DD` and `{prefix}:nav:YYYY-MM`.
    """
    b = InlineKeyboardBuilder()
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    b.row(
        InlineKeyboardButton(
            text="◀", callback_data=f"{prefix}:nav:{prev_y:04d}-{prev_m:02d}"
        ),
        InlineKeyboardButton(
            text=f"{year}-{month:02d}", callback_data="noop"
        ),
        InlineKeyboardButton(
            text="▶", callback_data=f"{prefix}:nav:{next_y:04d}-{next_m:02d}"
        ),
    )

    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="noop"))
            else:
                d_str = f"{year:04d}-{month:02d}-{day_num:02d}"
                row.append(
                    InlineKeyboardButton(
                        text=str(day_num),
                        callback_data=f"{prefix}:pick:{d_str}",
                    )
                )
        b.row(*row)

    b.row(
        InlineKeyboardButton(text=t(user, "btn_cancel"), callback_data="nav:menu"),
        InlineKeyboardButton(text=t(user, "btn_menu"), callback_data="nav:menu"),
    )
    return b.as_markup()
