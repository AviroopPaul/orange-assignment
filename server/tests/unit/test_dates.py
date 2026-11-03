"""The date resolver against a fixed today: Tuesday, 3 November 2026."""

from datetime import date

import pytest

from app.domains.workflow.dates import display, is_grounded, problem_with, resolve_phrase

TODAY = date(2026, 11, 3)


@pytest.mark.parametrize(
    ("phrase", "start", "end", "label"),
    [
        ("today", "2026-11-03", "2026-11-03", "Today (03/11/2026)"),
        ("tomorrow", "2026-11-04", "2026-11-04", "Tomorrow (04/11/2026)"),
        ("Tomorrow", "2026-11-04", "2026-11-04", "Tomorrow (04/11/2026)"),
        ("day after tomorrow", "2026-11-05", "2026-11-05", "Day after tomorrow (05/11/2026)"),
        ("Saturday", "2026-11-07", "2026-11-07", "This Saturday (07/11/2026)"),
        ("on Saturday", "2026-11-07", "2026-11-07", "This Saturday (07/11/2026)"),
        ("this saturday", "2026-11-07", "2026-11-07", "This Saturday (07/11/2026)"),
        ("sat", "2026-11-07", "2026-11-07", "This Saturday (07/11/2026)"),
        ("coming Friday", "2026-11-06", "2026-11-06", "This Friday (06/11/2026)"),
        ("next Saturday", "2026-11-07", "2026-11-07", "Next Saturday (07/11/2026)"),
        ("next Monday", "2026-11-09", "2026-11-09", "Next Monday (09/11/2026)"),
        ("this week", "2026-11-03", "2026-11-08", "This week (03/11/2026 to 08/11/2026)"),
        ("any day this week", "2026-11-03", "2026-11-08", "This week (03/11/2026 to 08/11/2026)"),
        ("next week", "2026-11-09", "2026-11-15", "Next week (09/11/2026 to 15/11/2026)"),
        ("this weekend", "2026-11-07", "2026-11-08", "This weekend (07/11/2026 to 08/11/2026)"),
        ("next weekend", "2026-11-07", "2026-11-08", "Next weekend (07/11/2026 to 08/11/2026)"),
    ],
)
def test_common_phrases(phrase: str, start: str, end: str, label: str) -> None:
    resolved = resolve_phrase(phrase, TODAY)
    assert resolved is not None
    assert (resolved.start.isoformat(), resolved.end.isoformat()) == (start, end)
    assert resolved.label == label


def test_weekday_is_the_next_possible_one_in_the_calendar() -> None:
    resolved = resolve_phrase("Monday", TODAY)
    assert resolved is not None and resolved.start == date(2026, 11, 9)


def test_next_weekday_is_the_one_coming_up_not_a_week_later() -> None:
    resolved = resolve_phrase("next Friday", TODAY)
    assert resolved is not None and resolved.start == date(2026, 11, 6)


def test_next_on_that_same_weekday_means_a_week_from_today() -> None:
    resolved = resolve_phrase("next Tuesday", TODAY)
    assert resolved is not None and resolved.start == date(2026, 11, 10)


def test_todays_weekday_means_today() -> None:
    resolved = resolve_phrase("Tuesday", TODAY)
    assert resolved is not None and resolved.start == TODAY


@pytest.mark.parametrize(
    ("today", "saturday", "sunday"),
    [
        (date(2026, 11, 3), date(2026, 11, 7), date(2026, 11, 8)),  # Tuesday
        (date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)),  # Friday
        (date(2026, 11, 7), date(2026, 11, 14), date(2026, 11, 15)),  # Saturday: never today
        (date(2026, 11, 8), date(2026, 11, 14), date(2026, 11, 15)),  # Sunday: never today
    ],
)
def test_next_weekend_is_the_coming_one_and_never_today(
    today: date, saturday: date, sunday: date
) -> None:
    resolved = resolve_phrase("next weekend", today)
    assert resolved is not None
    assert (resolved.start, resolved.end) == (saturday, sunday)


def test_a_later_weekend_must_be_given_exactly() -> None:
    assert resolve_phrase("the weekend after next", date(2026, 11, 3)) is None
    exact = resolve_phrase("14 Nov to 15 Nov", date(2026, 11, 3))
    assert exact is not None
    assert (exact.start, exact.end) == (date(2026, 11, 14), date(2026, 11, 15))


def test_weekend_on_a_sunday_is_only_today() -> None:
    sunday = date(2026, 11, 8)
    resolved = resolve_phrase("this weekend", sunday)
    assert resolved is not None
    assert (resolved.start, resolved.end) == (sunday, sunday)


