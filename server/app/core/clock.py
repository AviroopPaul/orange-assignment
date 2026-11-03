"""Time as a dependency, so tests and evals can move it."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

# The service runs for customers in India, so "today" and "Saturday" mean Indian time.
LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class OffsetClock:
    """Real time, moved by a fixed amount. Every eval process starts from the same fake day."""

    def __init__(self, offset_s: float) -> None:
        self._offset = timedelta(seconds=offset_s)

    def now(self) -> datetime:
        return datetime.now(UTC) + self._offset
