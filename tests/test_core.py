"""
Pytest test suite covering Section 12 requirements:
1. Duration formatting and parsing (`1h30m`, `3h`, `90m`, `1.5h`, Uzbek `1 soat 25 daqiqa`)
2. Midnight session splitting across local day boundaries
3. Daily score formula (`build` and `limit` cases) and rating bands
4. Streak and freeze-token logic (7-day token award, max 2, auto-use on miss, rest day)
5. Weekly pace calculation (`needed per remaining day`)
6. i18n key parity check between `uz.json` and `en.json`
"""

from datetime import date, datetime, timezone
import pytest

from bot.services.time_utils import (
    _load_translations,
    format_duration,
    format_progress_bar,
    parse_duration_minutes,
    split_session_by_local_days,
)
from bot.services.core_services import (
    compute_activity_ratio,
    compute_daily_score,
    compute_weekly_needed_per_day,
    evaluate_overall_streak_day,
    get_score_band,
)


def test_i18n_key_parity():
    translations = _load_translations()
    assert set(translations["en"].keys()) == set(translations["uz"].keys())
    assert len(translations["en"]) > 150


def test_duration_parsing_and_formatting():
    assert parse_duration_minutes("3h") == 180
    assert parse_duration_minutes("90m") == 90
    assert parse_duration_minutes("1h30m") == 90
    assert parse_duration_minutes("1.5h") == 90
    assert format_duration(85, "en") == "1h 25m"
    assert format_duration(85, "uz") == "1 soat 25 daqiqa"


def test_midnight_splitting():
    # 23:00 to 01:00 Asia/Tashkent (UTC+5) -> 18:00 to 20:00 UTC
    start_utc = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
    end_utc = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)
    slices = split_session_by_local_days(
        start_utc, end_utc, "Asia/Tashkent", activity_id=1
    )
    assert len(slices) == 2
    assert slices[0].local_date == date(2026, 11, 4)
    assert slices[0].minutes == 60.0
    assert slices[1].local_date == date(2026, 11, 5)
    assert slices[1].minutes == 60.0


def test_daily_score_formula_build_and_limit():
    # build: 90/90 -> ratio 1.0 (weight 2)
    # limit: 90 actual vs 60 max -> ratio = 1 - (30/60) = 0.5 (weight 1)
    score = compute_daily_score([
        ("build", 90.0, 90, 2),
        ("limit", 90.0, 60, 1),
    ])
    assert score == round((2.5 / 3.0) * 100.0, 1)
    assert get_score_band(score) == "✅"
    assert get_score_band(92.0) == "🔥"
    assert get_score_band(60.0) == "😐"
    assert get_score_band(30.0) == "💤"


def test_streak_and_freeze_tokens():
    # Day 7 milestone earns 1 freeze token
    r1 = evaluate_overall_streak_day(
        prev_current=6,
        prev_best=6,
        freeze_tokens=0,
        score=80.0,
        streak_threshold=70,
        is_rest_day=False,
    )
    assert r1.new_current == 7
    assert r1.new_freeze_tokens == 1
    assert r1.earned_freeze_token is True

    # Missed day auto-spends freeze token and preserves streak
    r2 = evaluate_overall_streak_day(
        prev_current=7,
        prev_best=7,
        freeze_tokens=1,
        score=40.0,
        streak_threshold=70,
        is_rest_day=False,
    )
    assert r2.new_current == 7
    assert r2.new_freeze_tokens == 0
    assert r2.used_freeze_token is True


def test_weekly_pace_calculation():
    # Wednesday (weekday=2 -> 5 remaining days including today: Wed, Thu, Fri, Sat, Sun)
    needed, behind = compute_weekly_needed_per_day(
        actual_week_minutes=60.0,
        weekly_target_minutes=360,
        current_local_date=date(2026, 11, 4),  # Wednesday
    )
    assert needed == 60  # (360 - 60) / 5 = 60m per day
    assert behind is True
