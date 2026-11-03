"""Turns the user's words into exact dates. The model never works out a date itself.

Pure functions, so every rule can be tested against a fixed "today".
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

MAX_SEARCH_DAYS = 31

_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_CONNECTOR = re.compile(r"\s+(?:or|to|and|till|until|through)\s+")
_LEADING = re.compile(r"^(?:between|from|on|for|by|at|the)\s+")
# Small words that say nothing about when. Everything else must come from the user.
_FILLER = frozenset({"on", "the", "for", "of", "between", "and", "to", "or", "at", "from", "by"})

_NUMERIC = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DAY_MONTH = re.compile(rf"^(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})(?:\s+(\d{{4}}))?$")
_MONTH_DAY = re.compile(rf"^({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?$")
_ORDINAL = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)$")
_WEEKDAY = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_DAY_IN_WEEK = re.compile(
    rf"^(?:(?P<day>{_WEEKDAY})(?:\s+of)?\s+(?P<week>this|next)\s+week"
    rf"|(?P<week2>this|next)\s+week(?:\s+on)?\s+(?P<day2>{_WEEKDAY}))$"
)


@dataclass(frozen=True)
class ResolvedDates:
    start: date
    end: date
    label: str

    @property
    def shown_dates(self) -> tuple[str, ...]:
        """What the user must see, so a wrong reading is easy to spot."""
        if self.start == self.end:
            return (display(self.start),)
        return (display(self.start), display(self.end))


def display(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _single(name: str, day: date) -> ResolvedDates:
    return ResolvedDates(day, day, f"{name} ({display(day)})")


def _span(name: str, start: date, end: date) -> ResolvedDates:
    return ResolvedDates(start, end, f"{name} ({display(start)} to {display(end)})")


def _next_weekday(today: date, weekday: int) -> date:
    """The next possible one in the calendar, counting today."""
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _start_of_next_week(today: date) -> date:
    return today + timedelta(days=7 - today.weekday())


def _with_year(day: int, month: int, year: str | None, today: date) -> date | None:
    """With no year given, the next time that date comes round."""
    try:
        chosen = date(int(year) if year else today.year, month, day)
        if not year and chosen < today:
            chosen = date(today.year + 1, month, day)
    except ValueError:
        return None
    return chosen


def _explicit_date(text: str, today: date) -> date | None:
    if match := _ISO.match(text):
        return _with_year(int(match[3]), int(match[2]), match[1], today)
    if match := _NUMERIC.match(text):
        # Day first, as people write dates in India.
        return _with_year(int(match[1]), int(match[2]), match[3], today)
    if match := _DAY_MONTH.match(text):
        return _with_year(int(match[1]), _MONTHS[match[2]], match[3], today)
    if match := _MONTH_DAY.match(text):
        return _with_year(int(match[2]), _MONTHS[match[1]], match[3], today)
    return None


def _day_of_month(day: int, today: date) -> date | None:
    """Just "the 11th" means the next 11th to come round."""
    month, year = today.month, today.year
    if day < today.day:
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _full_name(weekday: int) -> str:
    return next(name.capitalize() for name, number in _WEEKDAYS.items() if number == weekday)


def _one_phrase(text: str, today: date) -> ResolvedDates | None:
    text = _LEADING.sub("", text).strip()
    this_sunday = today + timedelta(days=6 - today.weekday())
    next_monday = _start_of_next_week(today)

    if text == "today":
        return _single("Today", today)
    if text == "tomorrow":
        return _single("Tomorrow", today + timedelta(days=1))
    if text == "day after tomorrow":
        return _single("Day after tomorrow", today + timedelta(days=2))
    if text in ("this week", "any day", "any day this week", "anytime this week"):
        return _span("This week", today, this_sunday)
    if text in ("next week", "any day next week"):
        return _span("Next week", next_monday, next_monday + timedelta(days=6))
    if text in ("weekend", "this weekend"):
        if today.weekday() == 6:
            return _single("This weekend", today)
        saturday = _next_weekday(today, 5)
        return _span("This weekend", saturday, saturday + timedelta(days=1))
    if text == "next weekend":
        # The coming one, never today. Anyone who means a later one gives the exact dates.
        saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
        return _span("Next weekend", saturday, saturday + timedelta(days=1))

    if match := _DAY_IN_WEEK.match(text):
        weekday = _WEEKDAYS[match["day"] or match["day2"]]
        week = match["week"] or match["week2"]
        monday = next_monday if week == "next" else today - timedelta(days=today.weekday())
        return _single(f"{_full_name(weekday)} {week} week", monday + timedelta(days=weekday))
    if match := _ORDINAL.match(text):
        ordinal = _day_of_month(int(match[1]), today)
        return _single(ordinal.strftime("%A"), ordinal) if ordinal else None

    words = text.split()
    if words and words[-1] in _WEEKDAYS and len(words) <= 2:
        weekday = _WEEKDAYS[words[-1]]
        full_name = _full_name(weekday)
        if len(words) == 1 or words[0] in ("this", "coming"):
            return _single(f"This {full_name}", _next_weekday(today, weekday))
        if words[0] == "next":
            # The next one coming, but never today: today is not "next".
            days_ahead = (weekday - today.weekday()) % 7 or 7
            return _single(f"Next {full_name}", today + timedelta(days=days_ahead))
        return None

    if day := _explicit_date(text, today):
        return _single(day.strftime("%A"), day)
    return None


def resolve_phrase(phrase: str, today: date) -> ResolvedDates | None:
    """None means the words were not understood, so the agent must ask for a clearer date."""
    text = re.sub(r"\s+", " ", phrase.strip().casefold().replace(",", " ")).strip()
    parts = _CONNECTOR.split(_LEADING.sub("", text))
    if len(parts) == 1:
        return _one_phrase(parts[0], today)

    resolved = [_one_phrase(part, today) for part in parts]
    if any(item is None for item in resolved):
        return None
    start = min(item.start for item in resolved if item)
    end = max(item.end for item in resolved if item)
    return _span(f"{start:%A} to {end:%A}", start, end)


def problem_with(resolved: ResolvedDates, today: date) -> str | None:
    if resolved.start < today:
        return f"That date has passed. Today is {display(today)}. Ask for another date."
    if (resolved.end - resolved.start).days > MAX_SEARCH_DAYS:
        return f"Search at most {MAX_SEARCH_DAYS} days at a time. Ask for a shorter range."
    return None


def time_is_stated(at: str, user_text: str) -> bool:
    """A time the user really gave, as "10am", "at 10", "10:00" or a bare "10" after being asked.

    "The same time" also counts. Nothing else does, so the model cannot pick a time for them.
    """
    text = user_text.casefold()
    hour, minute = int(at[:2]), int(at[3:])
    if "same time" in text or re.search(rf"\b0?{hour:d}[:.]{minute:02d}\b", text):
        return True
    if minute != 0:
        return False
    if (hour == 12 and "noon" in text) or (hour == 0 and "midnight" in text):
        return True
    twelve = hour % 12 or 12
    side = "a" if hour < 12 else "p"
    return bool(
        re.search(rf"\b{twelve}\s*{side}\.?m\b", text)
        or re.search(rf"\b(?:at|around|about|by)\s+(?:{twelve}|{hour})\b(?!\s*[ap]\.?m)", text)
        or re.search(rf"\b(?:{twelve}|{hour})\s*o'?clock\b", text)
        or re.search(rf"^\s*(?:{twelve}|{hour})\s*$", text, re.MULTILINE)
    )


def is_grounded(phrase: str, user_text: str) -> bool:
    """Every meaningful word must be the user's own. No date mentioned means nothing passes."""
    said = {word.casefold() for word in re.findall(r"\w+", user_text)}
    words = [word.casefold() for word in re.findall(r"\w+", phrase)]
    meaningful = [word for word in words if word not in _FILLER]
    return bool(meaningful) and all(word in said for word in meaningful)
