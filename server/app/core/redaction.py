"""Removes a fact's text from anything we stored. Forgetting must reach every copy."""

import re
from collections.abc import Iterable
from typing import Any

REMOVED = "[removed]"


def compile_patterns(values: Iterable[str]) -> re.Pattern[str] | None:
    """Longest first, so a whole phrase goes before a part of it. Whole words only."""
    parts = sorted({value.strip() for value in values if value.strip()}, key=len, reverse=True)
    if not parts:
        return None
    escaped = (r"\s+".join(re.escape(word) for word in part.split()) for part in parts)
    return re.compile(rf"(?<!\w)(?:{'|'.join(escaped)})(?!\w)", re.IGNORECASE)


def scrub_text(text: str, pattern: re.Pattern[str] | None) -> str:
    return pattern.sub(REMOVED, text) if pattern else text


def scrub_value(value: Any, pattern: re.Pattern[str] | None) -> Any:
    """Every string inside a JSON-like value. Keys and other types stay as they are."""
    if pattern is None:
        return value
    if isinstance(value, str):
        return pattern.sub(REMOVED, value)
    if isinstance(value, dict):
        return {key: scrub_value(item, pattern) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, pattern) for item in value]
    return value
