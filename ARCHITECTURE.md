# Architecture Overview

## 1. Layered Structure
- **`bot/handlers/`**: Telegram UI layer (`aiogram 3.x`). Parses incoming commands, callback queries, and FSM text inputs, invokes services, and renders localized responses via `t(user, key, **params)`. Contains zero business logic.
- **`bot/services/`**: Deterministic business logic (`time_utils.py` and `core_services.py`). Implements timer start/stop/switch, Pomodoro phase state transitions, midnight session splitting, the `build` vs. `limit` weighted daily score formula, streak & freeze-token transitions, 14-day recurring calendar occurrence generation, UTF-8-BOM CSV export, and 24-hour undo reversal.
- **`bot/repositories/`**: Async SQLAlchemy 2.x data access layer. Every query strictly filters by `user_id` to guarantee multi-tenant data isolation.
- **`bot/models/`**: SQLAlchemy 2.x ORM models (`User`, `Category`, `Activity`, `Goal`, `Session`, `Plan`, `PlanOccurrence`, `DailySummary`, `Streak`, `UndoLog`, `ScheduledJob`, `FSMStateRecord`) compatible with both PostgreSQL (`asyncpg`) and SQLite (`aiosqlite`) with zero code changes.
- **`bot/scheduler/`**: Database-driven 30-second polling loop (`SchedulerRunner`).
- **`bot/charts/`**: Headless Matplotlib chart renderer (`weekly_charts.py`) producing in-memory PNG bytes.

## 2. Database-Driven Scheduler Loop
Rather than storing ephemeral timers in RAM, all future events (`morning_digest`, `plan_pre_reminder`, `day_summary`, `idle_nudge`, `weekly_report`, `long_timer_check`, `pomodoro_phase_end`) are persisted in `scheduled_jobs(user_id, kind, run_at, payload_json, sent_at)` indexed on `(run_at) WHERE sent_at IS NULL`.
Every 30 seconds, `SchedulerRunner.run_once()` selects due jobs (`run_at <= now AND sent_at IS NULL`), dispatches the Telegram message respecting the 25 msg/sec global ceiling, and sets `sent_at = now` inside the same database transaction. If the bot restarts during a running timer or Pomodoro interval, overdue jobs fire immediately on startup.

## 3. Midnight Day-Splitting Logic
All session timestamps (`started_at`, `ended_at`) are stored in UTC as a single row in `sessions`. When calculating daily totals, daily scores, streaks, or weekly charts, `split_session_by_local_days(started_at_utc, ended_at_utc, user.timezone)` converts the session window into the user's IANA timezone (`00:00` day boundary) and slices any session crossing midnight into `[00:00, 24:00)` `DailySessionSlice` segments (e.g., `23:00` to `01:00` yields `60m` on Day 1 and `60m` on Day 2).
