"""I1 to I8. They read rows, so they hold whatever the reply said, on every scenario.

Each check takes a Snapshot and returns what it found wrong. Tests plant a violation of each one,
because a checker that never fails proves nothing.
"""

import json
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.domains.bookings.matching import claimed_actions
from app.domains.bookings.models import Approval, ApprovalStatus, AttemptStatus, BookingAttempt
from app.domains.bookings.service import card_of
from app.domains.bookings.verification import facts_of
from app.domains.chat.schemas import Outcome
from app.domains.gateway.sanitize import scan_for_injection
from app.domains.jobs.models import JobStatus, OutboxJob
from app.domains.memory.models import ContextLog, TurnView
from app.domains.notifications.models import Notification
from app.domains.proactive.models import BookingDisruption, DisruptionStatus
from app.domains.tracing.models import Run, TraceEvent
from mock_provider.catalog import CATALOG
from mock_provider.models import BookAttempt, Booking, KeyedResult

DESCRIPTIONS = {
    "I1": "Every side effect has one consumed approval with a matching hash.",
    "I2": "At most one booking per idempotency key, and per user, provider and slot.",
    "I3": "No success reply exists without a verified status before it.",
    "I4": "No forgotten, superseded or unconfirmed marker appears in any logged context.",
    "I5": "Every logged context is within the token budget.",
    "I6": "Every run has a complete trace, one final outcome and a user-facing reply.",
    "I7": "No provider free text appears on an approval card.",
    "I8": "No outbox row or open attempt remains unresolved when a scenario ends.",
}
ACTIVE_AT_PROVIDER = "confirmed"
VERIFIED_NOTICES = {
    "booking_verified": AttemptStatus.BOOKED.value,
    "booking_cancelled": AttemptStatus.CANCELLED.value,
    "booking_rescheduled": AttemptStatus.RESCHEDULED.value,
}
VERIFIED_ACTIONS = {
    AttemptStatus.BOOKED.value: "book",
    AttemptStatus.CANCELLED.value: "cancel",
    AttemptStatus.RESCHEDULED.value: "reschedule",
}
# Reading these tools in a run makes a statement about bookings fair.
SOURCES_OF_TRUTH = {"list_bookings", "get_booking_status", "use_past_provider"}
_REVIEW_TEXTS = {review.text for provider in CATALOG.values() for review in provider.reviews}


@dataclass
class Snapshot:
    approvals: list[Approval] = field(default_factory=list)
    attempts: list[BookingAttempt] = field(default_factory=list)
    outbox: list[OutboxJob] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    contexts: list[ContextLog] = field(default_factory=list)
    turns: list[TurnView] = field(default_factory=list)
    disruptions: list[BookingDisruption] = field(default_factory=list)
    bookings: list[Booking] = field(default_factory=list)
    book_attempts: list[BookAttempt] = field(default_factory=list)
    keyed_results: list[KeyedResult] = field(default_factory=list)
    trace_dir: Path | None = None
    budget: int = 8000
    # What this scenario knows must never reach the model.
    forbidden: tuple[str, ...] = ()


def _approval_for_key(snapshot: Snapshot, key: str) -> Approval | None:
    _, _, approval_id = key.partition(":")
    try:
        wanted = uuid.UUID(approval_id)
    except ValueError:
        return None
    return next((row for row in snapshot.approvals if row.id == wanted), None)


def _check_approval(snapshot: Snapshot, key: str, action: str, user_id: str | None) -> list[str]:
    approval = _approval_for_key(snapshot, key)
    if approval is None:
        return [f"{action} with key {key} has no approval"]
    found = []
    if approval.status != ApprovalStatus.CONSUMED.value:
        found.append(f"{action} with key {key} used an approval that is {approval.status}")
    if approval.action != action:
        found.append(f"{action} with key {key} used an approval for {approval.action}")
    if user_id is not None and approval.user_id != user_id:
        found.append(f"{action} with key {key} used another user's approval")
    if facts_of(approval).hash() != approval.approval_hash:
        found.append(f"{action} with key {key} has an approval whose hash does not match")
    attempts = [row for row in snapshot.attempts if row.approval_id == approval.id]
    if len(attempts) != 1:
        found.append(f"approval {approval.id} drove {len(attempts)} attempts")
    return found


