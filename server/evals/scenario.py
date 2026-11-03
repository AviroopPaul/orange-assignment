"""What a scenario is, and the simulated user's side of it.

A scenario drives the system only through what a person or an outside event could do: messages,
clicks and the provider's admin routes. It then states what must be true of the rows, not the words.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from app.domains.agent.scripted import ScriptedLlm, ToolCall
from app.domains.bookings.models import Approval, BookingAttempt
from app.domains.memory.models import ContextLog, Memory
from app.domains.notifications.models import Notification
from app.domains.proactive.models import BookingDisruption
from app.domains.tracing.models import Run, TraceEvent
from app.domains.workflow.models import Workflow, WorkflowState
from evals.stack import MODEL_NAME, Stack
from mock_provider.models import BookAttempt

# Brief section 3's list of what the evals must include, plus the blueprint's other two areas.
CATEGORIES = (
    "Core flow",
    "Adversarial input",
    "Prompt injection",
    "Changed price or slot after approval",
    "Booking timeout after possible success",
    "Slot conflict",
    "Repeated confirmation",
    "Memory correction and forgetting",
    "Restart mid-booking",
    "Proactive cancellation",
    "Context budget",
    "Limit exhaustion",
    "Tool failures",
    "Infrastructure loss",
)


class Unsettled(Exception):
    pass


@dataclass
class Fact:
    name: str
    actual: Any
    want: Any
    passed: bool


@dataclass
class Scenario:
    id: str
    title: str
    category: str
    expected: str
    run: Callable[["Eval"], Awaitable[None]]
    # Named by this scenario's run and known to be out of every context.
    forbidden: tuple[str, ...] = ()


REGISTRY: list[Scenario] = []


def scenario(
    id_: str, title: str, category: str, expected: str
) -> Callable[[Callable[["Eval"], Awaitable[None]]], Callable[["Eval"], Awaitable[None]]]:
    def register(run: Callable[["Eval"], Awaitable[None]]) -> Callable[["Eval"], Awaitable[None]]:
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category}")
        REGISTRY.append(Scenario(id_, title, category, expected, run))
        return run

    return register


@dataclass
class Eval:
    """One run of one scenario."""

    stack: Stack
    scenario: Scenario
    user: str = ""
    session: str = ""
    facts: list[Fact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.user = f"eval-{self.scenario.id.lower()}"
        self.session = uuid.uuid4().hex

    # Results

    def expect(self, name: str, actual: Any, want: Any = True) -> None:
        self.facts.append(Fact(name, actual, want, actual == want))

    def expect_that(self, name: str, actual: Any, passed: bool) -> None:
        """For a limit or a range, where `actual` alone shows the reader what happened."""
        self.facts.append(Fact(name, actual, "see expected", passed))

    def note(self, text: str) -> None:
        self.notes.append(text)

    def never_show(self, *markers: str) -> None:
        self.forbidden.extend(markers)

    # The simulated user

    def _script(self, script: tuple) -> None:
        self.stack.switchboard.active = ScriptedLlm(model=MODEL_NAME, script=list(script))

    async def say(
        self, text: str, *script: Any, session: str | None = None, user: str | None = None
    ) -> dict:
        self._script(script or ("Okay.",))
        return await self.post_chat(text, session, user)

    async def post_chat(
        self, text: str, session: str | None = None, user: str | None = None
    ) -> dict:
        response = await self.stack.http.post(
            "/chat",
            json={
                "user_id": user or self.user,
                "session_id": session or self.session,
                "message": text,
            },
        )
        response.raise_for_status()
        return response.json()

    async def click(self, approval_id: Any, answer: str = "confirm") -> httpx.Response:
        return await self.stack.http.post(
            f"/approvals/{approval_id}/{answer}", json={"user_id": self.user}
        )

    # The provider's side

    async def set_provider(self, **config: Any) -> None:
        response = await self.stack.provider.post("/admin/scenario", json=config)
        assert response.status_code == 204, response.text

    async def advance_provider_clock(self, seconds: float) -> None:
        response = await self.stack.provider.post("/admin/clock/advance", json={"seconds": seconds})
        assert response.status_code == 200, response.text

    async def provider_cancels(self, booking_id: str) -> None:
        response = await self.stack.provider.post(
            "/admin/provider-cancel", json={"booking_id": booking_id}
        )
        assert response.status_code == 200, response.text

    async def provider_bookings(self, user: str | None = None) -> list[dict]:
        who = user or self.user
        response = await self.stack.provider.post(
            "/tools/list_bookings", json={"user_id": who}, headers={"X-User-Id": who}
        )
        return response.json()["bookings"]

    async def book_calls(self) -> int:
        """How many `book` calls reached the provider, whatever our own rows say."""
        async with self.stack.mock_db.session_scope() as session:
            rows = await session.scalars(select(BookAttempt))
            return sum(row.attempts for row in rows)

    # Rows

    async def _rows(self, model: Any, *where: Any) -> list:
        async with self.stack.db.session_scope() as session:
            return list(await session.scalars(select(model).where(*where)))

    async def approvals(self) -> list[Approval]:
        rows = await self._rows(Approval)
        return sorted(rows, key=lambda row: row.created_at)

    async def attempts(self) -> list[BookingAttempt]:
        rows = await self._rows(BookingAttempt)
        return sorted(rows, key=lambda row: row.created_at)

    async def workflow(self, session: str | None = None, user: str | None = None) -> Workflow:
        rows = await self._rows(
            Workflow,
            Workflow.user_id == (user or self.user),
            Workflow.session_id == (session or self.session),
        )
        # A blank errand can follow a finished one, and a provider's cancel reopens the older one.
        active = [row for row in rows if row.state not in ("done", "failed")]
        return sorted(active or rows, key=lambda row: row.created_at)[-1]

    async def details(self, session: str | None = None) -> dict[str, Any]:
        workflow = await self.workflow(session)
        return {k: v.get("value") for k, v in workflow.request.items() if isinstance(v, dict)}

    async def notices(self, user: str | None = None) -> list[str]:
        rows = await self._rows(Notification, Notification.user_id == (user or self.user))
        return [row.kind for row in sorted(rows, key=lambda row: row.id)]

    async def trace(self, run_id: Any) -> list[TraceEvent]:
        rows = await self._rows(TraceEvent, TraceEvent.run_id == uuid.UUID(str(run_id)))
        return sorted(rows, key=lambda row: row.seq)

    async def all_events(self, kind: str | None = None) -> list[TraceEvent]:
        rows = await self._rows(TraceEvent)
        return [row for row in rows if kind is None or row.kind == kind]

    async def runs(self) -> list[Run]:
        return await self._rows(Run)

    async def contexts(self) -> list[ContextLog]:
        return await self._rows(ContextLog)

    async def memories(self, user: str | None = None) -> list[Memory]:
        return await self._rows(Memory, Memory.user_id == (user or self.user))

    async def disruptions(self) -> list[BookingDisruption]:
        return await self._rows(BookingDisruption)

    # Waiting

    async def wait_until(
        self, what: str, check: Callable[[], Awaitable[Any]], limit_s: float = 30
    ) -> Any:
        deadline = asyncio.get_running_loop().time() + limit_s
        while True:
            value = await check()
            if value:
                return value
            if asyncio.get_running_loop().time() > deadline:
                raise Unsettled(f"timed out waiting for {what}")
            await asyncio.sleep(0.1)

    async def attempt_closed(self, index: int = 0) -> BookingAttempt:
        async def closed() -> BookingAttempt | None:
            rows = await self.attempts()
            if len(rows) > index and rows[index].status != "open":
                return rows[index]
            return None

        return await self.wait_until("the attempt to close", closed)

    async def attempt_claimed(self) -> BookingAttempt:
        async def claimed() -> BookingAttempt | None:
            rows = await self.attempts()
            return rows[0] if rows and rows[0].claims > 0 else None

        return await self.wait_until("a worker to claim the attempt", claimed)

    # Steps that many scenarios share

    async def search(
        self,
        service: str = "plumber",
        area: str = "Indiranagar",
        when: str = "next week",
        extra: str = "",
        **limits: Any,
    ) -> dict:
        """A user states the whole errand, and the scripted model saves it and searches."""
        await self.say(
            f"I need a {service} in {area} {when} {extra}".strip(),
            ToolCall("update_request", {"service": service, "area": area, **limits}),
            ToolCall("resolve_dates", {"phrase": when}),
            ToolCall("search_providers"),
            "Here are some options.",
        )
        return (await self.workflow()).shortlist

    async def propose(self, provider_id: str, slot: str, text: str = "book that one") -> dict:
        return await self.say(
            text,
            ToolCall("propose_booking", {"provider_id": provider_id, "slot": slot}),
            "Please check the card and confirm.",
        )

    async def card(
        self, provider_id: str = "plm-01", service: str = "plumber", area: str = "Indiranagar"
    ) -> tuple[dict, str]:
        """A real search, then a real hold and card. Returns the card and the slot."""
        shortlist = await self.search(service, area)
        slot = shortlist[provider_id]["slots"][0]
        response = await self.propose(provider_id, slot)
        card = response["approval"]
        assert card is not None, response
        return card, slot

    async def booked(
        self, provider_id: str = "plm-01", service: str = "plumber", area: str = "Indiranagar"
    ) -> dict:
        """A verified booking, the normal way. Returns it as the provider shows it."""
        await self.set_provider(book_outcomes=["success"])
        before = len(await self.attempts())
        card, _ = await self.card(provider_id, service, area)
        await self.click(card["approval_id"])
        attempt = await self.attempt_closed(before)
        assert attempt.status == "booked", attempt.status
        return next(
            item
            for item in await self.provider_bookings()
            if item["booking_id"] == attempt.booking_id
        )

    async def propose_change(self, tool: str, text: str, **args: Any) -> dict:
        return await self.say(text, ToolCall(tool, args), "Please check the card and confirm.")

    async def state(self) -> str:
        return WorkflowState((await self.workflow()).state).value
