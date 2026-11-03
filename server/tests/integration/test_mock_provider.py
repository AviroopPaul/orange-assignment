"""What needs real PostgreSQL and real HTTP: races, restarts, migrations and a real hang."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError

from mock_provider.catalog import CATALOG, slots_on
from mock_provider.config import get_mock_settings
from mock_provider.migrate import main as migrate_mock

pytestmark = pytest.mark.integration

USER = {"X-User-Id": "it-user"}


@pytest.fixture(scope="module", autouse=True)
def migrated() -> None:
    migrate_mock()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_server(port: int) -> subprocess.Popen:
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
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return process
        except httpx.TransportError:
            time.sleep(0.1)
    process.kill()
    raise AssertionError("mock provider did not start")


def stop_server(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@pytest.fixture(scope="module")
def server() -> Iterator[int]:
    port = free_port()
    process = start_server(port)
    yield port
    stop_server(process)


@pytest.fixture
async def http(server: int) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server}", timeout=20) as client:
        assert (await client.post("/admin/reset")).status_code == 204
        yield client


def next_slots(provider_id: str, count: int = 2) -> list[str]:
    from datetime import UTC, datetime

    provider = CATALOG[provider_id]
    day = datetime.now(UTC).date() + timedelta(days=1)
    found: list[str] = []
    while len(found) < count:
        found.extend(slot for slot in slots_on(provider, day) if "T12:" not in slot)
        day += timedelta(days=1)
    return found[:count]


def test_models_and_migrations_agree() -> None:
    try:
        command.check(Config("alembic.ini", ini_section="mock"))
    except CommandError as error:
        pytest.fail(f"mock models changed without a migration: {error}")


def test_migrating_twice_is_safe() -> None:
    migrate_mock()
    migrate_mock()


def test_database_url_is_the_test_database() -> None:
    assert get_mock_settings().mock_database_url.endswith("mock_provider_test")


async def test_only_one_of_ten_concurrent_holds_gets_the_slot(http: httpx.AsyncClient) -> None:
    slot = next_slots("plm-01")[0]
    responses = await asyncio.gather(
        *[
            http.post(
                "/tools/hold_slot",
                json={"provider_id": "plm-01", "slot": slot},
                headers={"X-User-Id": f"user-{n}"},
            )
            for n in range(10)
        ]
    )
    codes = sorted(response.status_code for response in responses)
    assert codes == [200] + [409] * 9


async def test_ten_concurrent_books_with_one_key_make_one_booking(http: httpx.AsyncClient) -> None:
    await http.post("/admin/scenario", json={"book_outcomes": ["success"], "seed": 1})
    hold = await http.post(
        "/tools/hold_slot",
        json={"provider_id": "plm-01", "slot": next_slots("plm-01")[0]},
        headers=USER,
    )
    hold_id = hold.json()["hold_id"]

    responses = await asyncio.gather(
        *[
            http.post(
                "/tools/book", json={"hold_id": hold_id, "idempotency_key": "race"}, headers=USER
            )
            for _ in range(10)
        ]
    )
    booked_ids = {r.json()["booking_id"] for r in responses if r.status_code == 200}
    assert len(booked_ids) == 1

    listed = await http.post("/tools/list_bookings", json={"user_id": "it-user"}, headers=USER)
    assert len(listed.json()["bookings"]) == 1


async def test_concurrent_cancels_still_cancel_once(http: httpx.AsyncClient) -> None:
    await http.post("/admin/scenario", json={"book_outcomes": ["success"]})
    hold = await http.post(
        "/tools/hold_slot",
        json={"provider_id": "ele-01", "slot": next_slots("ele-01")[0]},
        headers=USER,
    )
    booking = await http.post(
        "/tools/book",
        json={"hold_id": hold.json()["hold_id"], "idempotency_key": "c-race"},
        headers=USER,
    )
    body = {"booking_id": booking.json()["booking_id"], "idempotency_key": "cancel-race"}
    responses = await asyncio.gather(
        *[http.post("/tools/cancel", json=body, headers=USER) for _ in range(5)]
    )
    codes = [response.status_code for response in responses]
    assert codes.count(503) == 1
    assert codes.count(200) == 4
    assert all(code in (200, 503) for code in codes)


async def test_a_real_client_timeout_can_still_leave_a_real_booking(
    http: httpx.AsyncClient,
) -> None:
    """Timeout-after-success over a real connection: the caller gives up, the booking exists."""
    await http.post(
        "/admin/scenario", json={"book_outcomes": ["timeout_booked"], "timeout_hang_s": 3}
    )
    hold = await http.post(
        "/tools/hold_slot",
        json={"provider_id": "car-01", "slot": next_slots("car-01")[0]},
        headers=USER,
    )

    with pytest.raises(httpx.ReadTimeout):
        await http.post(
            "/tools/book",
            json={"hold_id": hold.json()["hold_id"], "idempotency_key": "real-timeout"},
            headers=USER,
            timeout=0.5,
        )

    status = await http.post(
        "/tools/get_booking_status", json={"idempotency_key": "real-timeout"}, headers=USER
    )
    assert status.json()["status"] == "booked"


async def test_state_survives_a_server_kill() -> None:
    """Restart mid-booking needs this: the provider's world must outlive any process."""
    port = free_port()
    process = start_server(port)
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=20) as client:
            await client.post("/admin/reset")
            await client.post("/admin/scenario", json={"book_outcomes": ["success"]})
            hold = await client.post(
                "/tools/hold_slot",
                json={"provider_id": "cln-01", "slot": next_slots("cln-01")[0]},
                headers=USER,
            )
            await client.post(
                "/tools/book",
                json={"hold_id": hold.json()["hold_id"], "idempotency_key": "survivor"},
                headers=USER,
            )
    finally:
        stop_server(process)

    process = start_server(port)
    try:
        async with httpx.AsyncClient(base_url=base, timeout=20) as client:
            status = await client.post(
                "/tools/get_booking_status", json={"idempotency_key": "survivor"}, headers=USER
            )
            assert status.json()["status"] == "booked"
    finally:
        stop_server(process)
