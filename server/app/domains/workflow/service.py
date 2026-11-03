"""The errand's state and details. Only code moves the state, and every move is traced."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.clock import LOCAL_TZ, Clock
from app.core.database import Database
from app.domains.gateway.schemas import RefusalReason
from app.domains.tracing.service import RunRecorder
from app.domains.workflow import dates
from app.domains.workflow import request as details
from app.domains.workflow.filters import apply_hard_limits
from app.domains.workflow.models import Workflow, WorkflowState
from app.domains.workflow.repository import WorkflowRepository
from app.domains.workflow.schemas import RequestSnapshot
from app.domains.workflow.state_machine import TRANSITIONS, check_transition

logger = logging.getLogger(__name__)

MAX_USER_TEXT_CHARS = 20_000
SAVE_ATTEMPTS = 3
# Inside the request: dates waiting for the user's answer. They do not count until confirmed.
PROPOSED_DATES = "proposed_dates"
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
    result: dict[str, Any] = field(default_factory=dict)
    # Trace events to write once the change is committed: (kind, data).
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


_ALREADY_CONFIRMED: dict[str, Any] = {
    "already_confirmed": True,
    "message": "The user already confirmed these dates. Do not ask again. Search now.",
}


def _without_proposal(request: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in request.items() if name != PROPOSED_DATES}


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
            created_at=now,
            updated_at=now,
        )

    async def record_user_message(
        self, user_id: str, session_id: str, text: str, recorder: RunRecorder | None = None
    ) -> None:
        """Keep the user's words, read any answer about dates, and resume a stopped errand.

        Confirmation is decided here, from the raw message, before the model runs. The model has
        no way to confirm dates on the user's behalf.
        """
        now = self._clock.now()

        def decide(workflow: Workflow) -> _Change:
            combined = f"{workflow.user_text}\n{text}"[-MAX_USER_TEXT_CHARS:]
            change = _Change(user_text=combined)
            if WorkflowState(workflow.state) in (
                WorkflowState.LIMIT_REACHED,
                WorkflowState.SEARCHING,
            ):
                change.state = WorkflowState.INTAKE

            pending = workflow.request.get(PROPOSED_DATES)
            if not pending:
                return change
            answer = dates.read_answer(text, pending["shown"], pending.get("asked_alone", False))
            if answer == dates.DateAnswer.CONFIRMED:
                merged = _without_proposal(workflow.request)
                for name, key in (("start_date", "start"), ("end_date", "end")):
                    merged[name] = {
                        "value": pending[key],
                        "source": "user_confirmed",
                        "updated_at": now.isoformat(),
                    }
                change.request = merged
                change.events.append(("dates_confirmed", {"dates": pending["label"]}))
            elif answer == dates.DateAnswer.REJECTED:
                change.request = _without_proposal(workflow.request)
                change.events.append(("dates_rejected", {"dates": pending["label"]}))
            else:
                unclear = answer == dates.DateAnswer.UNCLEAR
                change.request = {
                    **workflow.request,
                    PROPOSED_DATES: {**pending, "asked_alone": False, "unclear": unclear},
                }
                if unclear:
                    change.events.append(("dates_unclear", {"dates": pending["label"]}))
            return change

        await self._apply(user_id, session_id, decide, recorder)

    def _known_dates(self, request: dict[str, Any], phrase: str) -> dict[str, Any] | None:
        pending = request.get(PROPOSED_DATES)
        if pending and all(day in phrase for day in pending["shown"]):
            return {
                "needs_confirmation": True,
                "dates": pending["label"],
                "ask_user": f"Ask the user to confirm: {pending['label']}. Do not search yet.",
            }
        start = details.field_value(request, "start_date")
        end = details.field_value(request, "end_date")
        if start and end:
            confirmed = {dates.display(date.fromisoformat(day)) for day in (start, end)}
            if all(day in phrase for day in confirmed):
                return _ALREADY_CONFIRMED | {"dates": " to ".join(sorted(confirmed))}
        return None

    async def note_date_question(self, user_id: str, session_id: str, asked_alone: bool) -> None:
        """Remember whether the reply asked only about the dates, to judge the next "yes"."""

        def decide(workflow: Workflow) -> _Change:
            pending = workflow.request.get(PROPOSED_DATES)
            if not pending:
                return _Change()
            return _Change(
                request={
                    **workflow.request,
                    PROPOSED_DATES: {**pending, "asked_alone": asked_alone, "unclear": False},
                }
            )

        await self._apply(user_id, session_id, decide, None)

    async def resolve_dates(
        self, user_id: str, session_id: str, phrase: str, recorder: RunRecorder | None = None
    ) -> dict[str, Any]:
        """Turn the user's words into exact dates, to be confirmed. Nothing counts until they do."""
        today = self._today()

        def decide(workflow: Workflow) -> _Change:
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
                return _Change(result=_ALREADY_CONFIRMED | {"dates": resolved.label})

            merged = {
                name: value
                for name, value in request.items()
                if name not in ("start_date", "end_date")
            }
            merged[PROPOSED_DATES] = {
                "start": start,
                "end": end,
                "label": resolved.label,
                "shown": list(resolved.shown_dates),
            }
            state = WorkflowState(workflow.state)
            return _Change(
                # Old results covered other dates, so they no longer apply.
                state=WorkflowState.INTAKE if state == WorkflowState.COMPARING else None,
                request=merged,
                result={
                    "needs_confirmation": True,
                    "dates": resolved.label,
                    "ask_user": f"Ask the user to confirm: {resolved.label}. Do not search yet.",
                },
                events=[("dates_proposed", {"phrase": phrase, "dates": resolved.label})],
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
            pending_dates=request.get(PROPOSED_DATES),
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
            if state == WorkflowState.COMPARING and changed & _SEARCH_INPUTS:
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
            if details.missing_required(workflow.request):
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
        if snapshot.missing_required:
            if snapshot.pending_dates:
                label = snapshot.pending_dates["label"]
                return (
                    RefusalReason.MISSING_DETAILS,
                    f"Cannot search yet. The user has not confirmed: {label}. No search was run.",
                )
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
