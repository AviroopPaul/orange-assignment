"""Records runs. A trace write failure must never change a user's outcome."""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from app.core.clock import Clock
from app.core.database import Database
from app.domains.tracing.models import TraceEvent
from app.domains.tracing.pricing import estimate_cost_usd
from app.domains.tracing.repository import TraceRepository
from app.domains.tracing.schemas import RunTraceRead, TraceEventRead

logger = logging.getLogger(__name__)


class RunNotFound(Exception):
    pass


@dataclass
class ModelUsage:
    latency_ms: float
    input_tokens: int
    output_tokens: int


class RunRecorder:
    """Writes one run's events, each in its own short transaction."""

    def __init__(self, db: Database, clock: Clock, run_id: uuid.UUID) -> None:
        self._db = db
        self._clock = clock
        self.run_id = run_id
        self._seq = 0
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost_usd = 0.0

    async def event(
        self,
        kind: str,
        name: str | None = None,
        data: dict | None = None,
        latency_ms: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
    ) -> None:
        self._seq += 1
        event = TraceEvent(
            run_id=self.run_id,
            seq=self._seq,
            kind=kind,
            name=name,
            data=data or {},
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            created_at=self._clock.now(),
        )
        try:
            async with self._db.session_scope() as session:
                await TraceRepository(session).add_event(event)
        except (SQLAlchemyError, OSError):
            logger.exception("could not write trace event %s for run %s", kind, self.run_id)

    async def model_call(self, model: str, usage: ModelUsage, data: dict) -> None:
        cost = estimate_cost_usd(model, usage.input_tokens, usage.output_tokens)
        self.model_calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.estimated_cost_usd += cost or 0.0
        await self.event(
            "model_call",
            name=model,
            data=data,
            latency_ms=usage.latency_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_usd=cost,
        )


class TraceService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def start_run(self, user_id: str, session_id: str) -> RunRecorder:
        async with self._db.session_scope() as session:
            run_id = await TraceRepository(session).create_run(
                user_id, session_id, self._clock.now()
            )
        return RunRecorder(self._db, self._clock, run_id)

    async def finish_run(
        self, recorder: RunRecorder, outcome: str, reason: str | None, reply: str
    ) -> None:
        await recorder.event("outcome", name=outcome, data={"reason": reason})
        try:
            async with self._db.session_scope() as session:
                await TraceRepository(session).finish_run(
                    recorder.run_id,
                    outcome=outcome,
                    reason=reason,
                    reply=reply,
                    model_calls=recorder.model_calls,
                    input_tokens=recorder.input_tokens,
                    output_tokens=recorder.output_tokens,
                    estimated_cost_usd=recorder.estimated_cost_usd,
                    finished_at=self._clock.now(),
                )
        except (SQLAlchemyError, OSError):
            logger.exception("could not finish run %s", recorder.run_id)

    async def get_trace(self, run_id: uuid.UUID) -> RunTraceRead:
        async with self._db.session_scope() as session:
            repository = TraceRepository(session)
            run = await repository.get_run(run_id)
            if run is None:
                raise RunNotFound(str(run_id))
            events = await repository.list_events(run_id)
        return RunTraceRead(
            id=run.id,
            user_id=run.user_id,
            session_id=run.session_id,
            outcome=run.outcome,
            reason=run.reason,
            reply=run.reply,
            model_calls=run.model_calls,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            estimated_cost_usd=run.estimated_cost_usd,
            started_at=run.started_at,
            finished_at=run.finished_at,
            events=[TraceEventRead.model_validate(event) for event in events],
        )
