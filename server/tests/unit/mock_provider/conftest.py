from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.pool import StaticPool

from app.core.database import Database
from mock_provider.catalog import CATALOG, parse_slot, slots_on
from mock_provider.config import MockSettings
from mock_provider.main import create_app
from mock_provider.models import MockBase
from tests.conftest import FakeClock

USER = "user-1"
OTHER_USER = "user-2"
HEADERS = {"X-User-Id": USER}


class Mock:
    """A small driver over the real HTTP routes, so tests read like the contract."""

    def __init__(self, http: httpx.AsyncClient, clock: FakeClock) -> None:
        self.http = http
        self.clock = clock

    async def tool(self, name: str, user: str | None = USER, **body: object) -> httpx.Response:
        headers = {"X-User-Id": user} if user else {}
        return await self.http.post(f"/tools/{name}", json=body, headers=headers)

    async def scenario(self, **config: object) -> None:
        response = await self.http.post("/admin/scenario", json=config)
        assert response.status_code == 204, response.text

    def free_slots(self, provider_id: str, count: int = 1, skip_hour: int = 12) -> list[str]:
        """Future working slots for a provider, avoiding the default conflict hour."""
        provider = CATALOG[provider_id]
        day = self.clock.now().date() + timedelta(days=1)
        found: list[str] = []
        while len(found) < count:
            for slot in slots_on(provider, day):
                parsed = parse_slot(slot)
                if parsed and parsed.hour != skip_hour and len(found) < count:
                    found.append(slot)
            day += timedelta(days=1)
        return found

    async def hold(self, provider_id: str = "plm-01", slot: str | None = None) -> dict:
        slot = slot or self.free_slots(provider_id)[0]
        response = await self.tool("hold_slot", provider_id=provider_id, slot=slot)
        assert response.status_code == 200, response.text
        return response.json()

    async def booked(self, provider_id: str = "plm-01", key: str = "key-1") -> dict:
        await self.scenario(book_outcomes=["success"])
        hold = await self.hold(provider_id)
        response = await self.tool("book", hold_id=hold["hold_id"], idempotency_key=key)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
async def mock(clock: FakeClock) -> AsyncIterator[Mock]:
    db = Database("sqlite+aiosqlite://", poolclass=StaticPool)
    async with db.engine.begin() as connection:
        await connection.run_sync(MockBase.metadata.create_all)
    settings = MockSettings(mock_eval_mode=True, mock_timeout_hang_s=0)
    app = create_app(settings=settings, db=db, real_now=clock.now)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as http:
        yield Mock(http, clock)
    await db.dispose()
