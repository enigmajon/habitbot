"""
Application entrypoint for the Telegram Habit & Time Tracker Bot.

Starts:
1. SQLAlchemy 2.x async engine and automatic schema verification (`Base.metadata.create_all`).
2. Lightweight HTTP health check server on `$PORT` (if set by Koyeb/Render free tier)
   so cloud PaaS health checks pass 24/7.
3. `SchedulerRunner` background task (every 30 seconds).
4. `aiogram 3.x` Dispatcher long polling with graceful shutdown that finishes in-flight
   jobs (Section 11).
"""

from __future__ import annotations

import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot.config import get_settings, setup_logging
from bot.fsm import DatabaseFSMStorage
from bot.handlers.router import router as main_router
from bot.middlewares import DatabaseAndUserMiddleware
from bot.models.models import Base, create_db_engine, create_session_factory
from bot.scheduler.runner import SchedulerRunner

logger = logging.getLogger(__name__)


async def _start_health_server() -> web.AppRunner | None:
    """
    If PORT is set in the environment (standard on Koyeb / Render free Web Services),
    bind a tiny HTTP 200 `/health` endpoint so the free cloud host marks the instance healthy.
    """
    port_str = os.environ.get("PORT", "8000")
    try:
        port = int(port_str)
    except ValueError:
        return None

    app = web.Application()
    app.router.add_get("/", lambda _req: web.Response(text="HabitBot OK"))
    app.router.add_get("/health", lambda _req: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health check server listening on port=%s", port)
    return runner


async def main() -> None:
    settings = get_settings()
    setup_logging(settings)

    engine = create_db_engine(settings.DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    storage = DatabaseFSMStorage(
        session_factory, timeout_minutes=settings.FSM_TIMEOUT_MINUTES
    )

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=storage)

    middleware = DatabaseAndUserMiddleware(
        session_factory, default_tz=settings.DEFAULT_TZ
    )
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.include_router(main_router)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start / Onboarding"),
            BotCommand(command="menu", description="Main menu"),
            BotCommand(command="today", description="Today's progress & score"),
            BotCommand(command="status", description="Running timer status"),
            BotCommand(command="week", description="Weekly report & charts"),
            BotCommand(command="plan", description="Planner & calendar"),
            BotCommand(command="export", description="Export CSV"),
            BotCommand(command="undo", description="Undo last action"),
            BotCommand(command="settings", description="Settings"),
            BotCommand(command="help", description="Help"),
        ]
    )

    scheduler = SchedulerRunner(
        bot=bot,
        session_factory=session_factory,
        interval_seconds=settings.SCHEDULER_INTERVAL_SECONDS,
        rate_limit_per_sec=settings.GLOBAL_MSG_RATE_LIMIT,
    )
    scheduler_task = asyncio.create_task(scheduler.start())
    health_runner = await _start_health_server()

    try:
        logger.info("Starting Telegram long polling...")
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down gracefully...")
        await scheduler.stop()
        scheduler_task.cancel()
        if health_runner:
            await health_runner.cleanup()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
