"""Tool inputs (the contract from the brief), callers and results."""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Caller(StrEnum):
    """Who is asking. Code sets this; the model cannot."""

    MODEL = "model"
    PROPOSAL = "proposal"
    SAGA = "saga"
    BACKGROUND = "background"


class ToolStatus(StrEnum):
    OK = "ok"
    REFUSED = "refused"
    ERROR = "error"
    TIMEOUT = "timeout"


class RefusalReason(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    NOT_ALLOWED = "not_allowed"
    INVALID_ARGUMENTS = "invalid_arguments"
    WRONG_USER = "wrong_user"
    APPROVAL_REQUIRED = "approval_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MISSING_DETAILS = "missing_details"
    REQUEST_MISMATCH = "request_mismatch"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DateRange(_Strict):
    start: date = Field(description="First day to search, as YYYY-MM-DD.")
    end: date = Field(description="Last day to search, as YYYY-MM-DD.")


class SearchProvidersArgs(_Strict):
    service: str = Field(min_length=1, max_length=60)
    area: str = Field(min_length=1, max_length=100)
    date_range: DateRange


class GetProviderArgs(_Strict):
    provider_id: str = Field(min_length=1, max_length=20)


class HoldSlotArgs(_Strict):
    provider_id: str = Field(min_length=1, max_length=20)
    slot: str = Field(min_length=1, max_length=40)


class BookArgs(_Strict):
    hold_id: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=100)


class GetBookingStatusArgs(_Strict):
    hold_id: str | None = Field(default=None, max_length=40)
    idempotency_key: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if bool(self.hold_id) == bool(self.idempotency_key):
            raise ValueError("send exactly one of hold_id or idempotency_key")
        return self


class CancelArgs(_Strict):
    booking_id: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=100)


class RescheduleArgs(_Strict):
    booking_id: str = Field(min_length=1, max_length=40)
    slot: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=100)


class ListBookingsArgs(_Strict):
    user_id: str = Field(min_length=1, max_length=64)


class UpdateRequestArgs(_Strict):
    service: str | None = Field(default=None, min_length=1, max_length=60)
    area: str | None = Field(default=None, min_length=1, max_length=100)
    earliest_time: str | None = Field(default=None, max_length=5)
    latest_time: str | None = Field(default=None, max_length=5)
    max_budget: int | None = Field(default=None, gt=0)
    preferences: list[str] | None = Field(default=None, max_length=20)
    clear_fields: list[str] | None = Field(default=None, max_length=8)


class ProposeBookingArgs(_Strict):
    provider_id: str = Field(min_length=1, max_length=20)
    slot: str = Field(min_length=1, max_length=40)


class ProposeCancelArgs(_Strict):
    booking_id: str = Field(min_length=1, max_length=40)


class ProposeRescheduleArgs(_Strict):
    booking_id: str = Field(min_length=1, max_length=40)
    day: str = Field(min_length=1, max_length=100)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class ResolveDatesArgs(_Strict):
    phrase: str = Field(min_length=1, max_length=100)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: ToolStatus
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    attempts: int = 0
    # True when a side effect may or may not have happened. The caller must check the source of
    # truth before it retries or reports anything.
    ambiguous: bool = False
    injection_flags: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == ToolStatus.OK
