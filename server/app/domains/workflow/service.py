"""The errand's state and details. Only code moves the state, and every move is traced."""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import LOCAL_TZ, Clock
from app.core.database import Database
from app.domains.gateway.schemas import RefusalReason
from app.domains.tracing.service import RunRecorder
from app.domains.workflow import dates
from app.domains.workflow import request as details
from app.domains.workflow.filters import apply_hard_limits, build_shortlist
from app.domains.workflow.models import Workflow, WorkflowState
from app.domains.workflow.repository import WorkflowRepository
from app.domains.workflow.schemas import RequestSnapshot
from app.domains.workflow.state_machine import TRANSITIONS, check_transition

logger = logging.getLogger(__name__)

MAX_USER_TEXT_CHARS = 20_000
SAVE_ATTEMPTS = 3
# Inside the request: dates saved but not yet shown to the user in a reply.
DATES_TO_SHOW = "dates_to_show"
# Changing any of these makes earlier search results out of date.
_SEARCH_INPUTS = frozenset(
    {
        "service",
        "area",
        "start_date",
        "end_date",
        "earliest_time",
        "latest_time",
        "max_budget",
    }
)


class WorkflowConflict(Exception):
    """Another process kept changing this workflow, even after retries."""


@dataclass
class _Change:
    """What one update wants to write, and what to tell the caller."""

    state: WorkflowState | None = None
    request: dict[str, Any] | None = None
    user_text: str | None = None
    shortlist: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    # Trace events to write once the change is committed: (kind, data).
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


_ALREADY_SAVED: dict[str, Any] = {
    "already_saved": True,
    "message": "These dates are already saved. Do not ask about them. "
    "Search now if nothing else is needed.",
}


_BOOKING_RUNNING = frozenset({WorkflowState.EXECUTING, WorkflowState.VERIFYING})
_BOOKING_IN_PROGRESS: dict[str, Any] = {
    "error": "booking_in_progress",
    "message": "A booking is in progress and not confirmed yet. "
    "Nothing can change until it finishes. Tell the user that.",
}
# Changing the request while a card is on screen, or after a search, needs a new search.
_REOPENED_BY_A_CHANGE = frozenset({WorkflowState.COMPARING, WorkflowState.AWAITING_APPROVAL})


@dataclass(frozen=True)
class ProposalTarget:
    """A provider and slot that came from the last search, within the user's limits."""

    workflow_id: uuid.UUID
    provider_id: str
    provider_name: str
    service: str
    slot: str
    max_budget: int | None


def _same_time(first: str, second: str) -> bool:
    try:
        one, other = datetime.fromisoformat(first), datetime.fromisoformat(second)
    except ValueError:
        return False
    return (one if one.tzinfo else one.replace(tzinfo=LOCAL_TZ)) == (
        other if other.tzinfo else other.replace(tzinfo=LOCAL_TZ)
    )


