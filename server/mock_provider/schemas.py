"""The tool contract from the brief. Names, inputs and failures here are fixed."""

from datetime import date, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mock_provider.outcomes import BookOutcome


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DateRange(_Strict):
    start: date
    end: date

    @model_validator(mode="after")
    def _in_order(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        if (self.end - self.start).days > 31:
            raise ValueError("date_range can cover at most 31 days")
        return self


class SearchProvidersInput(_Strict):
    service: str = Field(min_length=1, max_length=60)
    area: str = Field(min_length=1, max_length=100)
    date_range: DateRange


class GetProviderInput(_Strict):
    provider_id: str = Field(min_length=1, max_length=20)


class HoldSlotInput(_Strict):
    provider_id: str = Field(min_length=1, max_length=20)
    slot: str = Field(min_length=1, max_length=40)


class BookInput(_Strict):
    hold_id: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=100)


class GetBookingStatusInput(_Strict):
    hold_id: str | None = Field(default=None, max_length=40)
    idempotency_key: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if bool(self.hold_id) == bool(self.idempotency_key):
            raise ValueError("send exactly one of hold_id or idempotency_key")
        return self


class CancelInput(_Strict):
    booking_id: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=100)


class RescheduleInput(_Strict):
    booking_id: str = Field(min_length=1, max_length=40)
    slot: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=100)


class ListBookingsInput(_Strict):
    user_id: str = Field(min_length=1, max_length=64)


class ProviderAttributes(BaseModel):
    technician: str
    years_experience: int
    verified: bool
    languages: list[str]


class ProviderSummary(BaseModel):
    id: str
    name: str
    service: str
    area: str
    price: int
    currency: str
    rating: float
    review_count: int
    available_slots: list[str]
    attributes: ProviderAttributes


class SearchProvidersOutput(BaseModel):
    providers: list[ProviderSummary]


class ReviewRead(BaseModel):
    author: str
    rating: int
    text: str


class ProviderDetails(BaseModel):
    id: str
    name: str
    service: str
    price: int
    currency: str
    rating: float
    review_count: int
    attributes: ProviderAttributes
    reviews: list[ReviewRead]


class HoldRead(BaseModel):
    hold_id: str
    provider_id: str
    slot: str
    search_price: int
    quoted_price: int
    currency: str
    expires_at: datetime


class BookingRead(BaseModel):
    booking_id: str
    provider_id: str
    provider_name: str
    service: str
    slot: str
    price: int
    status: str
    when: str


class BookingStatusRead(BaseModel):
    status: str
    booking_id: str | None = None
    hold_id: str | None = None
    idempotency_key: str | None = None
    detail: str | None = None


class ListBookingsOutput(BaseModel):
    bookings: list[BookingRead]


class ToolErrorBody(BaseModel):
    error: str
    message: str


class ScenarioConfig(_Strict):
    """Eval mode only. Anything left out keeps its default behaviour."""

    seed: int | None = None
    book_outcomes: list[BookOutcome] = Field(default_factory=list)
    quote_deltas: dict[str, int] | None = None
    # A provider's list price changes, for example between an approval and the booking.
    provider_prices: dict[str, int] | None = None
    slot_conflicts: list[str] | None = None
    timeout_hang_s: float | None = None
    # `get_provider` holds the connection this long, so a worker can die just before `book`.
    get_provider_hang_s: float | None = None
    # How many calls to `cancel` get a 503 first. The brief's default is the first one.
    cancel_failures: int | None = None
    reset_clock: bool = False


class ProviderState(BaseModel):
    """Eval mode only. What is armed, so a person can see it took effect."""

    scenario: dict
    book_outcomes_used: int
    clock_offset_s: float


class AdvanceClock(_Strict):
    seconds: float = Field(gt=0)


class ProviderCancel(_Strict):
    booking_id: str = Field(min_length=1, max_length=40)