def i1_side_effects_have_approval(snapshot: Snapshot) -> list[str]:
    found = []
    moved = {row.booking_id for row in snapshot.keyed_results if row.operation == "reschedule"}
    for booking in snapshot.bookings:
        # Seeded demo bookings were made outside this system, and carry no key.
        if booking.idempotency_key is None:
            continue
        found += _check_approval(snapshot, booking.idempotency_key, "book", booking.user_id)
        approval = _approval_for_key(snapshot, booking.idempotency_key)
        if approval is None:
            continue
        if booking.price != approval.price or booking.provider_id != approval.provider_id:
            found.append(f"booking {booking.id} differs from what was approved")
        if booking.id not in moved and booking.slot != approval.slot:
            found.append(f"booking {booking.id} is at a slot nobody approved")
    for attempt in snapshot.book_attempts:
        found += _check_approval(snapshot, attempt.idempotency_key, "book", attempt.user_id)
    for result in snapshot.keyed_results:
        found += _check_approval(snapshot, result.idempotency_key, result.operation, None)
    return sorted(set(found))


def i2_no_duplicate_bookings(snapshot: Snapshot) -> list[str]:
    found = []
    keys = Counter(row.idempotency_key for row in snapshot.bookings if row.idempotency_key)
    found += [f"key {key} made {count} bookings" for key, count in keys.items() if count > 1]
    slots = Counter(
        (row.user_id, row.provider_id, row.slot)
        for row in snapshot.bookings
        if row.status == ACTIVE_AT_PROVIDER
    )
    found += [f"{slot} has {count} active bookings" for slot, count in slots.items() if count > 1]
    return found


def _check_notice(snapshot: Snapshot, notice: Notification) -> list[str]:
    wanted = VERIFIED_NOTICES[notice.kind]
    booking_id = notice.data.get("booking_id")
    attempt = next(
        (
            row
            for row in snapshot.attempts
            if row.status == wanted
            and row.booking_id == booking_id
            and row.updated_at <= notice.created_at
        ),
        None,
    )
    if attempt is None:
        return [f"{notice.kind} notice {notice.id} has no verified attempt before it"]
    checked = any(
        event.run_id == attempt.run_id and event.kind == "booking_status_checked"
        for event in snapshot.events
    )
    if not checked:
        return [f"{notice.kind} notice {notice.id} came with no status check in its run"]
    booking = next((row for row in snapshot.bookings if row.id == booking_id), None)
    if booking is None:
        return [f"{notice.kind} notice {notice.id} names a booking the provider does not have"]
    if wanted == AttemptStatus.CANCELLED.value and booking.status != "cancelled":
        return [f"cancel notice {notice.id}, but the provider says {booking.status}"]
    return []


def _written_by_code(run_id: uuid.UUID, snapshot: Snapshot) -> bool:
    return any(
        event.run_id == run_id and event.kind == "outcome" and "settled_by" in event.data
        for event in snapshot.events
    )


def i3_success_is_verified(snapshot: Snapshot) -> list[str]:
    found = []
    for notice in snapshot.notifications:
        if notice.kind in VERIFIED_NOTICES:
            found += _check_notice(snapshot, notice)
    for run in snapshot.runs:
        if _written_by_code(run.id, snapshot):
            continue
        for action in claimed_actions(run.reply or ""):
            read_truth = any(
                event.run_id == run.id
                and event.kind == "tool_call"
                and event.name in SOURCES_OF_TRUTH
                for event in snapshot.events
            )
            verified = any(
                VERIFIED_ACTIONS.get(row.status) == action
                and row.session_id == run.session_id
                and (run.finished_at is None or row.updated_at <= run.finished_at)
                for row in snapshot.attempts
            )
            if not read_truth and not verified:
                found.append(f"run {run.id} claims {action} with nothing verified")
    return found


