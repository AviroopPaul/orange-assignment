"""What the system tells a user outside a chat turn. Code writes the text, never the model."""

from datetime import datetime

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, BigIntPk, JsonColumn, UtcDateTime


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_session", "user_id", "session_id", "id"),)

    # The id also serves as the reader's cursor.
    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
