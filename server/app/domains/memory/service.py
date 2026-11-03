"""Facts across sessions, the redactable chat, and the log of what the model was sent.

Rules that decide trust live in `rules.py`. The model can propose a fact, but only the user's own
words, or their click, can make it count.
"""

import logging
import re
import uuid
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.core.clock import Clock
from app.core.database import Database
from app.core.redaction import compile_patterns, scrub_text, scrub_value
from app.domains.memory.assembler import Turn
from app.domains.memory.models import (
    ChatNote,
    ContextLog,
    Memory,
    MemorySource,
    MemoryStatus,
    SessionRotation,
    TurnView,
)
from app.domains.memory.repository import MemoryRepository
from app.domains.memory.rules import (
    Fact,
    asks_to_forget,
    is_explicit,
    rank,
    read_memory_answer,
)
from app.domains.memory.schemas import ChatMemoryNotes, MemoryRead
from app.domains.tracing.service import RunRecorder, TraceService
from app.domains.workflow.service import WorkflowService

logger = logging.getLogger(__name__)

USER = "user"
ASSISTANT = "assistant"
MAX_TURNS_READ = 200
MAX_FACTS_SENT = 12


class MemoryNotFound(Exception):
    pass


class MemoryNotActive(Exception):
    pass


def _is_trusted(row: Memory) -> bool:
    return row.source == MemorySource.EXPLICIT.value or row.confirmed_at is not None


def _read(row: Memory) -> MemoryRead:
    return MemoryRead(
        id=row.id,
        key=row.key,
        value=row.value,
        scope=row.scope,
        source=row.source,
        quote=row.quote,
        confirmed=_is_trusted(row),
        updated_at=row.updated_at,
    )


def _texts_to_remove(row: Memory) -> list[str]:
    if row.status == MemoryStatus.FORGOTTEN.value:
        return [row.value, row.quote or ""]
    return [row.value]


