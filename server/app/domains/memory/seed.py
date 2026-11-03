"""60 memories and a long prior chat, as the brief asks. Safe to run again.

Every value holds a unique marker, so a test can search each logged context for it.
"""

from datetime import datetime, timedelta

from app.core.clock import Clock
from app.core.database import Database
from app.domains.memory.models import (
    ALL_SCOPES,
    Memory,
    MemorySource,
    MemoryStatus,
    TurnView,
)
from app.domains.memory.repository import MemoryRepository

SEED_USER = "seed-user"
SEED_SESSION = "seed-chat"
SERVICES = ("plumber", "electrician", "ac_servicing", "carpenter", "house_cleaning")
CHAT_EXCHANGES = 120
# Said in the old chat as well, so forgetting it must clean the chat too.
FORGET_ME_KEY = "gate_code"
FORGET_ME_MARKER = "MARKER-FORGET-ME-4417"
UNCONFIRMED_MARKERS = tuple(f"MARKER-UNCONFIRMED-{n:02}" for n in range(8))
SUPERSEDED_MARKERS = tuple(f"MARKER-SUPERSEDED-{n:02}" for n in range(6))
FORGOTTEN_MARKERS = tuple(f"MARKER-FORGOTTEN-{n:02}" for n in range(6))


def _row(key: str, value: str, scope: str, at: datetime, **extra: object) -> Memory:
    fields: dict = {
        "source": MemorySource.EXPLICIT.value,
        "status": MemoryStatus.ACTIVE.value,
        "quote": None,
        "session_id": SEED_SESSION,
        **extra,
    }
    return Memory(
        user_id=SEED_USER, key=key, value=value, scope=scope, created_at=at, updated_at=at, **fields
    )


def seed_rows(now: datetime) -> list[Memory]:
    rows = [_row(FORGET_ME_KEY, FORGET_ME_MARKER, ALL_SCOPES, now - timedelta(hours=1))]
    for n in range(9):
        rows.append(
            _row(f"general_{n}", f"MARKER-GENERAL-{n:02}", ALL_SCOPES, now - timedelta(days=n + 1))
        )
    for service in SERVICES:
        for n in range(6):
            rows.append(
                _row(
                    f"note_{n}",
                    f"MARKER-{service.upper()}-{n:02}",
                    service,
                    now - timedelta(days=n + 2),
                )
            )
    for n, marker in enumerate(UNCONFIRMED_MARKERS):
        rows.append(
            _row(
                f"guess_{n}",
                marker,
                ALL_SCOPES,
                now,
                source=MemorySource.INFERRED.value,
                quote="a review said so",
            )
        )
    for n, marker in enumerate(SUPERSEDED_MARKERS):
        rows.append(_row(f"old_{n}", marker, ALL_SCOPES, now, status=MemoryStatus.SUPERSEDED.value))
    for n, marker in enumerate(FORGOTTEN_MARKERS):
        rows.append(_row(f"gone_{n}", marker, ALL_SCOPES, now, status=MemoryStatus.FORGOTTEN.value))
    return rows


def seed_chat(now: datetime) -> list[TurnView]:
    filler = "Please keep the details of this old errand in mind while we sort it out together. "
    rows = []
    for n in range(CHAT_EXCHANGES):
        at = now - timedelta(minutes=CHAT_EXCHANGES - n)
        said = f"Old message {n}. {filler}"
        if n == 3:
            said += f"My gate code is {FORGET_ME_MARKER}."
        rows.append(
            TurnView(
                user_id=SEED_USER, session_id=SEED_SESSION, role="user", text=said, created_at=at
            )
        )
        rows.append(
            TurnView(
                user_id=SEED_USER,
                session_id=SEED_SESSION,
                role="assistant",
                text=f"Old reply {n}. {filler}",
                created_at=at,
            )
        )
    return rows


async def seed_memories(db: Database, clock: Clock) -> bool:
    """False if the seed was already there."""
    async with db.session_scope() as session:
        repository = MemoryRepository(session)
        if await repository.has_any(SEED_USER):
            return False
        now = clock.now()
        session.add_all(seed_rows(now))
        session.add_all(seed_chat(now))
    return True
