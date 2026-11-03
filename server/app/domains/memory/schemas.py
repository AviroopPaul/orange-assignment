import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.identifiers import IDENTIFIER_PATTERN


class MemoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    value: str
    scope: str
    source: str
    quote: str | None
    confirmed: bool
    updated_at: datetime


class MemoryList(BaseModel):
    memories: list[MemoryRead]


class ChatMemoryNotes(BaseModel):
    """What a reply shows beside its text. Built from the rows, never from the model's words."""

    saved: list[MemoryRead] = []
    to_confirm: list[MemoryRead] = []


class MemoryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(pattern=IDENTIFIER_PATTERN)


class MemoryCorrection(MemoryAction):
    value: str = Field(min_length=1, max_length=300)
