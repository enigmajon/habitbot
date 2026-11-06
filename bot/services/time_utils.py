"""
Localization loader (`t(user, key, **params)`) and core deterministic time,
duration, and midnight-splitting utilities.

Implements:
- Section 4 Rule 2, 3, 4, 7:
  - UTC storage, conversion to user timezone only for display and day boundaries.
  - Day boundary is 00:00 in user's timezone.
  - Sessions crossing midnight are split across local days at query time.
  - Durations are formatted as `1h 25m` (English) or `1 soat 25 daqiqa` (Uzbek),
    never decimals.
- Section 6.1:
  - Goal/duration parser supporting `3h`, `90m`, `1h30m`, `1.5h`, `1 soat 30 daqiqa`.
- Section 6.5:
  - 10-segment text progress bar (`▓▓▓▓▓▓░░░░ 62%`).
- Section 10:
  - `t(user_or_lang, key, **params)` translation helper loading `en.json` and `uz.json`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# 1. Localization (`t(user, key, **params)`)
# ---------------------------------------------------------------------------

_I18N_CACHE: dict[str, dict[str, str]] = {}


def _load_translations() -> dict[str, dict[str, str]]:
    if _I18N_CACHE:
        return _I18N_CACHE

    # Look for en.json and uz.json in the same directory as this file or in bot/i18n/
    search_dirs = [
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent.parent / "i18n",
    ]
    for lang in ("en", "uz"):
        for directory in search_dirs:
            candidate = directory / f"{lang}.json"
            if candidate.exists():
                with candidate.open("r", encoding="utf-8") as fh:
                    _I18N_CACHE[lang] = json.load(fh)
                break
        if lang not in _I18N_CACHE:
            raise FileNotFoundError(f"Missing translation file for language '{lang}'")
    return _I18N_CACHE


def get_lang_code(user_or_lang: Any) -> str:
    """Extract 'uz' or 'en' from a User model instance, dict, or language string."""
    if isinstance(user_or_lang, str):
        code = user_or_lang.lower()
    elif hasattr(user_or_lang, "language"):
        code = str(getattr(user_or_lang, "language") or "uz").lower()
    elif isinstance(user_or_lang, dict) and "language" in user_or_lang:
        code = str(user_or_lang["language"]).lower()
    else:
        code = "uz"
    return code if code in ("uz", "en") else "uz"


def t(user_or_lang: Any, key: str, **params: Any) -> str:
    """
    Translate `key` into the user's language ('uz' or 'en') and interpolate `**params`.
    Falls back to 'en' if a key is missing.
    """
    translations = _load_translations()
    lang = get_lang_code(user_or_lang)
    catalog = translations.get(lang, translations["en"])
    template = catalog.get(key) or translations["en"].get(key) or key
    if params:
        return template.format(**params)
    return template


# ---------------------------------------------------------------------------
# 2. Duration Formatting & Parsing (Rule 7 & Section 6.1 / 10)
# ---------------------------------------------------------------------------

_DURATION_PATTERN = re.compile(
    r"^\s*(?:(?P<hours>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|soat))?\s*"
    r"(?:(?P<minutes>\d+)\s*(?:m|min|mins|daqiqa|daq))?\s*$",
    re.IGNORECASE,
)


def parse_duration_minutes(raw: str) -> int:
    """
    Parse human duration input into positive integer minutes.
    Accepts formats required by Section 6.1: `3h`, `90m`, `1h30m`, `1.5h`
    as well as Uzbek equivalents (`1 soat 30 daqiqa`).
    Raises ValueError if invalid or <= 0.
    """
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Empty duration string")

    match = _DURATION_PATTERN.match(cleaned)
    if not match or (match.group("hours") is None and match.group("minutes") is None):
        raise ValueError(f"Invalid duration format: {raw!r}")

    hours_str = match.group("hours")
    minutes_str = match.group("minutes")

    # Disallow fractional hours combined with explicit minutes like "1.5h 20m"
    if hours_str and "." in hours_str and minutes_str:
        raise ValueError(f"Cannot combine decimal hours with minutes: {raw!r}")

    total_minutes = 0
    if hours_str is not None:
        total_minutes += round(float(hours_str) * 60)
    if minutes_str is not None:
        total_minutes += int(minutes_str)

    if total_minutes <= 0:
        raise ValueError("Duration must be greater than 0 minutes")
    return total_minutes


def format_duration(
    total_seconds_or_minutes: Union[int, float],
    user_or_lang: Any = "en",
    *,
    is_seconds: bool = False,
) -> str:
    """
    Format a duration as `1h 25m` (English) or `1 soat 25 daqiqa` (Uzbek).
    Never shows decimals (Section 4 Rule 7 & Section 10).
    """
    if is_seconds:
        total_minutes = int(round(total_seconds_or_minutes / 60.0))
        # If non-zero seconds rounded to 0m (e.g. 30-59s), count as 1m
        if total_seconds_or_minutes >= 30 and total_minutes == 0:
            total_minutes = 1
    else:
        total_minutes = int(round(total_seconds_or_minutes))

    total_minutes = max(0, total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    lang = get_lang_code(user_or_lang)

    if lang == "uz":
        if hours > 0 and minutes > 0:
            return f"{hours} soat {minutes} daqiqa"
        if hours > 0:
            return f"{hours} soat"
        return f"{minutes} daqiqa"
    else:
        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h"
        return f"{minutes}m"


def format_progress_bar(
    actual_minutes: float,
    target_minutes: float,
    *,
    is_limit: bool = False,
    segments: int = 10,
) -> str:
    """
    Render a 10-segment text progress bar as specified in Section 6.5:
    `▓▓▓▓▓▓░░░░ 62%` (with ⚠️ suffix if a limit activity is exceeded).
    """
    if target_minutes <= 0:
        return ""
    ratio = actual_minutes / target_minutes
    pct = int(round(ratio * 100))
    filled = max(0, min(segments, int(round(min(ratio, 1.0) * segments))))
    bar = ("▓" * filled) + ("░" * (segments - filled))
    warning = " ⚠️" if (is_limit and actual_minutes > target_minutes) else ""
    return f"{bar} {pct}%{warning}"


# ---------------------------------------------------------------------------
# 3. Timezone & Day Boundary Helpers (Rule 2 & Rule 3)
# ---------------------------------------------------------------------------

def get_zoneinfo(tz_name: str) -> ZoneInfo:
    """Return ZoneInfo for `tz_name`, falling back to Asia/Tashkent if invalid."""
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Tashkent")


def to_local(utc_dt: datetime, tz_name: str) -> datetime:
    """Convert a UTC datetime to the user's local timezone."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(get_zoneinfo(tz_name))


