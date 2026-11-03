"""One errand in progress. Code owns this row; the model can never set its state."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, JsonColumn, UtcDateTime


class WorkflowState(StrEnum):
    INTAKE = "intake"
    SEARCHING = "searching"
    COMPARING = "comparing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    DONE = "done"
    DISRUPTED = "disrupted"
    FAILED = "failed"
    LIMIT_REACHED = "limit_reached"


_ACTIVE = "state NOT IN ('done', 'failed')"


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        # A session works on one errand at a time.
        Index(
            "uq_workflows_active_session",
            "user_id",
            "session_id",
            unique=True,
            postgresql_where=text(_ACTIVE),
            sqlite_where=text(_ACTIVE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default=WorkflowState.INTAKE.value)
    # Every update names the version it read, so two processes cannot overwrite each other.
    version: Mapped[int] = mapped_column(default=1)
    # {field: {"value": ..., "source": "user", "updated_at": iso time}}
    request: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    # What the user really wrote, kept to check that saved details are grounded in it.
    user_text: Mapped[str] = mapped_column(Text, default="")
    # The last search after hard limits, by provider ID. A proposal must come from this.
    shortlist: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
