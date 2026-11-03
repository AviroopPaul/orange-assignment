"""Notification queries. Flush only: the caller owns the transaction."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.notifications.models import Notification


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, notification: Notification) -> None:
        self._session.add(notification)
        await self._session.flush()

    async def after(
        self, user_id: str, session_id: str, after_id: int, limit: int
    ) -> Sequence[Notification]:
        rows = await self._session.scalars(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.session_id == session_id,
                Notification.id > after_id,
            )
            .order_by(Notification.id)
            .limit(limit)
        )
        return rows.all()

    async def latest(self, user_id: str, session_id: str, limit: int) -> Sequence[Notification]:
        rows = await self._session.scalars(
            select(Notification)
            .where(Notification.user_id == user_id, Notification.session_id == session_id)
            .order_by(Notification.id.desc())
            .limit(limit)
        )
        return rows.all()