class WorkflowService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def _today(self) -> date:
        return self._clock.now().astimezone(LOCAL_TZ).date()

    async def _apply(
        self,
        user_id: str,
        session_id: str,
        decide: Callable[[Workflow], _Change],
        recorder: RunRecorder | None,
    ) -> _Change:
        """Read, decide, then write only if nobody changed the row in between."""
        for _ in range(SAVE_ATTEMPTS):
            try:
                outcome = await self._apply_once(user_id, session_id, decide)
            except IntegrityError:
                # Another request created this session's workflow first. Use that one.
                continue
            if outcome is None:
                continue
            change, previous = outcome
            if change.state is not None and change.state != previous and recorder is not None:
                await recorder.event(
                    "state_transition",
                    name=change.state.value,
                    data={"from": previous.value, "to": change.state.value},
                )
            if recorder is not None:
                for kind, data in change.events:
                    await recorder.event(kind, data=data)
            return change
        raise WorkflowConflict(f"{user_id}:{session_id}")

    async def _apply_once(
        self, user_id: str, session_id: str, decide: Callable[[Workflow], _Change]
    ) -> tuple[_Change, WorkflowState] | None:
        async with self._db.session_scope() as session:
            repository = WorkflowRepository(session)
            workflow = await repository.get_active(user_id, session_id, lock=True)
            if workflow is None:
                workflow = self._new_workflow(user_id, session_id)
                await repository.add(workflow)

            previous = WorkflowState(workflow.state)
            change = decide(workflow)
            if change.state is not None and change.state != previous:
                check_transition(previous, change.state)

            values: dict[str, Any] = {"updated_at": self._clock.now()}
            if change.state is not None:
                values["state"] = change.state.value
            if change.request is not None:
                values["request"] = change.request
            if change.user_text is not None:
                values["user_text"] = change.user_text
            if change.shortlist is not None:
                values["shortlist"] = change.shortlist
            if not await repository.save(workflow.id, workflow.version, **values):
                return None
        return change, previous

    def _new_workflow(self, user_id: str, session_id: str) -> Workflow:
        now = self._clock.now()
        return Workflow(
            user_id=user_id,
            session_id=session_id,
            state=WorkflowState.INTAKE.value,
            version=1,
            request={},
            user_text="",
            shortlist={},
            created_at=now,
            updated_at=now,
        )

    async def record_user_message(
        self, user_id: str, session_id: str, text: str, recorder: RunRecorder | None = None
    ) -> None:
        """Keep the user's words, and resume a stopped errand."""

        def decide(workflow: Workflow) -> _Change:
            combined = f"{workflow.user_text}\n{text}"[-MAX_USER_TEXT_CHARS:]
            change = _Change(user_text=combined)
            if WorkflowState(workflow.state) in (
                WorkflowState.LIMIT_REACHED,
                WorkflowState.SEARCHING,
            ):
                change.state = WorkflowState.INTAKE
            return change

        await self._apply(user_id, session_id, decide, recorder)

    def _known_dates(self, request: dict[str, Any], phrase: str) -> dict[str, Any] | None:
        start = details.field_value(request, "start_date")
        end = details.field_value(request, "end_date")
        if start and end:
            saved = {dates.display(date.fromisoformat(day)) for day in (start, end)}
            if all(day in phrase for day in saved):
                return _ALREADY_SAVED | {"dates": " to ".join(sorted(saved))}
        return None

    async def note_dates_shown(self, user_id: str, session_id: str, label: str) -> None:
        """A reply showed these dates. Dates saved since then still need showing."""

        def decide(workflow: Workflow) -> _Change:
            waiting = workflow.request.get(DATES_TO_SHOW)
            if not waiting or waiting["label"] != label:
                return _Change()
            request = {k: v for k, v in workflow.request.items() if k != DATES_TO_SHOW}
            return _Change(request=request)

        await self._apply(user_id, session_id, decide, None)

    async def resolve_dates(
        self, user_id: str, session_id: str, phrase: str, recorder: RunRecorder | None = None
    ) -> dict[str, Any]:
        """Turn the user's own words into exact dates and save them. The model cannot invent any."""
        today = self._today()
        now = self._clock.now()

        def decide(workflow: Workflow) -> _Change:
            if WorkflowState(workflow.state) in _BOOKING_RUNNING:
                return _Change(result=_BOOKING_IN_PROGRESS)
            # The model may repeat the exact dates it was given. Those are already in hand.
            known = self._known_dates(workflow.request, phrase)
            if known is not None:
                return _Change(result=known)
            if not dates.is_grounded(phrase, workflow.user_text):
                return _Change(
                    result={
                        "error": "date_not_stated",
                        "message": "The user has not said this. Ask them which dates they want.",
                    }
                )
            resolved = dates.resolve_phrase(phrase, today)
            if resolved is None:
                return _Change(
                    result={
                        "error": "date_not_understood",
                        "message": "Ask the user for a specific day or date.",
                    }
                )
            problem = dates.problem_with(resolved, today)
            if problem:
                return _Change(result={"error": "date_not_usable", "message": problem})

            start, end = resolved.start.isoformat(), resolved.end.isoformat()
            request = workflow.request
            if (
                details.field_value(request, "start_date") == start
                and details.field_value(request, "end_date") == end
            ):
                return _Change(result=_ALREADY_SAVED | {"dates": resolved.label})

            merged = dict(request)
            for name, value in (("start_date", start), ("end_date", end)):
                merged[name] = {"value": value, "source": "user", "updated_at": now.isoformat()}
            merged[DATES_TO_SHOW] = {"label": resolved.label, "shown": list(resolved.shown_dates)}
            state = WorkflowState(workflow.state)
            return _Change(
                # Old results covered other dates, so they no longer apply.
                state=WorkflowState.INTAKE if state in _REOPENED_BY_A_CHANGE else None,
                request=merged,
                result={
                    "saved": True,
                    "dates": resolved.label,
                    "message": "Saved. Do not ask the user to check them. Show these exact dates "
                    "in your reply, and search now if nothing else is needed.",
                },
                events=[("dates_saved", {"phrase": phrase, "dates": resolved.label})],
            )

        return (await self._apply(user_id, session_id, decide, recorder)).result

    async def snapshot(self, user_id: str, session_id: str) -> RequestSnapshot:
        async with self._db.session_scope() as session:
            workflow = await WorkflowRepository(session).get_active(user_id, session_id)
            request = workflow.request if workflow else {}
            state = WorkflowState(workflow.state) if workflow else WorkflowState.INTAKE
            workflow_id = workflow.id if workflow else None
        return RequestSnapshot(
            workflow_id=workflow_id,
            state=state,
            details={
                name: details.field_value(request, name)
                for name in details.ALL_FIELDS
                if details.field_value(request, name) not in (None, "", [])
            },
            missing_required=details.missing_required(request),
            optional_unset=details.optional_unset(request),
            dates_to_show=request.get(DATES_TO_SHOW),
        )

    async def update_request(
        self,
        user_id: str,
        session_id: str,
        changes: dict[str, Any],
        clear_fields: list[str] | None = None,
        recorder: RunRecorder | None = None,
    ) -> dict[str, Any]:
        """Merge details one field at a time. Everything not named here stays as it was."""
        now = self._clock.now()

        def decide(workflow: Workflow) -> _Change:
            if WorkflowState(workflow.state) in _BOOKING_RUNNING:
                return _Change(result=_BOOKING_IN_PROGRESS)
            merged = dict(workflow.request)
            rejected: dict[str, str] = {}
            changed: set[str] = set()

            for name in clear_fields or []:
                if name in details.OPTIONAL_DETAILS and name in merged:
                    del merged[name]
                    changed.add(name)
                elif name in details.REQUIRED_FOR_SEARCH:
                    rejected[name] = (
                        "This detail is required, so it can be changed but not removed."
                    )

            for name, value in changes.items():
                problem = details.rejection(name, value, workflow.user_text)
                if problem:
                    rejected[name] = problem
                    continue
                if details.field_value(merged, name) != value:
                    merged[name] = {
                        "value": value,
                        "source": "user",
                        "updated_at": now.isoformat(),
                    }
                    changed.add(name)

            # A pair of times must make sense together, or the new value is not kept.
            for name, problem in details.pair_rejections(merged).items():
                rejected[name] = problem
                changed.discard(name)
                if name in workflow.request:
                    merged[name] = workflow.request[name]
                else:
                    merged.pop(name, None)

            state = WorkflowState(workflow.state)
            next_state = None
            if state in _REOPENED_BY_A_CHANGE and changed & _SEARCH_INPUTS:
                next_state = WorkflowState.INTAKE

            result = {
                "saved": {
                    name: details.field_value(merged, name)
                    for name in details.ALL_FIELDS
                    if details.field_value(merged, name) not in (None, "", [])
                },
                "missing_fields": details.missing_required(merged),
                "not_given": details.optional_unset(merged),
                "search_needed": bool(changed & _SEARCH_INPUTS)
                or state in (WorkflowState.INTAKE, WorkflowState.SEARCHING),
            }
            if rejected:
                result["rejected"] = rejected
            return _Change(state=next_state, request=merged, result=result)

        return (await self._apply(user_id, session_id, decide, recorder)).result

    async def begin_search(
        self, user_id: str, session_id: str, recorder: RunRecorder | None = None
    ) -> dict[str, Any]:
        """The search inputs, from saved details only. Moves to SEARCHING when they are complete."""

        def decide(workflow: Workflow) -> _Change:
            args = details.search_args(workflow.request)
            # The guard refuses both of these cases, with the reason.
            if details.missing_required(workflow.request) or (
                WorkflowState(workflow.state) in _BOOKING_RUNNING
            ):
                return _Change(result=args)
            return _Change(state=WorkflowState.SEARCHING, result=args)

        return (await self._apply(user_id, session_id, decide, recorder)).result

    async def finish_search(
        self,
        user_id: str,
        session_id: str,
        providers: list[dict[str, Any]],
        recorder: RunRecorder | None = None,
    ) -> dict[str, Any]:
        """Apply the hard limits. The model only ever sees what is left."""

        def decide(workflow: Workflow) -> _Change:
            return _Change(
                state=WorkflowState.COMPARING,
                shortlist=build_shortlist(providers, workflow.request),
                result=apply_hard_limits(providers, workflow.request),
            )

        return (await self._apply(user_id, session_id, decide, recorder)).result

    async def search_failed(
        self, user_id: str, session_id: str, recorder: RunRecorder | None = None
    ) -> None:
        def decide(workflow: Workflow) -> _Change:
            if WorkflowState(workflow.state) == WorkflowState.SEARCHING:
                return _Change(state=WorkflowState.INTAKE)
            return _Change()

        await self._apply(user_id, session_id, decide, recorder)

    async def mark_limit_reached(
        self, user_id: str, session_id: str, recorder: RunRecorder | None = None
    ) -> None:
        """A stopped run must not break a step that may not be interrupted, such as a booking."""

        def decide(workflow: Workflow) -> _Change:
            if WorkflowState.LIMIT_REACHED in TRANSITIONS[WorkflowState(workflow.state)]:
                return _Change(state=WorkflowState.LIMIT_REACHED)
            return _Change()

        await self._apply(user_id, session_id, decide, recorder)

    async def proposal_target(
        self, user_id: str, session_id: str, provider_id: str, slot: str
    ) -> ProposalTarget | dict[str, str]:
        """The model may only propose what the last search returned. Returns why not, otherwise."""
        async with self._db.session_scope() as session:
            workflow = await WorkflowRepository(session).get_active(user_id, session_id)
        state = WorkflowState(workflow.state) if workflow else WorkflowState.INTAKE
        if state in _BOOKING_RUNNING:
            return _BOOKING_IN_PROGRESS
        if workflow is None or state not in _REOPENED_BY_A_CHANGE:
            return {
                "error": "search_first",
                "message": "Search with the user's current details before proposing a booking.",
            }
        provider = workflow.shortlist.get(provider_id)
        if provider is None:
            return {
                "error": "provider_not_in_results",
                "message": "That provider is not in the latest results. Choose one that is.",
            }
        match = next((item for item in provider["slots"] if _same_time(item, slot)), None)
        if match is None:
            return {
                "error": "slot_not_in_results",
                "message": "That slot is not open within the user's limits. Choose a listed slot.",
            }
        return ProposalTarget(
            workflow_id=workflow.id,
            provider_id=provider_id,
            provider_name=provider["name"],
            service=provider["service"],
            slot=match,
            max_budget=details.field_value(workflow.request, "max_budget"),
        )

    async def state_in(self, session: AsyncSession, workflow_id: uuid.UUID) -> WorkflowState | None:
        """Locks the row, so a decision in the caller's transaction cannot race a change."""
        workflow = await WorkflowRepository(session).get_by_id(workflow_id, lock=True)
        return WorkflowState(workflow.state) if workflow else None

    async def move_in(
        self, session: AsyncSession, workflow_id: uuid.UUID, *targets: WorkflowState
    ) -> list[tuple[WorkflowState, WorkflowState]]:
        """Walk through states inside the caller's transaction. Each step must be legal."""
        repository = WorkflowRepository(session)
        workflow = await repository.get_by_id(workflow_id, lock=True)
        if workflow is None:
            raise WorkflowConflict(str(workflow_id))
        current = WorkflowState(workflow.state)
        moves = []
        for target in targets:
            if target != current:
                check_transition(current, target)
                moves.append((current, target))
                current = target
        if moves and not await repository.save(
            workflow.id, workflow.version, state=current.value, updated_at=self._clock.now()
        ):
            raise WorkflowConflict(str(workflow_id))
        return moves

    async def other_slots(
        self, workflow_id: uuid.UUID, provider_id: str, taken_slot: str, limit: int = 3
    ) -> list[str]:
        async with self._db.session_scope() as session:
            workflow = await WorkflowRepository(session).get_by_id(workflow_id)
        provider = workflow.shortlist.get(provider_id) if workflow else None
        slots = provider["slots"] if provider else []
        return [slot for slot in slots if slot != taken_slot][:limit]


