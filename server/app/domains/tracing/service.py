"""Records runs. A trace write failure must never change a user's outcome."""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.clock import Clock
from app.core.database import Database
from app.core.redaction import scrub_text, scrub_value
from app.domains.tracing.jsonl import JsonlTraceFiles
from app.domains.tracing.models import TraceEvent
from app.domains.tracing.pricing import estimate_cost_usd
from app.domains.tracing.repository import TraceRepository
from app.domains.tracing.schemas import RunTraceRead, TraceEventRead

logger = logging.getLogger(__name__)

SEQ_ATTEMPTS = 5
PROCESS_DIED = "process_died"
ABANDONED_REPLY = (
    "This message was interrupted by a restart before it finished. Please send it again."
)


class RunNotFound(Exception):
    pass


@dataclass
class ModelUsage:
    latency_ms: float
    input_tokens: int
    output_tokens: int


class RunRecorder:
    """Writes one run's events, each in its own short transaction."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        run_id: uuid.UUID,
        seq: int = 0,
        files: JsonlTraceFiles | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self.run_id = run_id
        self._seq = seq
        self._files = files
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
        # The saga writes to the run that took the user's yes, maybe while that run still finishes.
        for _ in range(SEQ_ATTEMPTS):
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
                # A caller cancelled mid-write must not leave a gap in the trace. This happens when
                # a parallel tool call spends the budget and ADK cancels the others.
                await asyncio.shield(self._store(event))
                return
            except IntegrityError:
                self._seq = await self._last_seq()
            except (SQLAlchemyError, OSError):
                logger.exception("could not write trace event %s for run %s", kind, self.run_id)
                return
        logger.error("gave up on trace event %s for run %s", kind, self.run_id)

    async def _store(self, event: TraceEvent) -> None:
        line = {
            "seq": event.seq,
            "at": event.created_at.isoformat(),
            "kind": event.kind,
            "name": event.name,
            "data": event.data,
            "latency_ms": event.latency_ms,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "estimated_cost_usd": event.estimated_cost_usd,
        }
        async with self._db.session_scope() as session:
            await TraceRepository(session).add_event(event)
        if self._files is not None:
            self._files.append(self.run_id, line)

    async def _last_seq(self) -> int:
        async with self._db.session_scope() as session:
            return await TraceRepository(session).last_seq(self.run_id)

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
    def __init__(self, db: Database, clock: Clock, files: JsonlTraceFiles | None = None) -> None:
        self._db = db
        self._clock = clock
        self._files = files

    async def start_run(self, user_id: str, session_id: str) -> RunRecorder:
        async with self._db.session_scope() as session:
            run_id = await TraceRepository(session).create_run(
                user_id, session_id, self._clock.now()
            )
        if self._files is not None:
            self._files.append(
                run_id,
                {
                    "seq": 0,
                    "at": self._clock.now().isoformat(),
                    "kind": "run_started",
                    "data": {"user_id": user_id, "session_id": session_id},
                },
            )
        return RunRecorder(self._db, self._clock, run_id, files=self._files)

    async def resume_run(self, run_id: uuid.UUID) -> RunRecorder:
        """Continue a run's trace from a later process, such as the saga in a worker."""
        async with self._db.session_scope() as session:
            seq = await TraceRepository(session).last_seq(run_id)
        return RunRecorder(self._db, self._clock, run_id, seq, self._files)

    async def settle_run(
        self,
        recorder: RunRecorder,
        outcome: str,
        reply: str,
        settled_by: str = "saga",
        reason: str | None = None,
    ) -> None:
        """A run left open gets its real outcome. Its model counts stay as they are."""
        await recorder.event("outcome", name=outcome, data={"settled_by": settled_by})
        try:
            async with self._db.session_scope() as session:
                await TraceRepository(session).finish_run(
                    recorder.run_id,
                    outcome=outcome,
                    reason=reason,
                    reply=reply,
                    finished_at=self._clock.now(),
                )
        except (SQLAlchemyError, OSError):
            logger.exception("could not settle run %s", recorder.run_id)

    async def close_abandoned(self, older_than: timedelta) -> int:
        """A run still open after this long lost its process, because a live turn cannot outlast
        its lock. The sweep calls this, so every run ends with an outcome."""
        cutoff = self._clock.now() - older_than
        async with self._db.session_scope() as session:
            run_ids = await TraceRepository(session).open_run_ids(cutoff)
        for run_id in run_ids:
            recorder = await self.resume_run(run_id)
            await self.settle_run(
                recorder, "tool_failed", ABANDONED_REPLY, settled_by="sweep", reason=PROCESS_DIED
            )
        return len(run_ids)

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

    async def scrub_user(self, user_id: str, pattern: re.Pattern[str]) -> None:
        """A forgotten fact must not stay readable in old traces."""
        async with self._db.session_scope() as session:
            repository = TraceRepository(session)
            for run in await repository.runs_for_user(user_id):
                if run.reply:
                    run.reply = scrub_text(run.reply, pattern)
                for event in await repository.list_events(run.id):
                    event.data = scrub_value(event.data, pattern)
                if self._files is not None:
                    self._files.scrub(run.id, pattern)

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
