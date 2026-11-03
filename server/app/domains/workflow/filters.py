"""Hard limits, applied by code before the model sees any provider.

The model must not be trusted to apply a budget or a time window. It compares what is left. The
result is also kept small, because a raw search cost over 6,000 input tokens. So it names the days a
provider is open, and `get_open_slots` gives the times of one day when they are needed.
"""

from datetime import datetime
from typing import Any

from app.domains.workflow.request import CHOSEN_PROVIDER, field_value, parse_time


def open_days(slots: list[str]) -> list[str]:
    """Written by code, so the model need not work out a date or a weekday."""
    days = {datetime.fromisoformat(slot).date() for slot in slots}
    return [f"{day.isoformat()} {day:%a}" for day in sorted(days)]


def _in_window(slot: str, earliest: Any, latest: Any) -> bool:
    """The earliest time is included and the latest is not, so mornings end before 12:00."""
    try:
        starts_at = datetime.fromisoformat(slot).time()
    except ValueError:
        return False
    return (earliest is None or starts_at >= earliest) and (latest is None or starts_at < latest)


def _slots_in_window(provider: dict[str, Any], request: dict[str, Any]) -> list[str]:
    earliest = parse_time(str(field_value(request, "earliest_time") or ""))
    latest = parse_time(str(field_value(request, "latest_time") or ""))
    return [
        slot for slot in provider.get("available_slots", []) if _in_window(slot, earliest, latest)
    ]


def _is_chosen(provider: dict[str, Any], request: dict[str, Any]) -> bool:
    chosen = field_value(request, CHOSEN_PROVIDER)
    return chosen is None or provider.get("id") == chosen


def build_shortlist(providers: list[dict[str, Any]], request: dict[str, Any]) -> dict[str, Any]:
    """What may be proposed, including slots too many to show the model."""
    max_budget = field_value(request, "max_budget")
    shortlist = {}
    for provider in providers:
        if not _is_chosen(provider, request):
            continue
        slots = _slots_in_window(provider, request)
        if slots and (max_budget is None or provider.get("price", 0) <= max_budget):
            shortlist[str(provider.get("id"))] = {
                "name": provider.get("name"),
                "service": provider.get("service"),
                "price": provider.get("price"),
                "rating": provider.get("rating"),
                "slots": slots,
            }
    return shortlist


def apply_hard_limits(providers: list[dict[str, Any]], request: dict[str, Any]) -> dict[str, Any]:
    max_budget = field_value(request, "max_budget")

    eligible = []
    over_budget = 0
    no_slot_in_window = 0
    not_chosen = 0
    for provider in providers:
        if not _is_chosen(provider, request):
            not_chosen += 1
            continue
        if max_budget is not None and provider.get("price", 0) > max_budget:
            over_budget += 1
            continue
        slots = _slots_in_window(provider, request)
        if not slots:
            no_slot_in_window += 1
            continue
        attributes = provider.get("attributes", {})
        eligible.append(
            {
                "id": provider.get("id"),
                "name": provider.get("name"),
                "price": provider.get("price"),
                "currency": provider.get("currency"),
                "rating": provider.get("rating"),
                "review_count": provider.get("review_count"),
                "technician": attributes.get("technician"),
                "years_experience": attributes.get("years_experience"),
                "verified": attributes.get("verified"),
                "open_days": open_days(slots),
            }
        )

    removed = {"over_budget": over_budget, "no_slot_in_time_window": no_slot_in_window}
    found: dict[str, Any] = {
        "providers": eligible,
        "removed": removed,
        "note": "Only providers within the user's limits are listed. "
        "The final price is quoted when a slot is held, and may differ. "
        "Times are not listed: use get_open_slots for a provider and a day.",
    }
    if field_value(request, CHOSEN_PROVIDER) is not None:
        removed["not_the_past_provider"] = not_chosen
        if not eligible:
            found["past_provider_not_open"] = (
                "The provider from the user's past booking is not open within these details. Tell "
                'the user. Do not offer anyone else unless they agree, then send "provider_id" in '
                "clear_fields and search again."
            )
    return found