async def record_moves(
    recorder: RunRecorder, moves: list[tuple[WorkflowState, WorkflowState]]
) -> None:
    for previous, target in moves:
        await recorder.event(
            "state_transition",
            name=target.value,
            data={"from": previous.value, "to": target.value},
        )


class WorkflowSearchGuard:
    """The gateway's check on a model's search: details complete, and exactly what was saved."""

    def __init__(self, workflows: WorkflowService, user_id: str, session_id: str) -> None:
        self._workflows = workflows
        self._user_id = user_id
        self._session_id = session_id

    async def check(self, tool: str, args: dict[str, Any]) -> tuple[RefusalReason, str] | None:
        if tool != "search_providers":
            return None
        snapshot = await self._workflows.snapshot(self._user_id, self._session_id)
        if snapshot.state in _BOOKING_RUNNING:
            return (RefusalReason.MISSING_DETAILS, _BOOKING_IN_PROGRESS["message"])
        if snapshot.missing_required:
            missing = ", ".join(snapshot.missing_required)
            return (
                RefusalReason.MISSING_DETAILS,
                f"Cannot search yet. Ask the user for: {missing}. No search was run.",
            )
        saved = details.search_args(
            {name: {"value": value} for name, value in snapshot.details.items()}
        )
        if args != saved:
            return (
                RefusalReason.REQUEST_MISMATCH,
                "That search does not match the user's saved request. No search was run.",
            )
        return None
