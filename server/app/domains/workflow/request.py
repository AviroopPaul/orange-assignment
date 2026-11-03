"""An errand's details: which are needed, whether they are valid, and whether the user gave them.

These are pure functions, so each rule can be tested on its own.
"""

import re
from datetime import datetime, time
from typing import Any

REQUIRED_FOR_SEARCH = ("service", "area", "start_date", "end_date")
OPTIONAL_DETAILS = ("earliest_time", "latest_time", "max_budget", "preferences")
ALL_FIELDS = (*REQUIRED_FOR_SEARCH, *OPTIONAL_DETAILS)


def field_value(request: dict[str, Any], name: str) -> Any:
    entry = request.get(name)
    return entry.get("value") if isinstance(entry, dict) else None


def missing_required(request: dict[str, Any]) -> list[str]:
    return [name for name in REQUIRED_FOR_SEARCH if field_value(request, name) in (None, "")]


def optional_unset(request: dict[str, Any]) -> list[str]:
    return [name for name in OPTIONAL_DETAILS if field_value(request, name) in (None, "", [])]


def search_args(request: dict[str, Any]) -> dict[str, Any]:
    """The contract's inputs, built only from saved details. Incomplete until nothing is missing."""
    args: dict[str, Any] = {}
    if service := field_value(request, "service"):
        args["service"] = service
    if area := field_value(request, "area"):
        args["area"] = area
    start, end = field_value(request, "start_date"), field_value(request, "end_date")
    if start and end:
        args["date_range"] = {"start": start, "end": end}
    return args


def parse_time(value: str) -> time | None:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def _numbers_in(text: str) -> set[int]:
    """Amounts as people write them: 1500, 1,500, ₹1,500, 2k, 1.5k."""
    found = set()
    for digits, suffix in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", text, re.I):
        number = float(digits.replace(",", ""))
        found.add(round(number * 1000 if suffix else number))
    return found


def is_area_grounded(value: str, user_text: str) -> bool:
    """A real word of the area must appear in what the user wrote. Stops an invented area."""
    said = {word.casefold() for word in re.findall(r"\w+", user_text)}
    words = [word.casefold() for word in re.findall(r"\w+", value) if len(word) >= 3]
    return any(word in said for word in words)


def is_budget_grounded(value: int, user_text: str) -> bool:
    return value in _numbers_in(user_text)


def rejection(name: str, value: Any, user_text: str) -> str | None:
    """Why this value cannot be saved, or None if it can."""
    if name == "area":
        if not is_area_grounded(str(value), user_text):
            return "The user has not named this area. Ask which area they are in."
    elif name == "max_budget":
        if not is_budget_grounded(int(value), user_text):
            return "The user has not stated this budget. Ask them."
    elif name in ("start_date", "end_date"):
        return "Dates can only be set through resolve_dates, once the user confirms them."
    elif name in ("earliest_time", "latest_time"):
        if parse_time(str(value)) is None:
            return "Use the 24-hour time format HH:MM."
    return None


def only_remembered(value: Any, user_text: str, remembered: str) -> bool:
    """The detail is in what we remember, and not in anything said in this chat."""
    said = user_text.casefold()
    known = remembered.casefold()
    words = value if isinstance(value, list) else [value]
    return any(str(word).casefold() in known and str(word).casefold() not in said for word in words)


def pair_rejections(request: dict[str, Any]) -> dict[str, str]:
    """A rule across two fields, checked on the merged request."""
    problems = {}
    earliest = parse_time(str(field_value(request, "earliest_time") or ""))
    latest = parse_time(str(field_value(request, "latest_time") or ""))
    if earliest and latest and latest <= earliest:
        problems["latest_time"] = "The latest time must be after the earliest time."
    return problems
