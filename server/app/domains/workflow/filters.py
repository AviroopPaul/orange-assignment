"""Hard limits, applied by code before the model sees any provider.

The model must not be trusted to apply a budget or a time window. It compares what is left. The
result is also kept small, because a raw search cost over 6,000 input tokens.
"""

from datetime import datetime
from typing import Any

from app.domains.workflow.request import field_value, parse_time

MAX_SLOTS_SHOWN = 6


def _in_window(slot: str, earliest: Any, latest: Any) -> bool:
    """The earliest time is included and the latest is not, so mornings end before 12:00."""
    try:
        starts_at = datetime.fromisoformat(slot).time()
    except ValueError:
        return False
    return (earliest is None or starts_at >= earliest) and (latest is None or starts_at < latest)


def apply_hard_limits(providers: list[dict[str, Any]], request: dict[str, Any]) -> dict[str, Any]:
    max_budget = field_value(request, "max_budget")
    earliest = parse_time(str(field_value(request, "earliest_time") or ""))
    latest = parse_time(str(field_value(request, "latest_time") or ""))

    eligible = []
    over_budget = 0
    no_slot_in_window = 0
    for provider in providers:
        if max_budget is not None and provider.get("price", 0) > max_budget:
            over_budget += 1
            continue
        slots = [
            slot
            for slot in provider.get("available_slots", [])
            if _in_window(slot, earliest, latest)
        ]
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
                "slots": slots[:MAX_SLOTS_SHOWN],
                "more_slots": max(0, len(slots) - MAX_SLOTS_SHOWN),
            }
        )

    return {
        "providers": eligible,
        "removed": {"over_budget": over_budget, "no_slot_in_time_window": no_slot_in_window},
        "note": "Only providers within the user's limits are listed. "
        "The final price is quoted when a slot is held, and may differ.",
    }
