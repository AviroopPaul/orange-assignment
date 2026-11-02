"""Durable job records. PostgreSQL decides what work exists; Redis only carries it."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, JsonColumn, UtcDateTime


class JobStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OutboxJob(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index(
            "ix_outbox_open",
            "status",
            "created_at",
            postgresql_where=text("status IN ('pending', 'published', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_name: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
