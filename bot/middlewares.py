"""
Telegram middlewares for session management, user loading, per-user token-bucket
button rate limiting (Section 11), and privacy-safe error handling.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.repositories import UserRepository
from bot.services.time_utils import t

logger = logging.getLogger(__name__)


class TokenBucketRateLimiter:
    """Simple per-user token bucket to prevent button spam (Section 11)."""

    def __init__(self, rate: float = 3.0, capacity: float = 6.0) -> None:
        self.rate = rate
        self.capacity = capacity
        self._buckets: dict[int, tuple[float, float]] = {}

    def allow(self, telegram_id: int) -> bool:
        now = time.monotonic()
        tokens, last_ts = self._buckets.get(telegram_id, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last_ts) * self.rate)
        if tokens < 1.0:
            self._buckets[telegram_id] = (tokens, now)
            return False
        self._buckets[telegram_id] = (tokens - 1.0, now)
        return True


class DatabaseAndUserMiddleware(BaseMiddleware):
    """
    Injects `db` (`AsyncSession`) and `user` (`User`) into handler data, commits on
    success, rolls back on exception, and applies token-bucket rate limiting.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        default_tz: str = "Asia/Tashkent",
    ) -> None:
        self.session_factory = session_factory
        self.default_tz = default_tz
        self.limiter = TokenBucketRateLimiter(rate=3.0, capacity=6.0)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user and isinstance(event, CallbackQuery):
            if not self.limiter.allow(tg_user.id):
                try:
                    await event.answer("⏳", show_alert=False)
                except Exception:
                    pass
                return None

        async with self.session_factory() as session:
            data["db"] = session
            if tg_user is not None:
                user_repo = UserRepository(session)
                user, is_new = await user_repo.get_or_create(
                    tg_user.id, default_tz=self.default_tz
                )
                if user.is_blocked:
                    user.is_blocked = False
                data["user"] = user
                data["is_new_user"] = is_new

            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception as exc:
                await session.rollback()
                logger.error(
                    "Unhandled handler error user_id=%s err=%s",
                    tg_user.id if tg_user else "unknown",
                    type(exc).__name__,
                    exc_info=True,
                )
                user_obj = data.get("user", "uz")
                err_msg = t(user_obj, "error_generic")
                try:
                    if isinstance(event, CallbackQuery):
                        await event.answer(err_msg, show_alert=True)
                    elif isinstance(event, Message):
                        await event.answer(err_msg)
                except Exception:
                    pass
                return None
