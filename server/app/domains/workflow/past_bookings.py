"""Who did this service for the user before. Pure code, so "same as last time" cannot be guessed."""

import re
from typing import Any

_PAST_WORDS = re.compile(
    r"\b(?:same|last time|previous|previously|usual|again|regular)\b", re.IGNORECASE
)
# Only a visit that really happened counts. A cancelled booking is no "last time".
_DONE = "completed"


def refers_to_past(user_text: str) -> bool:
    return bool(_PAST_WORDS.search(user_text))


def _service_key(service: str) -> str:
    return re.sub(r"[\s-]+", "_", service.strip().casefold())


def past_providers(bookings: list[dict[str, Any]], service: str) -> list[dict[str, Any]]:
    """One entry per provider, latest visit first. Several entries mean the user must choose."""
    wanted = _service_key(service)
    latest: dict[str, dict[str, Any]] = {}
    for booking in bookings:
        if (
            booking.get("when") != "past"
            or booking.get("status") != _DONE
            or _service_key(str(booking.get("service"))) != wanted
        ):
            continue
        provider_id = str(booking.get("provider_id"))
        slot = str(booking.get("slot"))
        if provider_id not in latest or slot > latest[provider_id]["last_visit"]:
            latest[provider_id] = {
                "provider_id": provider_id,
                "provider": booking.get("provider_name"),
                "service": booking.get("service"),
                "last_visit": slot,
            }
    return sorted(latest.values(), key=lambda entry: entry["last_visit"], reverse=True)
