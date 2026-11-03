"""The booking path on real PostgreSQL, Redis, mock service and worker processes."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import delete, select

from app.container import Core, build_core
from app.core.clock import LOCAL_TZ, SystemClock
from app.core.config import Settings
from app.core.database import Database
from app.domains.bookings.models import ApprovalStatus, AttemptStatus, BookingAttempt
from app.domains.bookings.schemas import ApprovalCard, DecisionStatus
from app.domains.gateway.schemas import Caller
from app.domains.gateway.service import CallContext, ToolBudget
from app.domains.jobs.broker import build_broker
from app.domains.jobs.models import OutboxJob
from app.domains.workflow.repository import WorkflowRepository
from mock_provider import catalog
from mock_provider.catalog import CATALOG
from mock_provider.migrate import main as migrate_mock
from tests.conftest import make_ready

pytestmark = pytest.mark.integration

LEASE_S = 6
TOOL_TIMEOUT_S = 4
WORKER_ENV = {
    "JOB_TIME_LIMIT_S": "5",
    "JOB_LEASE_S": str(LEASE_S),
    "TOOL_TIMEOUT_S": str(TOOL_TIMEOUT_S),
    "BROKER_IDLE_TIMEOUT_S": "30",
    "BROKER_QUEUE_NAME": "errand.jobs.booking",
}


@pytest.fixture(scope="module", autouse=True)
def migrated_schema() -> None:
    command.upgrade(Config("alembic.ini"), "head")
    migrate_mock()


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mock_provider.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=os.environ.copy(),
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        yield url
    finally:
        process.terminate()
        process.wait()


@dataclass
class Rig:
    core: Core
    provider: httpx.AsyncClient
    user: str
    session: str
    settings: Settings

    async def scenario(self, **config: object) -> None:
        assert (await self.provider.post("/admin/scenario", json=config)).status_code == 204

    async def card(self) -> ApprovalCard:
        today = SystemClock().now().astimezone(LOCAL_TZ).date()
        first, last = today + timedelta(days=1), today + timedelta(days=6)
        phrase = f"{first:%-d %b} to {last:%-d %b}"
        workflows = self.core.workflows
        await make_ready(workflows, self.user, self.session, phrase)
        recorder = await self.core.traces.start_run(self.user, self.session)
        context = CallContext(self.user, Caller.MODEL, ToolBudget(12), recorder)
        args = await workflows.begin_search(self.user, self.session, recorder)
        found = await self.core.gateway.call("search_providers", args, context)
        assert found.ok, found
        await workflows.finish_search(self.user, self.session, found.data["providers"], recorder)
        async with self.core.db.session_scope() as scope:
            workflow = await WorkflowRepository(scope).get_active(self.user, self.session)
        provider_id = next(key for key in workflow.shortlist if key.endswith("-01"))
        result = await self.core.approvals.propose_booking(
            self.session, provider_id, workflow.shortlist[provider_id]["slots"][0], context
        )
        assert result.get("card_shown_to_user"), result
        view = await self.core.approvals.view(self.user, self.session)
        assert view.card is not None
        return view.card

    async def attempts(self) -> list[BookingAttempt]:
        async with self.core.db.session_scope() as session:
            rows = await session.scalars(
                select(BookingAttempt).where(BookingAttempt.user_id == self.user)
            )
            return list(rows)

    async def wait_for_attempt(self, check, limit_s: float = 25) -> BookingAttempt:  # type: ignore[no-untyped-def]
        async with asyncio.timeout(limit_s):
            while True:
                rows = await self.attempts()
                if rows and check(rows[0]):
                    return rows[0]
                await asyncio.sleep(0.1)

    async def provider_bookings(self) -> list[dict]:
        response = await self.provider.post(
            "/tools/list_bookings", json={"user_id": self.user}, headers={"X-User-Id": self.user}
        )
        return response.json()["bookings"]


@pytest.fixture
async def rig(server_url: str) -> AsyncIterator[Rig]:
    settings = Settings(
        mock_provider_url=server_url,
        tool_timeout_s=TOOL_TIMEOUT_S,
        job_time_limit_s=5,
        job_lease_s=LEASE_S,
        broker_idle_timeout_s=30,
        broker_queue_name="errand.jobs.booking",
    )
    broker = build_broker(settings)
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    await broker.startup()
    core = build_core(
        settings, broker=broker, redis=redis, db=Database(settings.errand_database_url)
    )
    # The test database outlives a run, and a job left by another test would be swept up here.
    async with core.db.session_scope() as session:
        await session.execute(delete(OutboxJob))
    async with httpx.AsyncClient(base_url=server_url, timeout=10) as provider:
        assert (await provider.post("/admin/reset")).status_code == 204
        yield Rig(core, provider, f"it-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex, settings)
    await broker.shutdown()
    await core.close()


@pytest.fixture
def workers(server_url: str) -> Iterator[list[subprocess.Popen]]:
    started: list[subprocess.Popen] = []
    yield started
    for process in started:
        kill(process)


def start_worker(started: list[subprocess.Popen], server_url: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "taskiq", "worker", "app.worker:broker", "--workers", "1"],
        env={**os.environ, **WORKER_ENV, "MOCK_PROVIDER_URL": server_url},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started.append(process)
    return process


def kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


async def test_ten_concurrent_confirmations_start_one_attempt(rig: Rig) -> None:
    """Separate connections, so the conditional update has to decide the winner."""
    card = await rig.card()
    answers = await asyncio.gather(
        *[rig.core.approvals.confirm_click(card.approval_id, rig.user) for _ in range(10)]
    )
    statuses = [answer.status for answer in answers]
    assert statuses.count(DecisionStatus.IN_PROGRESS) == 1
    assert statuses.count(DecisionStatus.ALREADY_CONFIRMED) == 9
    assert len(await rig.attempts()) == 1


async def test_a_real_worker_books_and_verifies(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    await rig.scenario(book_outcomes=["success"])
    card = await rig.card()
    start_worker(workers, server_url)
    await rig.core.approvals.confirm_click(card.approval_id, rig.user)

    attempt = await rig.wait_for_attempt(lambda row: row.status != AttemptStatus.OPEN.value)
    assert attempt.status == AttemptStatus.BOOKED.value
    bookings = await rig.provider_bookings()
    assert [booking["booking_id"] for booking in bookings] == [attempt.booking_id]

    notes = await rig.core.notifications.list_after(rig.user, rig.session)
    assert [note.kind for note in notes] == ["booking_verified"]
    trace = await rig.core.traces.get_trace(attempt.run_id)
    assert trace.outcome == "done"


async def test_worker_killed_during_book_is_resolved_by_status_with_one_booking(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    """The book call took effect and its reply never came. The worker dies while waiting for it."""
    await rig.scenario(book_outcomes=["timeout_booked"], timeout_hang_s=TOOL_TIMEOUT_S + 2)
    card = await rig.card()
    first = start_worker(workers, server_url)
    await rig.core.approvals.confirm_click(card.approval_id, rig.user)

    await rig.wait_for_attempt(lambda row: row.book_calls == 1)
    await asyncio.sleep(0.5)
    kill(first)
    assert (await rig.attempts())[0].status == AttemptStatus.OPEN.value
    assert await rig.core.notifications.list_after(rig.user, rig.session) == []

    start_worker(workers, server_url)
    await asyncio.sleep(LEASE_S + 0.5)
    await rig.core.jobs.sweep()
    attempt = await rig.wait_for_attempt(lambda row: row.status != AttemptStatus.OPEN.value)

    assert attempt.status == AttemptStatus.BOOKED.value
    assert attempt.book_calls == 1
    bookings = await rig.provider_bookings()
    assert [booking["booking_id"] for booking in bookings] == [attempt.booking_id]
    notes = await rig.core.notifications.list_after(rig.user, rig.session)
    assert [note.kind for note in notes] == ["booking_verified"]
    trace = await rig.core.traces.get_trace(attempt.run_id)
    assert [event.kind for event in trace.events].count("saga_claimed") == 2


async def test_redis_wipe_after_approval_still_books_once(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    await rig.scenario(book_outcomes=["success"])
    card = await rig.card()
    await rig.core.approvals.confirm_click(card.approval_id, rig.user)
    redis = Redis.from_url(rig.settings.redis_url)
    await redis.flushdb()
    await redis.aclose()

    start_worker(workers, server_url)
    await asyncio.sleep(LEASE_S + 0.5)
    await rig.core.jobs.sweep()
    attempt = await rig.wait_for_attempt(lambda row: row.status != AttemptStatus.OPEN.value)
    assert attempt.status == AttemptStatus.BOOKED.value
    assert len(await rig.provider_bookings()) == 1


async def test_price_change_after_approval_on_real_services(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    card = await rig.card()
    start = await rig.core.approvals.read(card.approval_id, rig.user)
    await rig.scenario(provider_prices={"plm-01": card.price + 900})
    start_worker(workers, server_url)
    await rig.core.approvals.confirm_click(card.approval_id, rig.user)

    attempt = await rig.wait_for_attempt(lambda row: row.status != AttemptStatus.OPEN.value)
    assert attempt.status == AttemptStatus.PRICE_CHANGED.value
    assert attempt.book_calls == 0
    assert await rig.provider_bookings() == []
    view = await rig.core.approvals.view(rig.user, rig.session)
    assert view.card is not None
    assert view.card.price == card.price + 900
    assert start.card.status == ApprovalStatus.PENDING.value


async def _booked_for_change(rig: Rig, workers: list[subprocess.Popen], server_url: str) -> dict:
    await rig.scenario(book_outcomes=["success"])
    card = await rig.card()
    start_worker(workers, server_url)
    await rig.core.approvals.confirm_click(card.approval_id, rig.user)
    await rig.wait_for_attempt(lambda row: row.status != AttemptStatus.OPEN.value)
    return (await rig.provider_bookings())[0]


async def _propose_cancel(rig: Rig, booking_id: str, chat: str) -> ApprovalCard:
    await rig.core.workflows.record_user_message(rig.user, chat, "cancel my booking")
    context = CallContext(rig.user, Caller.MODEL, ToolBudget(12))
    result = await rig.core.approvals.propose_cancel(chat, booking_id, context)
    assert result.get("card_shown_to_user"), result
    view = await rig.core.approvals.view(rig.user, chat)
    assert view.card is not None
    return view.card


async def _wait_for_change(rig: Rig, action: str, limit_s: float = 25) -> BookingAttempt:
    async with asyncio.timeout(limit_s):
        while True:
            for row in await rig.attempts():
                if row.action == action and row.status != AttemptStatus.OPEN.value:
                    return row
            await asyncio.sleep(0.1)


async def test_a_real_worker_cancels_through_the_503_and_verifies(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    booking = await _booked_for_change(rig, workers, server_url)
    chat = uuid.uuid4().hex
    card = await _propose_cancel(rig, booking["booking_id"], chat)

    await rig.core.approvals.confirm_click(card.approval_id, rig.user)

    attempt = await _wait_for_change(rig, "cancel")
    assert attempt.status == AttemptStatus.CANCELLED.value
    assert (await rig.provider_bookings())[0]["status"] == "cancelled"
    notes = await rig.core.notifications.list_after(rig.user, chat)
    assert [note.kind for note in notes] == ["booking_cancelled"]


async def test_ten_concurrent_cancel_confirmations_start_one_attempt(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    booking = await _booked_for_change(rig, workers, server_url)
    chat = uuid.uuid4().hex
    card = await _propose_cancel(rig, booking["booking_id"], chat)

    answers = await asyncio.gather(
        *[rig.core.approvals.confirm_click(card.approval_id, rig.user) for _ in range(10)]
    )

    statuses = [answer.status for answer in answers]
    assert statuses.count(DecisionStatus.IN_PROGRESS) == 1
    assert statuses.count(DecisionStatus.ALREADY_CONFIRMED) == 9
    assert len([row for row in await rig.attempts() if row.action == "cancel"]) == 1
    await _wait_for_change(rig, "cancel")


async def test_two_chats_cannot_change_one_booking_at_once(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    """Checks the partial unique index on PostgreSQL, with both confirmations at the same time."""
    booking = await _booked_for_change(rig, workers, server_url)
    first = await _propose_cancel(rig, booking["booking_id"], uuid.uuid4().hex)
    second = await _propose_cancel(rig, booking["booking_id"], uuid.uuid4().hex)

    answers = await asyncio.gather(
        rig.core.approvals.confirm_click(first.approval_id, rig.user),
        rig.core.approvals.confirm_click(second.approval_id, rig.user),
    )

    statuses = sorted(answer.status for answer in answers)
    assert statuses == sorted([DecisionStatus.IN_PROGRESS, DecisionStatus.NO_LONGER_VALID])
    assert len([row for row in await rig.attempts() if row.action == "cancel"]) == 1
    await _wait_for_change(rig, "cancel")


async def test_a_real_worker_keeps_the_old_booking_on_slot_conflict(
    rig: Rig, workers: list[subprocess.Popen], server_url: str
) -> None:
    booking = await _booked_for_change(rig, workers, server_url)
    chat = uuid.uuid4().hex
    # A slot this provider really works, or the provider refuses it before any conflict.
    start = SystemClock().now().astimezone(LOCAL_TZ).date() + timedelta(days=20)
    open_slots = [
        slot
        for offset in range(7)
        for slot in catalog.slots_on(CATALOG[booking["provider_id"]], start + timedelta(offset))
    ]
    chosen = datetime.fromisoformat(open_slots[0])
    phrase = f"{chosen.day} {chosen:%b}"
    await rig.core.workflows.record_user_message(
        rig.user, chat, f"move it to {phrase} at {chosen:%H:%M}"
    )
    context = CallContext(rig.user, Caller.MODEL, ToolBudget(12))
    result = await rig.core.approvals.propose_reschedule(
        chat, booking["booking_id"], phrase, f"{chosen:%H:%M}", context
    )
    assert result.get("card_shown_to_user"), result
    view = await rig.core.approvals.view(rig.user, chat)
    assert view.card is not None
    await rig.scenario(slot_conflicts=[view.card.slot])

    await rig.core.approvals.confirm_click(view.card.approval_id, rig.user)

    attempt = await _wait_for_change(rig, "reschedule")
    assert attempt.status == AttemptStatus.SLOT_CONFLICT.value
    now = (await rig.provider_bookings())[0]
    assert now["slot"] == booking["slot"]
    assert now["status"] == "confirmed"
