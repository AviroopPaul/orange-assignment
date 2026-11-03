"""What `book` does: 70% success, 15% slot taken, 15% timeout, of which half really booked."""

import hashlib
from enum import StrEnum


class BookOutcome(StrEnum):
    SUCCESS = "success"
    SLOT_TAKEN = "slot_taken"
    TIMEOUT_BOOKED = "timeout_booked"
    TIMEOUT_NOT_BOOKED = "timeout_not_booked"


def decide_book_outcome(seed: int, idempotency_key: str, attempt: int) -> BookOutcome:
    """Comes from a hash, so the result does not depend on the order of calls."""
    digest = hashlib.sha256(f"{seed}:{idempotency_key}:{attempt}".encode()).hexdigest()
    bucket = int(digest, 16) % 1000
    if bucket < 700:
        return BookOutcome.SUCCESS
    if bucket < 850:
        return BookOutcome.SLOT_TAKEN
    if bucket < 925:
        return BookOutcome.TIMEOUT_BOOKED
    return BookOutcome.TIMEOUT_NOT_BOOKED
