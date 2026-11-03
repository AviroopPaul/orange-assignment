import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.core.identifiers import IDENTIFIER_PATTERN


class ApprovalCard(BaseModel):
    """Built only from fields on the approval row. No model text and no reviews reach it."""

    approval_id: uuid.UUID
    action: str
    provider_name: str
    service: str
    slot: str
    slot_label: str
    price: int
    currency: str
    notes: list[str]
    expires_at: datetime
    status: str


class ApprovalRead(BaseModel):
    card: ApprovalCard
    attempt_status: str | None = None
    booking_id: str | None = None


class BookingState(BaseModel):
    """What a page needs after a reload: a card to answer, or a booking to wait for."""

    approval: ApprovalCard | None = None
    attempt_open: bool = False


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(pattern=IDENTIFIER_PATTERN)


class DecisionStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    ALREADY_CONFIRMED = "already_confirmed"
    NEW_CARD = "new_card"
    SLOT_GONE = "slot_gone"
    NO_LONGER_VALID = "no_longer_valid"
    REJECTED = "rejected"


class ApprovalDecision(BaseModel):
    """The answer to a yes or a no, whether it was clicked or typed. `outcome` is a run outcome."""

    status: DecisionStatus
    outcome: str
    reply: str
    run_id: uuid.UUID | None = None
    session_id: str | None = None
    approval: ApprovalCard | None = None