def local_today(tz_name: str, now_utc: Optional[datetime] = None) -> date:
    """Return the current local date in `tz_name`."""
    now = now_utc or datetime.now(timezone.utc)
    return to_local(now, tz_name).date()


def local_day_bounds_utc(local_d: date, tz_name: str) -> tuple[datetime, datetime]:
    """
    Return `(start_utc, end_utc)` corresponding to `[00:00:00, 24:00:00)` on `local_d`
    in the user's timezone.
    """
    tz = get_zoneinfo(tz_name)
    start_local = datetime.combine(local_d, time.min, tzinfo=tz)
    end_local = datetime.combine(local_d + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def local_week_bounds(local_d: date) -> tuple[date, date]:
    """
    Return `(monday_date, sunday_date)` for the week containing `local_d`
    (Section 6.5: Weeks run Monday 00:00 to Sunday 24:00 in the user's timezone).
    """
    monday = local_d - timedelta(days=local_d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


# ---------------------------------------------------------------------------
# 4. Midnight Session Splitting (Section 4 Rule 4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DailySessionSlice:
    """Represents the portion of a session falling within a single local calendar day."""

    session_id: Optional[int]
    activity_id: int
    local_date: date
    slice_start_utc: datetime
    slice_end_utc: datetime
    seconds: float

    @property
    def minutes(self) -> float:
        return self.seconds / 60.0


def split_session_by_local_days(
    started_at_utc: datetime,
    ended_at_utc: datetime,
    tz_name: str,
    *,
    session_id: Optional[int] = None,
    activity_id: int = 0,
) -> list[DailySessionSlice]:
    """
    Split a session `[started_at_utc, ended_at_utc)` across local midnight boundaries
    in `tz_name` (Section 4 Rule 4).

    Example: A session from 23:00 to 01:00 local time yields two slices:
    - Day 1: 23:00 -> 24:00 (3600 seconds / 60 minutes)
    - Day 2: 00:00 -> 01:00 (3600 seconds / 60 minutes)
    """
    if started_at_utc.tzinfo is None:
        started_at_utc = started_at_utc.replace(tzinfo=timezone.utc)
    if ended_at_utc.tzinfo is None:
        ended_at_utc = ended_at_utc.replace(tzinfo=timezone.utc)

    if ended_at_utc <= started_at_utc:
        return []

    tz = get_zoneinfo(tz_name)
    start_local = started_at_utc.astimezone(tz)
    end_local = ended_at_utc.astimezone(tz)

    slices: list[DailySessionSlice] = []
    cur_date = start_local.date()
    final_date = end_local.date()

    while cur_date <= final_date:
        day_start_utc, day_end_utc = local_day_bounds_utc(cur_date, tz_name)
        overlap_start = max(started_at_utc, day_start_utc)
        overlap_end = min(ended_at_utc, day_end_utc)

        if overlap_end > overlap_start:
            secs = (overlap_end - overlap_start).total_seconds()
            slices.append(
                DailySessionSlice(
                    session_id=session_id,
                    activity_id=activity_id,
                    local_date=cur_date,
                    slice_start_utc=overlap_start,
                    slice_end_utc=overlap_end,
                    seconds=secs,
                )
            )
        cur_date += timedelta(days=1)

    return slices
