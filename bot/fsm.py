"""
Database-backed finite-state machine storage for aiogram 3.x with automatic
10-minute staleness expiration (Section 7 UI rules).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.models.models import FSMStateRecord, utc_now


class DatabaseFSMStorage(BaseStorage):
    """Stores aiogram FSM state and data in the `fsm_states` database table."""

    def __init__(
        self, session_factory: async_sessionmaker, timeout_minutes: int = 10
    ) -> None:
        self.session_factory = session_factory
        self.timeout_minutes = timeout_minutes

    def _is_expired(self, record: FSMStateRecord) -> bool:
        cutoff = utc_now() - timedelta(minutes=self.timeout_minutes)
        return record.updated_at < cutoff

    async def _get_record(
        self, session, key: StorageKey
    ) -> Optional[FSMStateRecord]:
        stmt = select(FSMStateRecord).where(
            and_(
                FSMStateRecord.bot_id == key.bot_id,
                FSMStateRecord.chat_id == key.chat_id,
                FSMStateRecord.user_id == key.user_id,
            )
        )
        record = (await session.execute(stmt)).scalar_one_or_none()
        if record and self._is_expired(record):
            await session.delete(record)
            await session.commit()
            return None
        return record

    async def set_state(
        self, key: StorageKey, state: Optional[State | str] = None
    ) -> None:
        state_str = state.state if isinstance(state, State) else state
        async with self.session_factory() as session:
            record = await self._get_record(session, key)
            if record is None:
                record = FSMStateRecord(
                    bot_id=key.bot_id,
                    chat_id=key.chat_id,
                    user_id=key.user_id,
                    state=state_str,
                    data_json={},
                    updated_at=utc_now(),
                )
                session.add(record)
            else:
                record.state = state_str
                record.updated_at = utc_now()
            await session.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with self.session_factory() as session:
            record = await self._get_record(session, key)
            return record.state if record else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        async with self.session_factory() as session:
            record = await self._get_record(session, key)
            if record is None:
                record = FSMStateRecord(
                    bot_id=key.bot_id,
                    chat_id=key.chat_id,
                    user_id=key.user_id,
                    state=None,
                    data_json=dict(data),
                    updated_at=utc_now(),
                )
                session.add(record)
            else:
                record.data_json = dict(data)
                record.updated_at = utc_now()
            await session.commit()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with self.session_factory() as session:
            record = await self._get_record(session, key)
            return dict(record.data_json) if record else {}

    async def close(self) -> None:
        pass