class MemoryService:
    def __init__(
        self, db: Database, clock: Clock, workflows: WorkflowService, traces: TraceService
    ) -> None:
        self._db = db
        self._clock = clock
        self._workflows = workflows
        self._traces = traces

    # The chat as the model may see it

    async def record_turn(self, user_id: str, session_id: str, role: str, text: str) -> None:
        async with self._db.session_scope() as session:
            await MemoryRepository(session).add(
                TurnView(
                    user_id=user_id,
                    session_id=session_id,
                    role=role,
                    text=text,
                    created_at=self._clock.now(),
                )
            )

    async def recent_turns(self, user_id: str, session_id: str) -> list[Turn]:
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).recent_turns(user_id, session_id, MAX_TURNS_READ)
        return [Turn(row.role, row.text) for row in rows]

    # What the model may know

    async def _trusted_facts(self, user_id: str) -> list[Fact]:
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).trusted(user_id)
        return [Fact(row.key, row.value, row.scope, row.updated_at) for row in rows]

    async def relevant_facts(self, user_id: str, service: str | None, message: str) -> list[Fact]:
        facts = await self._trusted_facts(user_id)
        return rank(facts, service, message, MAX_FACTS_SENT)

    async def remembered_text(self, user_id: str) -> str:
        """A detail from here counts as stated by the user. Nothing unconfirmed is in it."""
        return "\n".join(fact.value for fact in await self._trusted_facts(user_id))

    async def hidden_pattern(self, user_id: str) -> re.Pattern[str] | None:
        """Text being removed right now. A run in progress may still hold a copy."""
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).scrub_pending(user_id)
            return compile_patterns(text for row in rows for text in _texts_to_remove(row))

    # Model tools

    async def propose(
        self,
        user_id: str,
        session_id: str,
        key: str,
        value: str,
        quote: str,
        scope: str,
        recorder: RunRecorder | None = None,
    ) -> dict[str, Any]:
        turns = await self.recent_turns(user_id, session_id)
        said = [turn.text for turn in turns if turn.role == USER]
        if not is_explicit(value, quote, said):
            await self._save_unconfirmed(user_id, session_id, key, value, quote, scope)
            if recorder is not None:
                await recorder.event("memory_unconfirmed", data={"key": key, "scope": scope})
            return {
                "status": "unconfirmed",
                "message": "Not saved, because the user's words do not state it. The chat is "
                "asking them about it, so do not ask yourself. Do not use it. Carry on.",
            }
        changed = await self._save_trusted(
            user_id, session_id, key, value, quote, scope, ChatNote.SAVED
        )
        if recorder is not None:
            await recorder.event("memory_saved", data={"key": key, "scope": scope})
        if changed is False:
            return {"status": "already_saved", "key": key, "value": value}
        await self._scrub_now(user_id)
        return {
            "status": "saved",
            "key": key,
            "value": value,
            "message": "Saved. The chat shows this to the user, so you need not repeat it.",
        }

    async def _save_unconfirmed(
        self, user_id: str, session_id: str, key: str, value: str, quote: str, scope: str
    ) -> None:
        async with self._db.session_scope() as session:
            repository = MemoryRepository(session)
            now = self._clock.now()
            # A later guess replaces an earlier one. A guess never leaves anything to scrub.
            for old in await repository.unconfirmed_for_key(user_id, key, scope):
                old.status = MemoryStatus.SUPERSEDED.value
                old.chat_note = None
                old.updated_at = now
            await repository.add(
                Memory(
                    user_id=user_id,
                    key=key,
                    value=value,
                    scope=scope,
                    source=MemorySource.INFERRED.value,
                    status=MemoryStatus.ACTIVE.value,
                    quote=quote,
                    session_id=session_id,
                    chat_note=ChatNote.ASK.value,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def _save_trusted(
        self,
        user_id: str,
        session_id: str | None,
        key: str,
        value: str,
        quote: str | None,
        scope: str,
        chat_note: ChatNote | None = None,
    ) -> bool:
        """False if nothing changed. A correction adds a row and supersedes the old one."""
        async with self._db.session_scope() as session:
            repository = MemoryRepository(session)
            now = self._clock.now()
            current = await repository.trusted_for_key(user_id, key, scope)
            if current is not None and current.value.casefold() == value.casefold():
                return False
            new_id = uuid.uuid4()
            if current is not None:
                current.status = MemoryStatus.SUPERSEDED.value
                current.superseded_by = new_id
                current.updated_at = now
                # "HSR" must not be removed from the chat when the new value is "HSR Layout".
                current.scrub_pending = current.value.casefold() not in value.casefold()
            for guess in await repository.unconfirmed_for_key(user_id, key, scope):
                guess.status = MemoryStatus.SUPERSEDED.value
                guess.superseded_by = new_id
                guess.chat_note = None
                guess.updated_at = now
            await session.flush()
            await repository.add(
                Memory(
                    id=new_id,
                    user_id=user_id,
                    key=key,
                    value=value,
                    scope=scope,
                    source=MemorySource.EXPLICIT.value,
                    status=MemoryStatus.ACTIVE.value,
                    quote=quote,
                    session_id=session_id,
                    chat_note=chat_note.value if chat_note else None,
                    created_at=now,
                    updated_at=now,
                )
            )
        return True

    async def list_for_model(self, user_id: str) -> dict[str, Any]:
        """Only what the model may know. Unconfirmed guesses are counted, never shown."""
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).active(user_id)
            shown = [_read(row) for row in rows]
        trusted = [row for row in shown if row.confirmed]
        return {
            "memories": [
                {"key": row.key, "value": row.value, "scope": row.scope, "source": row.source}
                for row in trusted
            ],
            "waiting_on_memory_panel": len(shown) - len(trusted),
        }

    async def forget_from_chat(
        self,
        user_id: str,
        session_id: str,
        key: str,
        scope: str,
        recorder: RunRecorder | None = None,
    ) -> dict[str, Any]:
        """Only the user's own message can ask for this. A review cannot, whatever it says."""
        turns = await self.recent_turns(user_id, session_id)
        latest = next((turn.text for turn in reversed(turns) if turn.role == USER), "")
        if not asks_to_forget(latest):
            return {
                "error": "not_asked",
                "message": "The user did not ask to forget anything. Nothing was forgotten.",
            }
        forgotten = await self._forget_key(user_id, key, scope)
        if not forgotten:
            return {
                "error": "memory_not_found",
                "message": "Nothing is saved under that key. Use list_memories to check.",
            }
        if recorder is not None:
            await recorder.event("memory_forgotten", data={"key": key, "scope": scope})
        return {
            "status": "forgotten",
            "key": key,
            "message": "Forgotten. Tell the user, without repeating the value.",
        }

    async def _forget_key(self, user_id: str, key: str, scope: str) -> bool:
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).all_for_key(user_id, key, scope)
            self._mark_forgotten(rows)
        if rows:
            await self._scrub_now(user_id)
        return bool(rows)

    def _mark_forgotten(self, rows: Any) -> None:
        now = self._clock.now()
        for row in rows:
            row.status = MemoryStatus.FORGOTTEN.value
            row.scrub_pending = True
            row.updated_at = now

    # The memory panel

    async def list_all(self, user_id: str) -> list[MemoryRead]:
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).active(user_id)
            return [_read(row) for row in rows]

    async def _active_row(self, user_id: str, memory_id: uuid.UUID) -> tuple[str, str, str]:
        async with self._db.session_scope() as session:
            row = await MemoryRepository(session).get(user_id, memory_id)
            if row is None:
                raise MemoryNotFound(str(memory_id))
            if row.status != MemoryStatus.ACTIVE.value:
                raise MemoryNotActive(str(memory_id))
            return row.key, row.scope, row.value

    async def forget_by_id(self, user_id: str, memory_id: uuid.UUID) -> None:
        """Saying no to a guess drops only the guess, not a trusted value of the same fact."""
        key, scope, _ = await self._active_row(user_id, memory_id)
        if await self._dismiss_if_guess(user_id, memory_id):
            return
        await self._forget_key(user_id, key, scope)

    async def _dismiss_if_guess(self, user_id: str, memory_id: uuid.UUID) -> bool:
        async with self._db.session_scope() as session:
            row = await MemoryRepository(session).get(user_id, memory_id, lock=True)
            if row is None or _is_trusted(row):
                return False
            row.status = MemoryStatus.SUPERSEDED.value
            row.chat_note = None
            row.updated_at = self._clock.now()
            return True

    async def correct_by_id(self, user_id: str, memory_id: uuid.UUID, value: str) -> MemoryRead:
        key, scope, _ = await self._active_row(user_id, memory_id)
        await self._save_trusted(user_id, None, key, value, None, scope)
        await self._scrub_now(user_id)
        async with self._db.session_scope() as session:
            row = await MemoryRepository(session).trusted_for_key(user_id, key, scope)
            if row is None:
                raise MemoryNotFound(str(memory_id))
            return _read(row)

    async def confirm_by_id(self, user_id: str, memory_id: uuid.UUID) -> MemoryRead:
        """The user's click. Any older value of this fact is superseded."""
        key, scope, _ = await self._active_row(user_id, memory_id)
        now = self._clock.now()
        async with self._db.session_scope() as session:
            repository = MemoryRepository(session)
            row = await repository.get(user_id, memory_id, lock=True)
            if row is None or row.status != MemoryStatus.ACTIVE.value:
                raise MemoryNotActive(str(memory_id))
            if row.confirmed_at is None and row.source == MemorySource.INFERRED.value:
                current = await repository.trusted_for_key(user_id, key, scope)
                if current is not None:
                    current.status = MemoryStatus.SUPERSEDED.value
                    current.superseded_by = row.id
                    current.scrub_pending = current.value.casefold() not in row.value.casefold()
                    current.updated_at = now
                    await session.flush()
                row.confirmed_at = now
                row.updated_at = now
            row.chat_note = None
            result = _read(row)
        await self._scrub_now(user_id)
        return result

    # What the chat shows about memory

    async def answer_from_message(
        self, user_id: str, session_id: str, message: str, recorder: RunRecorder | None = None
    ) -> str | None:
        """Read before the model runs, like a yes on a card, so the model cannot answer for them.

        The question lasts for one message. After that, the fact waits on the Memory panel.
        """
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).with_chat_note(user_id, session_id)
            asked = [row.id for row in rows if row.chat_note == ChatNote.ASK.value]
            if not asked:
                return None
            answer = read_memory_answer(message)
            if answer is None:
                for row in rows:
                    row.chat_note = None
                return None
        if answer:
            confirmed = [await self.confirm_by_id(user_id, memory_id) for memory_id in asked]
            reply = "Okay, I'll remember: " + "; ".join(row.value for row in confirmed) + "."
        else:
            for memory_id in asked:
                await self._dismiss_if_guess(user_id, memory_id)
            reply = "Okay, I won't remember that."
        if recorder is not None:
            await recorder.event("memory_answered", data={"yes": answer, "count": len(asked)})
        return reply

    async def take_chat_notes(self, user_id: str, session_id: str) -> ChatMemoryNotes:
        """A saved fact is announced once. A question stays open for the user's next message."""
        async with self._db.session_scope() as session:
            rows = await MemoryRepository(session).with_chat_note(user_id, session_id)
            active = [row for row in rows if row.status == MemoryStatus.ACTIVE.value]
            notes = ChatMemoryNotes(
                saved=[_read(row) for row in active if row.chat_note == ChatNote.SAVED.value],
                to_confirm=[_read(row) for row in active if row.chat_note == ChatNote.ASK.value],
            )
            for row in rows:
                if row.chat_note == ChatNote.SAVED.value or row not in active:
                    row.chat_note = None
        return notes

    # Removing text from every stored copy

    async def _scrub_now(self, user_id: str) -> None:
        """Clean what the next model call reads, and mark the ADK sessions for deletion."""
        pattern = await self.hidden_pattern(user_id)
        if pattern is None:
            return
        await self._scrub_turns(user_id, pattern)
        await self._workflows.scrub_user(user_id, pattern)
        async with self._db.session_scope() as session:
            repository = MemoryRepository(session)
            for session_id in await repository.session_ids(user_id):
                if await repository.rotation(user_id, session_id) is None:
                    await repository.add(
                        SessionRotation(
                            user_id=user_id,
                            session_id=session_id,
                            requested_at=self._clock.now(),
                        )
                    )

    async def _scrub_turns(self, user_id: str, pattern: re.Pattern[str]) -> None:
        async with self._db.session_scope() as session:
            for row in await MemoryRepository(session).all_turns(user_id):
                row.text = scrub_text(row.text, pattern)

    async def finish_scrub(self, user_id: str) -> None:
        """Runs once no reply can still quote the text: the reply is stored, then cleaned too.

        Only here is a forgotten value wiped. Until now, a run in progress needed it for redaction.
        """
        pattern = await self.hidden_pattern(user_id)
        if pattern is None:
            return
        await self._scrub_turns(user_id, pattern)
        await self._workflows.scrub_user(user_id, pattern)
        await self._traces.scrub_user(user_id, pattern)
        async with self._db.session_scope() as session:
            repository = MemoryRepository(session)
            for log in await repository.context_logs(user_id):
                log.payload = scrub_value(log.payload, pattern)
            for row in await repository.scrub_pending(user_id):
                if row.status == MemoryStatus.FORGOTTEN.value:
                    row.value = ""
                    row.quote = None
                row.scrub_pending = False

    async def rotations_due(self, user_id: str) -> list[str]:
        async with self._db.session_scope() as session:
            return await MemoryRepository(session).rotations_due(user_id)

    async def clear_rotation(self, user_id: str, session_id: str) -> None:
        async with self._db.session_scope() as session:
            await MemoryRepository(session).clear_rotation(user_id, session_id)

    # What the model was sent

    async def log_context(
        self,
        run_id: uuid.UUID,
        user_id: str,
        session_id: str,
        payload: dict[str, Any],
        tokens: dict[str, int],
        estimated_tokens: int,
        budget: int,
    ) -> int:
        """Raises if the write fails, because a call that cannot be logged must not run."""
        row = ContextLog(
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            payload=payload,
            tokens=tokens,
            estimated_tokens=estimated_tokens,
            budget=budget,
            created_at=self._clock.now(),
        )
        async with self._db.session_scope() as session:
            await MemoryRepository(session).add(row)
            return row.id

    async def note_actual_tokens(self, context_id: int, input_tokens: int) -> None:
        try:
            async with self._db.session_scope() as session:
                row = await MemoryRepository(session).context_log(context_id)
                if row is not None:
                    row.actual_input_tokens = input_tokens
        except (SQLAlchemyError, OSError):
            logger.warning("could not note the real token count for context %s", context_id)