def test_this_week_on_a_sunday_is_one_day() -> None:
    sunday = date(2026, 11, 8)
    resolved = resolve_phrase("this week", sunday)
    assert resolved is not None and resolved.start == resolved.end == sunday


def test_saturday_on_a_saturday_means_today() -> None:
    saturday = date(2026, 11, 7)
    resolved = resolve_phrase("Saturday", saturday)
    assert resolved is not None and resolved.start == saturday


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("07/11/2026", date(2026, 11, 7)),
        ("7/11", date(2026, 11, 7)),
        ("07-11-2026", date(2026, 11, 7)),
        ("2026-11-07", date(2026, 11, 7)),
        ("7 Nov", date(2026, 11, 7)),
        ("7th November", date(2026, 11, 7)),
        ("7th of November 2026", date(2026, 11, 7)),
        ("November 7", date(2026, 11, 7)),
        ("Nov 7, 2026", date(2026, 11, 7)),
        ("the 7th of Nov", date(2026, 11, 7)),
    ],
)
def test_explicit_dates(phrase: str, expected: date) -> None:
    resolved = resolve_phrase(phrase, TODAY)
    assert resolved is not None
    assert resolved.start == resolved.end == expected
    assert resolved.label == f"{expected:%A} ({display(expected)})"


def test_day_comes_before_month() -> None:
    resolved = resolve_phrase("05/12", TODAY)
    assert resolved is not None and resolved.start == date(2026, 12, 5)


def test_date_with_no_year_means_the_next_time_it_comes_round() -> None:
    resolved = resolve_phrase("5 Jan", TODAY)
    assert resolved is not None and resolved.start == date(2027, 1, 5)


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("Thursday or Friday", date(2026, 11, 5), date(2026, 11, 6)),
        ("Friday or Thursday", date(2026, 11, 5), date(2026, 11, 6)),
        ("Thursday to Saturday", date(2026, 11, 5), date(2026, 11, 7)),
        ("between 5 Nov and 7 Nov", date(2026, 11, 5), date(2026, 11, 7)),
        ("3 Nov to 5 Nov", date(2026, 11, 3), date(2026, 11, 5)),
        ("tomorrow or Saturday", date(2026, 11, 4), date(2026, 11, 7)),
    ],
)
def test_ranges(phrase: str, start: date, end: date) -> None:
    resolved = resolve_phrase(phrase, TODAY)
    assert resolved is not None
    assert (resolved.start, resolved.end) == (start, end)
    assert display(start) in resolved.label and display(end) in resolved.label


@pytest.mark.parametrize(
    "phrase",
    [
        "soon",
        "whenever",
        "asap",
        "",
        "last Saturday",
        "32/13/2026",
        "31 Feb",
        "Saturday or whenever",
        "sometime next month",
    ],
)
def test_words_it_cannot_resolve_are_not_guessed(phrase: str) -> None:
    assert resolve_phrase(phrase, TODAY) is None


def test_label_always_shows_the_exact_date_in_day_first_form() -> None:
    resolved = resolve_phrase("Saturday", TODAY)
    assert resolved is not None
    assert resolved.shown_dates == ("07/11/2026",)
    range_ = resolve_phrase("this week", TODAY)
    assert range_ is not None
    assert range_.shown_dates == ("03/11/2026", "08/11/2026")


def test_past_date_is_a_problem() -> None:
    resolved = resolve_phrase("01/01/2026", TODAY)
    assert resolved is not None
    assert "passed" in (problem_with(resolved, TODAY) or "")


def test_today_is_not_a_problem() -> None:
    resolved = resolve_phrase("today", TODAY)
    assert resolved is not None
    assert problem_with(resolved, TODAY) is None


def test_very_long_range_is_a_problem() -> None:
    resolved = resolve_phrase("5 Nov to 5 Jan", TODAY)
    assert resolved is not None
    assert "31 days" in (problem_with(resolved, TODAY) or "")


@pytest.mark.parametrize(
    ("phrase", "said", "expected"),
    [
        ("tomorrow", "I need it tomorrow please", True),
        ("Saturday", "on saturday", True),
        ("on Saturday", "Saturday works", True),
        ("next Saturday", "I said next saturday", True),
        ("5 Nov to 7 Nov", "from 5 nov to 7 nov", True),
        ("tomorrow", "I need a plumber", False),
        ("this week", "I need a plumber", False),
        ("next Saturday", "on Saturday", False),
        ("Saturday", "", False),
        ("on the", "on the", False),
    ],
)
def test_dates_must_come_from_the_users_own_words(phrase: str, said: str, expected: bool) -> None:
    assert is_grounded(phrase, said) is expected
