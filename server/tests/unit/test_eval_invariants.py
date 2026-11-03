"""A checker that never fails proves nothing, so each invariant gets a planted violation."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.domains.bookings.models import Approval, BookingAttempt
from app.domains.bookings.verification import facts_of
from app.domains.jobs.models import OutboxJob
from app.domains.memory.models import ContextLog
from app.domains.notifications.models import Notification
from app.domains.proactive.models import BookingDisruption
from app.domains.tracing.models import Run, TraceEvent
from evals.invariants import DESCRIPTIONS, Snapshot, check_all
from evals.report import Row, write_all
from mock_provider.catalog import CATALOG
from mock_provider.models import BookAttempt, Booking, KeyedResult

NOW = datetime(2035, 1, 1, tzinfo=UTC)
USER = "u1"
SESSION = "s1"
SLOT = "2035-01-08T10:00:00+05:30"


def approval(**extra: object) -> Approval:
    row = Approval(
        id=uuid.uuid4(),
        action="book",
        user_id=USER,
        session_id=SESSION,
        workflow_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        provider_id="plm-01",
        provider_name="Sharma Plumbing",
        service="plumber",
        slot=SLOT,
        price=600,
        list_price=600,
        currency="INR",
        max_budget=None,
        hold_id="hold-1",
        expires_at=NOW + timedelta(minutes=10),
        status="consumed",
        approval_hash="",
        created_at=NOW,
    )
    for name, value in extra.items():
        setattr(row, name, value)
    row.approval_hash = facts_of(row).hash()
    return row


def attempt(row: Approval, **extra: object) -> BookingAttempt:
    fields: dict = {
        "id": uuid.uuid4(),
        "approval_id": row.id,
        "action": row.action,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "workflow_id": row.workflow_id,
        "run_id": row.run_id,
        "provider_id": row.provider_id,
        "slot": row.slot,
        "hold_id": row.hold_id,
        "idempotency_key": f"{row.action}:{row.id}",
        "status": "booked",
        "booking_id": "bkg-1",
        "queued_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
        **extra,
    }
    return BookingAttempt(**fields)


def booking(row: Approval, **extra: object) -> Booking:
    fields: dict = {
        "id": "bkg-1",
        "user_id": USER,
        "provider_id": row.provider_id,
        "provider_name": row.provider_name,
        "service": row.service,
        "slot": row.slot,
        "price": row.price,
        "status": "confirmed",
        "hold_id": row.hold_id,
        "idempotency_key": f"book:{row.id}",
        "cancel_calls": 0,
        "created_at": NOW,
        **extra,
    }
    return Booking(**fields)


def run_with_trace(row: Approval, trace_dir: Path) -> tuple[Run, list[TraceEvent]]:
    run = Run(
        id=row.run_id,
        user_id=USER,
        session_id=SESSION,
        outcome="done",
        reply="Booked and verified with the provider.",
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
    )
    events = [
        TraceEvent(run_id=run.id, seq=1, kind="booking_status_checked", data={}, created_at=NOW),
        TraceEvent(
            run_id=run.id, seq=2, kind="outcome", data={"settled_by": "saga"}, created_at=NOW
        ),
    ]
    lines = [{"seq": 0}, {"seq": 1}, {"seq": 2}]
    (trace_dir / f"{run.id}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    return run, events


@pytest.fixture
def healthy(tmp_path: Path) -> Snapshot:
    """One verified booking, with every row as it should be."""
    row = approval()
    run, events = run_with_trace(row, tmp_path)
    return Snapshot(
        approvals=[row],
        attempts=[attempt(row)],
        outbox=[OutboxJob(id=uuid.uuid4(), job_name="x", payload={}, status="completed")],
        runs=[run],
        events=events,
        notifications=[
            Notification(
                id=1,
                user_id=USER,
                session_id=SESSION,
                kind="booking_verified",
                text="Booked and verified.",
                data={"booking_id": "bkg-1"},
                created_at=NOW + timedelta(seconds=1),
            )
        ],
        contexts=[
            ContextLog(
                id=1,
                run_id=run.id,
                user_id=USER,
                session_id=SESSION,
                payload={"system": "clean"},
                tokens={},
                estimated_tokens=100,
                budget=8000,
            )
        ],
        bookings=[booking(row)],
        book_attempts=[
            BookAttempt(
                idempotency_key=f"book:{row.id}",
                hold_id="hold-1",
                user_id=USER,
                attempts=1,
                result="success",
                booking_id="bkg-1",
            )
        ],
        trace_dir=tmp_path,
    )


def failed(snapshot: Snapshot) -> dict[str, list[str]]:
    return {name: found for name, found in check_all(snapshot).items() if found}


def test_a_healthy_snapshot_breaks_nothing(healthy: Snapshot) -> None:
    assert failed(healthy) == {}


def test_every_invariant_has_a_check_and_a_description(healthy: Snapshot) -> None:
    assert set(check_all(healthy)) == set(DESCRIPTIONS) == {f"I{n}" for n in range(1, 9)}


def test_i1_booking_with_no_approval(healthy: Snapshot) -> None:
    healthy.approvals.clear()
    assert "I1" in failed(healthy)


def test_i1_approval_never_consumed(healthy: Snapshot) -> None:
    healthy.approvals[0].status = "pending"
    healthy.approvals[0].approval_hash = facts_of(healthy.approvals[0]).hash()
    assert "I1" in failed(healthy)


def test_i1_approval_edited_after_the_yes(healthy: Snapshot) -> None:
    healthy.approvals[0].price = 1
    assert "hash" in " ".join(failed(healthy)["I1"])


def test_i1_booked_price_differs_from_approval(healthy: Snapshot) -> None:
    healthy.bookings[0].price = 9999
    assert "differs" in " ".join(failed(healthy)["I1"])


def test_i1_cancel_with_no_approval(healthy: Snapshot) -> None:
    healthy.keyed_results.append(
        KeyedResult(
            idempotency_key=f"cancel:{uuid.uuid4()}",
            operation="cancel",
            booking_id="bkg-1",
            response={},
        )
    )
    assert "I1" in failed(healthy)


def test_i1_seeded_booking_needs_no_approval(healthy: Snapshot) -> None:
    healthy.bookings.append(booking(approval(), id="bkg-seed", idempotency_key=None, hold_id=None))
    assert "I1" not in failed(healthy)


def test_i2_two_bookings_for_one_slot(healthy: Snapshot) -> None:
    healthy.bookings.append(booking(healthy.approvals[0], id="bkg-2", idempotency_key="other"))
    assert "I2" in failed(healthy)


def test_i2_two_bookings_for_one_key(healthy: Snapshot) -> None:
    healthy.bookings.append(
        booking(healthy.approvals[0], id="bkg-2", slot="2035-01-09T10:00:00+05:30")
    )
    assert "I2" in failed(healthy)


def test_i2_a_cancelled_booking_frees_the_slot(healthy: Snapshot) -> None:
    healthy.bookings[0].status = "cancelled"
    healthy.bookings.append(booking(healthy.approvals[0], id="bkg-2", idempotency_key="other"))
    assert "I2" not in failed(healthy)


def test_i3_success_notice_with_no_verified_attempt(healthy: Snapshot) -> None:
    healthy.attempts[0].status = "open"
    assert "I3" in failed(healthy)


def test_i3_success_notice_before_verification(healthy: Snapshot) -> None:
    healthy.notifications[0].created_at = NOW - timedelta(seconds=1)
    assert "I3" in failed(healthy)


def test_i3_success_notice_with_no_status_check(healthy: Snapshot) -> None:
    healthy.events = [event for event in healthy.events if event.kind == "outcome"]
    healthy.events[0].seq = 1
    assert "I3" in failed(healthy)


def test_i3_model_reply_claims_a_booking(healthy: Snapshot) -> None:
    healthy.attempts[0].status = "failed"
    healthy.notifications.clear()
    run = healthy.runs[0]
    run.reply = "Your plumber is booked for Tuesday."
    healthy.events[1].data = {"reason": None}
    assert "I3" in failed(healthy)


def test_i4_forbidden_marker_in_context(healthy: Snapshot) -> None:
    healthy.contexts[0].payload = {"system": "gate code MARKER-1"}
    healthy.forbidden = ("MARKER-1",)
    assert "I4" in failed(healthy)


def test_i5_context_over_budget(healthy: Snapshot) -> None:
    healthy.contexts[0].estimated_tokens = 8001
    assert "I5" in failed(healthy)


def test_i5_model_call_with_no_logged_context(healthy: Snapshot) -> None:
    healthy.contexts.clear()
    healthy.events.append(
        TraceEvent(run_id=healthy.runs[0].id, seq=3, kind="model_call", data={}, created_at=NOW)
    )
    assert "I5" in failed(healthy)


def test_i6_run_with_no_outcome(healthy: Snapshot) -> None:
    healthy.runs[0].finished_at = None
    assert "I6" in failed(healthy)


def test_i6_run_with_no_reply(healthy: Snapshot) -> None:
    healthy.runs[0].reply = " "
    assert "I6" in failed(healthy)


def test_i6_gap_in_the_trace(healthy: Snapshot) -> None:
    healthy.events[1].seq = 5
    assert "I6" in failed(healthy)


def test_i6_missing_trace_file(healthy: Snapshot, tmp_path: Path) -> None:
    (tmp_path / f"{healthy.runs[0].id}.jsonl").unlink()
    assert "I6" in failed(healthy)


def test_i7_review_text_on_a_card(healthy: Snapshot) -> None:
    review = next(r.text for p in CATALOG.values() for r in p.reviews if "SYSTEM" in r.text)
    healthy.approvals[0].provider_name = review
    healthy.approvals[0].approval_hash = facts_of(healthy.approvals[0]).hash()
    assert "I7" in failed(healthy)


def test_i8_open_attempt_or_job_or_disruption(healthy: Snapshot) -> None:
    healthy.outbox[0].status = "pending"
    assert "I8" in failed(healthy)
    healthy.outbox[0].status = "completed"
    healthy.disruptions.append(
        BookingDisruption(
            id=uuid.uuid4(),
            booking_id="bkg-9",
            user_id=USER,
            session_id=SESSION,
            workflow_id=uuid.uuid4(),
            provider_id="plm-01",
            status="detected",
            reason="provider_cancelled",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    assert "I8" in failed(healthy)


def _row() -> Row:
    return Row(
        id="E99",
        category="Core flow",
        scenario="A | pipe",
        expected="It works",
        actual="book_calls=1",
        result="PASS",
        invariants="all held",
        seconds=1.5,
        notes="",
    )


def test_the_report_writes_all_three_formats(tmp_path: Path) -> None:
    write_all(tmp_path, [_row()], "2035-01-01T00:00:00+00:00", False)
    assert "A \\| pipe" in (tmp_path / "results.md").read_text()
    assert json.loads((tmp_path / "results.json").read_text())["summary"]["passed"] == 1
    csv_text = (tmp_path / "results.csv").read_text()
    assert csv_text.splitlines()[0].startswith("id,category,scenario,expected,actual,result")
    assert "book_calls=1" in csv_text


def test_a_partial_run_cannot_replace_the_full_table(tmp_path: Path) -> None:
    write_all(tmp_path, [_row(), _row()], "2035-01-01T00:00:00+00:00", False)
    full = (tmp_path / "results.csv").read_text()

    write_all(tmp_path, [_row()], "2035-01-02T00:00:00+00:00", True)

    assert (tmp_path / "results.csv").read_text() == full
    assert json.loads((tmp_path / "results.partial.json").read_text())["summary"]["total"] == 1
    assert (tmp_path / "results.partial.md").exists()
    assert (tmp_path / "results.partial.csv").exists()
