"""The gateway over a real network to the real mock service, on real PostgreSQL."""

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domains.gateway.client import ProviderClient
from app.domains.gateway.schemas import Caller, ToolStatus
from app.domains.gateway.service import CallContext, ToolBudget, ToolGateway
from mock_provider.catalog import CATALOG, slots_on
from mock_provider.migrate import main as migrate_mock

pytestmark = pytest.mark.integration

USER = "gateway-it"


class AllowAll:
    async def is_valid(self, *args: object) -> bool:
        return True


def context(caller: Caller = Caller.SAGA) -> CallContext:
    return CallContext(USER, caller, ToolBudget(12), None, "token")


def next_slot(provider_id: str) -> str:
    provider = CATALOG[provider_id]
    day = datetime.now(UTC).date() + timedelta(days=1)
    while True:
        slots = [slot for slot in slots_on(provider, day) if "T12:" not in slot]
        if slots:
            return slots[0]
        day += timedelta(days=1)


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    migrate_mock()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mock_provider.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env={**os.environ, "MOCK_EVAL_MODE": "true"},
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    yield url
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()


@pytest.fixture
async def gateway(server_url: str) -> AsyncIterator[ToolGateway]:
    async with httpx.AsyncClient(base_url=server_url) as admin:
        assert (await admin.post("/admin/reset")).status_code == 204
    client = ProviderClient(server_url, timeout_s=1)
    yield ToolGateway(client, AllowAll(), retry_backoff_s=0.01)
    await client.close()


async def set_scenario(server_url: str, **config: object) -> None:
    async with httpx.AsyncClient(base_url=server_url) as admin:
        assert (await admin.post("/admin/scenario", json=config)).status_code == 204


async def test_timeout_after_success_end_to_end(gateway: ToolGateway, server_url: str) -> None:
    """The hardest case, over a real connection: the caller times out, the booking exists."""
    await set_scenario(server_url, book_outcomes=["timeout_booked"], timeout_hang_s=3)
    key = uuid.uuid4().hex
    hold = await gateway.call(
        "hold_slot",
        {"provider_id": "plm-01", "slot": next_slot("plm-01")},
        context(Caller.PROPOSAL),
    )
    assert hold.ok

    started = time.monotonic()
    booked = await gateway.call(
        "book", {"hold_id": hold.data["hold_id"], "idempotency_key": key}, context()
    )
    assert time.monotonic() - started < 2
    assert booked.status == ToolStatus.TIMEOUT
    assert booked.ambiguous
    assert booked.attempts == 1

    status = await gateway.call(
        "get_booking_status", {"idempotency_key": key}, context(Caller.BACKGROUND)
    )
    assert status.data["status"] == "booked"

    replay = await gateway.call(
        "book", {"hold_id": hold.data["hold_id"], "idempotency_key": key}, context()
    )
    assert replay.ok
    assert replay.data["booking_id"] == status.data["booking_id"]

    bookings = await gateway.call("list_bookings", {"user_id": USER}, context(Caller.BACKGROUND))
    assert len(bookings.data["bookings"]) == 1


async def test_cancel_503_is_retried_over_a_real_connection(
    gateway: ToolGateway, server_url: str
) -> None:
    await set_scenario(server_url, book_outcomes=["success"])
    hold = await gateway.call(
        "hold_slot",
        {"provider_id": "ele-01", "slot": next_slot("ele-01")},
        context(Caller.PROPOSAL),
    )
    booking = await gateway.call(
        "book", {"hold_id": hold.data["hold_id"], "idempotency_key": uuid.uuid4().hex}, context()
    )
    result = await gateway.call(
        "cancel",
        {"booking_id": booking.data["booking_id"], "idempotency_key": uuid.uuid4().hex},
        context(),
    )
    assert result.ok
    assert result.attempts == 2
    assert result.data["status"] == "cancelled"


async def test_reachability_and_a_dead_provider(gateway: ToolGateway) -> None:
    assert await gateway.is_provider_reachable()

    dead = ToolGateway(ProviderClient("http://127.0.0.1:9", timeout_s=0.5), AllowAll(), 0.01)
    assert not await dead.is_provider_reachable()
    result = await dead.call(
        "search_providers",
        {
            "service": "plumber",
            "area": "x",
            "date_range": {"start": "2026-11-03", "end": "2026-11-04"},
        },
        context(Caller.MODEL),
    )
    assert result.error_code == "provider_unavailable"
    assert result.attempts == 3
