"""Memory queries. Flush only: the caller owns the transaction."""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.memory.models import (
    ContextLog,
    Memory,
    MemorySource,
    MemoryStatus,
    SessionRotation,
    TurnView,
)

_IS_ACTIVE = Memory.status == MemoryStatus.ACTIVE.value
_IS_TRUSTED = or_(Memory.source == MemorySource.EXPLICIT.value, Memory.confirmed_at.is_not(None))


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, row: object) -> None:
        self._session.add(row)
        await self._session.flush()

    async def trusted(self, user_id: str) -> Sequence[Memory]:
        """The filter comes first, in SQL: nothing forgotten or unconfirmed leaves the table."""
        result = await self._session.scalars(
            select(Memory)
            .where(Memory.user_id == user_id, _IS_ACTIVE, _IS_TRUSTED)
            .order_by(Memory.updated_at.desc(), Memory.key)
        )
        return result.all()

    async def active(self, user_id: str) -> Sequence[Memory]:
        result = await self._session.scalars(
            select(Memory)
            .where(Memory.user_id == user_id, _IS_ACTIVE)
            .order_by(Memory.updated_at.desc(), Memory.key)
        )
        return result.all()

    async def get(self, user_id: str, memory_id: uuid.UUID, lock: bool = False) -> Memory | None:
        query = (
            select(Memory)
            .where(Memory.id == memory_id, Memory.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(query.with_for_update() if lock else query)

    async def trusted_for_key(self, user_id: str, key: str, scope: str) -> Memory | None:
        return await self._session.scalar(
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.key == key,
                Memory.scope == scope,
                _IS_ACTIVE,
                _IS_TRUSTED,
            )
            .with_for_update()
        )

    async def unconfirmed_for_key(self, user_id: str, key: str, scope: str) -> Sequence[Memory]:
        result = await self._session.scalars(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.key == key,
                Memory.scope == scope,
                _IS_ACTIVE,
                Memory.source == MemorySource.INFERRED.value,
                Memory.confirmed_at.is_(None),
            )
        )
        return result.all()

    async def all_for_key(self, user_id: str, key: str, scope: str) -> Sequence[Memory]:
        result = await self._session.scalars(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.key == key,
                Memory.scope == scope,
                Memory.status != MemoryStatus.FORGOTTEN.value,
            )
        )
        return result.all()

    async def with_chat_note(self, user_id: str, session_id: str) -> Sequence[Memory]:
        result = await self._session.scalars(
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.session_id == session_id,
                Memory.chat_note.is_not(None),
            )
            .order_by(Memory.created_at, Memory.key)
        )
        return result.all()

    async def scrub_pending(self, user_id: str) -> Sequence[Memory]:
        result = await self._session.scalars(
            select(Memory).where(Memory.user_id == user_id, Memory.scrub_pending.is_(True))
        )
        return result.all()

    async def has_any(self, user_id: str) -> bool:
        found = await self._session.scalar(
            select(Memory.id).where(Memory.user_id == user_id).limit(1)
        )
        return found is not None

    async def recent_turns(self, user_id: str, session_id: str, limit: int) -> list[TurnView]:
        result = await self._session.scalars(
            select(TurnView)
            .where(TurnView.user_id == user_id, TurnView.session_id == session_id)
            .order_by(TurnView.id.desc())
            .limit(limit)
        )
        return list(reversed(result.all()))

    async def all_turns(self, user_id: str) -> Sequence[TurnView]:
        result = await self._session.scalars(select(TurnView).where(TurnView.user_id == user_id))
        return result.all()

    async def context_logs(self, user_id: str) -> Sequence[ContextLog]:
        result = await self._session.scalars(
            select(ContextLog).where(ContextLog.user_id == user_id)
        )
        return result.all()

    async def context_log(self, context_id: int) -> ContextLog | None:
        return await self._session.get(ContextLog, context_id)

    async def session_ids(self, user_id: str) -> list[str]:
        result = await self._session.scalars(
            select(TurnView.session_id).where(TurnView.user_id == user_id).distinct()
        )
        return list(result.all())

    async def rotations_due(self, user_id: str) -> list[str]:
        result = await self._session.scalars(
            select(SessionRotation.session_id).where(SessionRotation.user_id == user_id)
        )
        return list(result.all())

    async def rotation(self, user_id: str, session_id: str) -> SessionRotation | None:
        return await self._session.get(SessionRotation, (user_id, session_id))

    async def clear_rotation(self, user_id: str, session_id: str) -> None:
        await self._session.execute(
            delete(SessionRotation).where(
                SessionRotation.user_id == user_id, SessionRotation.session_id == session_id
            )
        )
