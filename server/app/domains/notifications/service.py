"""Writes join the caller's transaction, so a notice commits with the change it reports."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.database import Database
from app.domains.notifications.models import Notification
from app.domains.notifications.repository import NotificationRepository
from app.domains.notifications.schemas import NotificationRead

PAGE_SIZE = 50


class NotificationService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

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

    async def list_after(
        self, user_id: str, session_id: str, after_id: int = 0
    ) -> list[NotificationRead]:
        async with self._db.session_scope() as session:
            rows = await NotificationRepository(session).after(
                user_id, session_id, after_id, PAGE_SIZE
            )
        return [NotificationRead.model_validate(row) for row in rows]

    async def latest_texts(self, user_id: str, session_id: str, limit: int) -> list[str]:
        """Newest last, ready for the model's next turn."""
        async with self._db.session_scope() as session:
            rows = await NotificationRepository(session).latest(user_id, session_id, limit)
        return [row.text for row in reversed(rows)]
