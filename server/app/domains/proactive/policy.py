"""Whether to tell the user, and what to offer. Pure code: the model has no part in this.

The rules can be tested one by one, and every decision carries the reason the trace records.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from app.domains.workflow.request import field_value, missing_required, search_args

PROVIDER_CANCELLED = "cancelled_by_provider"
PAST = "past"
MAX_OPTIONS = 3
MAX_DAYS_SHOWN = 3


class Reason(StrEnum):
    PROVIDER_CANCELLED = "provider_cancelled"
    PAST_BOOKING = "past_booking"
    DUPLICATE = "duplicate"
    NOT_OUR_BOOKING = "not_our_booking"
    CHANGE_IN_PROGRESS = "change_in_progress"
    NO_CHANGE = "no_change"
    REPLACEMENTS_OFFERED = "replacements_offered"
    NO_REPLACEMENT_FITS = "no_replacement_fits"
    NO_PREFERENCES = "no_preferences"
    DATES_PASSED = "dates_passed"
    SEARCH_FAILED = "search_failed"
    ERRAND_IN_PROGRESS = "errand_in_progress"


@dataclass(frozen=True)
class Decision:
    reason: Reason
    # Tell the user now.
    notify: bool = False
    # Keep a row, so the decision is on record and is not made again.
    record: bool = False


def decide(
    booking: dict[str, Any], on_record: bool, already_recorded: bool, change_running: bool
) -> Decision:
    if booking.get("status") != PROVIDER_CANCELLED:
        return Decision(Reason.NO_CHANGE)
    if not on_record:
        return Decision(Reason.NOT_OUR_BOOKING)
    if already_recorded:
        return Decision(Reason.DUPLICATE)
    # The saga is working on this booking and will report what the provider says. Look next time.
    if change_running:
        return Decision(Reason.CHANGE_IN_PROGRESS)
    if booking.get("when") == PAST:
        return Decision(Reason.PAST_BOOKING, record=True)
    return Decision(Reason.PROVIDER_CANCELLED, notify=True, record=True)


def replacement_search(request: dict[str, Any], today: date) -> dict[str, Any] | Reason:
    """The saved request, moved on to today. There is no search without the user's own limits."""
    if missing_required(request):
        return Reason.NO_PREFERENCES
    start = date.fromisoformat(field_value(request, "start_date"))
    end = date.fromisoformat(field_value(request, "end_date"))
    if end < today:
        return Reason.DATES_PASSED
    args = search_args(request)
    args["date_range"] = {"start": max(start, today).isoformat(), "end": end.isoformat()}
    return args


def best_options(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(providers, key=lambda found: (-(found.get("rating") or 0), found.get("price")))
    return ranked[:MAX_OPTIONS]
