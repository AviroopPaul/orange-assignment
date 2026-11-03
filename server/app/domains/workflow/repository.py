"""Workflow queries. Flush only: the caller owns the transaction."""

import uuid
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.workflow.models import Workflow, WorkflowState

_CLOSED = [WorkflowState.DONE.value, WorkflowState.FAILED.value]


class WorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(
        self, user_id: str, session_id: str, lock: bool = False
    ) -> Workflow | None:
        query = select(Workflow).where(
            Workflow.user_id == user_id,
            Workflow.session_id == session_id,
            Workflow.state.not_in(_CLOSED),
        )
        # Writers queue on the row for a moment, so concurrent updates do not collide.
        return await self._session.scalar(query.with_for_update() if lock else query)

    async def add(self, workflow: Workflow) -> None:
        self._session.add(workflow)
        await self._session.flush()

    async def save(self, workflow_id: uuid.UUID, expected_version: int, **values: object) -> bool:
        """Write only if nobody changed the row since it was read."""
        result = await self._session.execute(
            update(Workflow)
            .where(Workflow.id == workflow_id, Workflow.version == expected_version)
            .values(version=expected_version + 1, **values)
        )
        return cast(CursorResult, result).rowcount > 0
