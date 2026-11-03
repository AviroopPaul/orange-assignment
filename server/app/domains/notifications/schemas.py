from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    kind: str
    text: str
    data: dict[str, Any]
    created_at: datetime


class NotificationList(BaseModel):
    notifications: list[NotificationRead]
