"""Turns the user's words into exact dates. The model never works out a date itself.

Pure functions, so every rule can be tested against a fixed "today".
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

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

_AFFIRMATIVE = re.compile(
    r"^(yes|yep|yeah|yup|y|ok|okay|sure|correct|right|confirm|confirmed|perfect|fine|"
    r"that'?s right|that works|sounds good|looks good|go ahead)\b"
)
_NEGATIVE = re.compile(
    r"^(no|nope|nah|wrong|that'?s wrong|not (that|this|those|these|ok|okay|right|correct))\b"
)
_CONTRARY = re.compile(r"\b(but|no|not|instead|change|actually|wrong|except|however|different)\b")
_HEDGE = re.compile(
    r"\b(maybe|perhaps|probably|possibly|i guess|i think|i suppose|not sure|unsure|hmm+|"
    r"kind of|sort of|dunno|don'?t know|no idea)\b"
)


class DateAnswer(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCLEAR = "unclear"
    OTHER = "other"


@dataclass(frozen=True)
class ResolvedDates:
    start: date
    end: date
    label: str

    @property
    def shown_dates(self) -> tuple[str, ...]:
        """What the user must see before they can confirm."""
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
        saturday = next_monday + timedelta(days=5)
        return _span("Next weekend", saturday, saturday + timedelta(days=1))

    words = text.split()
    if words and words[-1] in _WEEKDAYS and len(words) <= 2:
        weekday = _WEEKDAYS[words[-1]]
        full_name = next(
            name.capitalize() for name, number in _WEEKDAYS.items() if number == weekday
        )
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


def is_grounded(phrase: str, user_text: str) -> bool:
    """Every meaningful word must be the user's own. No date mentioned means nothing passes."""
    said = {word.casefold() for word in re.findall(r"\w+", user_text)}
    words = [word.casefold() for word in re.findall(r"\w+", phrase)]
    meaningful = [word for word in words if word not in _FILLER]
    return bool(meaningful) and all(word in said for word in meaningful)


def _normalized(message: str) -> str:
    return re.sub(r"\s+", " ", message.strip().casefold())


def read_answer(message: str, shown_dates: list[str], asked_alone: bool) -> DateAnswer:
    """What this message says about the proposed dates. Code decides this, not the model.

    A plain "yes" is clear only if the dates were the one question asked. After a reply with
    another question in it, that "yes" could mean either, so it is unclear.
    """
    text = _normalized(message)
    if _HEDGE.search(text):
        return DateAnswer.UNCLEAR
    if _NEGATIVE.match(text):
        return DateAnswer.REJECTED
    if _CONTRARY.search(text):
        return DateAnswer.OTHER
    if all(day in text for day in shown_dates):
        return DateAnswer.CONFIRMED
    if _AFFIRMATIVE.match(text):
        return DateAnswer.CONFIRMED if asked_alone else DateAnswer.UNCLEAR
    return DateAnswer.OTHER


def date_question(label: str) -> str:
    return f"Are these dates okay: {label}?"


def is_asked_alone(reply: str, shown_dates: list[str]) -> bool:
    """The reply shows the exact dates, and asks nothing else."""
    return reply.count("?") == 1 and all(day in reply for day in shown_dates)
