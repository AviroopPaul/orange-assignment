"""Every tool the system may call, who may call it, and how failures are retried."""

from dataclasses import dataclass

from pydantic import BaseModel

from app.domains.gateway.schemas import (
    BookArgs,
    Caller,
    CancelArgs,
    GetBookingStatusArgs,
    GetProviderArgs,
    HoldSlotArgs,
    ListBookingsArgs,
    ProposeBookingArgs,
    ProposeCancelArgs,
    ProposeRescheduleArgs,
    RescheduleArgs,
    ResolveDatesArgs,
    SearchProvidersArgs,
    UpdateRequestArgs,
)

EVERYONE = frozenset(Caller)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 1
    on_statuses: frozenset[int] = frozenset()
    on_timeout: bool = False


# Safe to repeat, because they change nothing.
READ_RETRY = RetryPolicy(attempts=3, on_statuses=frozenset({502, 503, 504}), on_timeout=True)
# Safe to repeat, because the provider replays a repeated idempotency key.
KEYED_RETRY = RetryPolicy(attempts=4, on_statuses=frozenset({502, 503}), on_timeout=True)
# `book` is never retried here. After a timeout the saga must ask the source of truth first.
# `hold_slot` has no key, so a repeat could hold a second slot.
NO_RETRY = RetryPolicy()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    callers: frozenset[Caller]
    retry: RetryPolicy
    side_effect: bool = False
    requires_approval: bool = False
    # Answered by our own code, through call_local. Nothing reaches the provider.
    local: bool = False


_SPECS = (
    ToolSpec("update_request", UpdateRequestArgs, frozenset({Caller.MODEL}), NO_RETRY, local=True),
    ToolSpec("resolve_dates", ResolveDatesArgs, frozenset({Caller.MODEL}), NO_RETRY, local=True),
    # Takes a hold and shows the user a card. Nothing else follows without their approval.
    ToolSpec(
        "propose_booking", ProposeBookingArgs, frozenset({Caller.MODEL}), NO_RETRY, local=True
    ),
    # Each only reads the booking and shows a card. The change waits for the user's approval.
    ToolSpec("propose_cancel", ProposeCancelArgs, frozenset({Caller.MODEL}), NO_RETRY, local=True),
    ToolSpec(
        "propose_reschedule",
        ProposeRescheduleArgs,
        frozenset({Caller.MODEL}),
        NO_RETRY,
        local=True,
    ),
    ToolSpec("search_providers", SearchProvidersArgs, EVERYONE, READ_RETRY),
    ToolSpec("get_provider", GetProviderArgs, EVERYONE, READ_RETRY),
    ToolSpec("list_bookings", ListBookingsArgs, EVERYONE, READ_RETRY),
    ToolSpec("get_booking_status", GetBookingStatusArgs, EVERYONE, READ_RETRY),
    ToolSpec(
        "hold_slot",
        HoldSlotArgs,
        frozenset({Caller.PROPOSAL, Caller.SAGA}),
        NO_RETRY,
        side_effect=True,
    ),
    ToolSpec(
        "book",
        BookArgs,
        frozenset({Caller.SAGA}),
        NO_RETRY,
        side_effect=True,
        requires_approval=True,
    ),
    ToolSpec(
        "cancel",
        CancelArgs,
        frozenset({Caller.SAGA}),
        KEYED_RETRY,
        side_effect=True,
        requires_approval=True,
    ),
    ToolSpec(
        "reschedule",
        RescheduleArgs,
        frozenset({Caller.SAGA}),
        KEYED_RETRY,
        side_effect=True,
        requires_approval=True,
    ),
)

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}