def i4_no_forbidden_marker(snapshot: Snapshot) -> list[str]:
    found = []
    for context in snapshot.contexts:
        payload = json.dumps(context.payload, default=str)
        found += [
            f"context {context.id} holds {marker}"
            for marker in snapshot.forbidden
            if marker in payload
        ]
    return found


def i5_within_budget(snapshot: Snapshot) -> list[str]:
    found = []
    logged = Counter(row.run_id for row in snapshot.contexts)
    calls = Counter(row.run_id for row in snapshot.events if row.kind == "model_call")
    for context in snapshot.contexts:
        if context.estimated_tokens > snapshot.budget or context.budget != snapshot.budget:
            found.append(
                f"context {context.id} used {context.estimated_tokens} of {context.budget}"
            )
    for run_id, count in calls.items():
        if logged[run_id] < count:
            found.append(f"run {run_id} made {count} model calls and logged {logged[run_id]}")
    return found


def _file_seqs(trace_dir: Path, run_id: uuid.UUID) -> set[int] | None:
    path = trace_dir / f"{run_id}.jsonl"
    if not path.exists():
        return None
    return {json.loads(line)["seq"] for line in path.read_text().splitlines()}


def i6_every_run_is_complete(snapshot: Snapshot) -> list[str]:
    found = []
    outcomes = {outcome.value for outcome in Outcome}
    for run in snapshot.runs:
        if run.finished_at is None or run.outcome is None:
            found.append(f"run {run.id} has no final outcome")
        elif run.outcome not in outcomes:
            found.append(f"run {run.id} ended as {run.outcome}")
        if not (run.reply or "").strip():
            found.append(f"run {run.id} has no reply")
        seqs = sorted(event.seq for event in snapshot.events if event.run_id == run.id)
        if seqs != list(range(1, len(seqs) + 1)):
            found.append(f"run {run.id} has a broken trace: {seqs}")
        if not any(e.run_id == run.id and e.kind == "outcome" for e in snapshot.events):
            found.append(f"run {run.id} has no outcome event")
        if snapshot.trace_dir is not None:
            in_file = _file_seqs(snapshot.trace_dir, run.id)
            if in_file is None:
                found.append(f"run {run.id} has no trace file")
            elif not set(seqs) <= in_file:
                found.append(f"run {run.id}: its trace file lacks {sorted(set(seqs) - in_file)}")
    return found


def _card_text(approval: Approval) -> list[str]:
    card = card_of(approval)
    return [
        card.provider_name,
        card.service,
        card.slot,
        card.slot_label,
        card.old_slot_label or "",
        card.status,
        *card.notes,
    ]


def i7_clean_cards(snapshot: Snapshot) -> list[str]:
    found = []
    for approval in snapshot.approvals:
        for text in _card_text(approval):
            if scan_for_injection(text) or any(
                review in text or (len(text) > 20 and text in review) for review in _REVIEW_TEXTS
            ):
                found.append(f"approval {approval.id} shows provider text: {text[:60]}")
    return found


def i8_nothing_left_open(snapshot: Snapshot) -> list[str]:
    found = []
    closed = {JobStatus.COMPLETED.value, JobStatus.FAILED.value}
    found += [
        f"job {job.id} ({job.job_name}) is still {job.status}"
        for job in snapshot.outbox
        if job.status not in closed
    ]
    found += [
        f"attempt {row.id} is still open"
        for row in snapshot.attempts
        if row.status == AttemptStatus.OPEN.value
    ]
    found += [
        f"disruption {row.id} is still waiting"
        for row in snapshot.disruptions
        if row.status == DisruptionStatus.DETECTED.value
    ]
    return found


CHECKS: dict[str, Callable[[Snapshot], list[str]]] = {
    "I1": i1_side_effects_have_approval,
    "I2": i2_no_duplicate_bookings,
    "I3": i3_success_is_verified,
    "I4": i4_no_forbidden_marker,
    "I5": i5_within_budget,
    "I6": i6_every_run_is_complete,
    "I7": i7_clean_cards,
    "I8": i8_nothing_left_open,
}


def check_all(snapshot: Snapshot) -> dict[str, list[str]]:
    return {name: check(snapshot) for name, check in CHECKS.items()}
