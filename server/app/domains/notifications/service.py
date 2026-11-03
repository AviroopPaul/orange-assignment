"""Writes join the caller's transaction, so a notice commits with the change it reports."""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.database import Database
from app.core.wakeups import Wakeups
from app.domains.notifications.models import Notification
from app.domains.notifications.repository import NotificationRepository
from app.domains.notifications.schemas import NotificationRead

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
# How long a reader waits for a wake-up before it looks again, and proves the connection is alive.
LOOK_AGAIN_S = 10.0


class NotificationService:
    def __init__(self, db: Database, clock: Clock, wakeups: Wakeups | None = None) -> None:
        self._db = db
        self._clock = clock
        self._wakeups = wakeups
        self._sending: set[asyncio.Task[None]] = set()

    async def add_in(
        self,
        session: AsyncSession,
        user_id: str,
        session_id: str,
        kind: str,
        text: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        await NotificationRepository(session).add(
            Notification(
                user_id=user_id,
                session_id=session_id,
                kind=kind,
                text=text,
                data=data or {},
                created_at=self._clock.now(),
            )
        )
        self._wake_after_commit(session, user_id)

    def _wake_after_commit(self, session: AsyncSession, user_id: str) -> None:
        """A reader woken before the commit would find nothing, then wait for its timer."""
        wakeups = self._wakeups
        if wakeups is None:
            return

        def send(_: object) -> None:
            task = asyncio.get_running_loop().create_task(wakeups.publish(user_id))
            self._sending.add(task)
            task.add_done_callback(self._sending.discard)

        event.listen(session.sync_session, "after_commit", send, once=True)

    async def list_after(
        self, user_id: str, session_id: str, after_id: int = 0
    ) -> list[NotificationRead]:
        async with self._db.session_scope() as session:
            rows = await NotificationRepository(session).after(
                user_id, session_id, after_id, PAGE_SIZE
            )
        return [NotificationRead.model_validate(row) for row in rows]

    async def _read_for_user(self, user_id: str, after_id: int) -> list[NotificationRead]:
        try:
            async with self._db.session_scope() as session:
                rows = await NotificationRepository(session).after_for_user(
                    user_id, after_id, PAGE_SIZE
                )
        except (SQLAlchemyError, OSError):
            # The stream stays open. The next look finds whatever was stored.
            logger.warning("could not read notifications for %s", user_id)
            return []
        return [NotificationRead.model_validate(row) for row in rows]

    async def stream(self, user_id: str, after_id: int) -> AsyncIterator[NotificationRead | None]:
        """Everything stored after the cursor, then new rows as they come. None means no news."""
        if self._wakeups is None:
            raise RuntimeError("streaming needs wakeups")
        cursor = after_id
        async with self._wakeups.listen(user_id) as listener:
            while True:
                rows = await self._read_for_user(user_id, cursor)
                for row in rows:
                    cursor = row.id
                    yield row
                if len(rows) == PAGE_SIZE:
                    continue
                if not await listener.wait(LOOK_AGAIN_S):
                    yield None

    async def latest_with_times(
        self, user_id: str, session_id: str, limit: int
    ) -> list[tuple[str, datetime]]:
        """Newest last."""
        async with self._db.session_scope() as session:
            rows = await NotificationRepository(session).latest(user_id, session_id, limit)
        return [(row.text, row.created_at) for row in reversed(rows)]

    async def latest_texts(self, user_id: str, session_id: str, limit: int) -> list[str]:
        """Newest last, ready for the model's next turn."""
        async with self._db.session_scope() as session:
            rows = await NotificationRepository(session).latest(user_id, session_id, limit)
        return [row.text for row in reversed(rows)]
