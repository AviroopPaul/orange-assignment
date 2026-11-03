"""The eight contract tools, plus the eval-only controls."""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from mock_provider import catalog
from mock_provider.catalog import CATALOG, CURRENCY, Provider
from mock_provider.models import BookAttempt, Booking, Hold, KeyedResult, MockState
from mock_provider.outcomes import BookOutcome, decide_book_outcome
from mock_provider.repository import MockRepository
from mock_provider.schemas import (
    BookingRead,
    BookingStatusRead,
    HoldRead,
    ProviderAttributes,
    ProviderDetails,
    ProviderSummary,
    ReviewRead,
    ScenarioConfig,
    SearchProvidersInput,
)
from mock_provider.seed import seed_demo_bookings

HOLD_MINUTES = 10
ACTIVE_BOOKING = "confirmed"


class ToolError(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


def _not_found(what: str) -> ToolError:
    return ToolError(f"{what}_not_found", 404, f"No such {what.replace('_', ' ')}.")


def _attributes(provider: Provider) -> ProviderAttributes:
    return ProviderAttributes(
        technician=provider.technician,
        years_experience=provider.years_experience,
        verified=provider.verified,
        languages=list(provider.languages),
    )


class MockProviderService:
    def __init__(
        self,
        db: Database,
        default_seed: int,
        timeout_hang_s: float,
        real_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._default_seed = default_seed
        self._timeout_hang_s = timeout_hang_s
        self._real_now = real_now or (lambda: datetime.now(UTC))

    def _now(self, state: MockState) -> datetime:
        return self._real_now() + timedelta(seconds=state.clock_offset_s)

    def _booking_read(self, booking: Booking, now: datetime) -> BookingRead:
        slot_time = catalog.parse_slot(booking.slot)
        is_past = slot_time is not None and slot_time <= now
        status = booking.status
        if status == ACTIVE_BOOKING and is_past:
            status = "completed"
        return BookingRead(
            booking_id=booking.id,
            provider_id=booking.provider_id,
            provider_name=booking.provider_name,
            service=booking.service,
            slot=booking.slot,
            price=booking.price,
            status=status,
            when="past" if is_past else "upcoming",
        )

    # Read tools

    async def search_providers(self, request: SearchProvidersInput) -> list[ProviderSummary]:
        service = catalog.normalize_service(request.service)
        if service is None:
            known = ", ".join(catalog.SERVICES)
            raise ToolError("unknown_service", 422, f"Unknown service. Known services: {known}.")

        async with self._db.session_scope() as session:
            repository = MockRepository(session)
            state = await repository.read_state(self._default_seed)
            now = self._now(state)
            days = (request.date_range.end - request.date_range.start).days + 1
            results = []
            for provider in catalog.select_providers(state.seed, service, request.area):
                claimed = await repository.claimed_slots(provider.id, now)
                slots = [
                    slot
                    for offset in range(days)
                    for slot in catalog.slots_on(
                        provider, request.date_range.start + timedelta(days=offset)
                    )
                    if slot not in claimed and (catalog.parse_slot(slot) or now) > now
                ]
                results.append(
                    ProviderSummary(
                        id=provider.id,
                        name=provider.name,
                        service=provider.service,
                        area=request.area,
                        price=self._list_price(state, provider),
                        currency=CURRENCY,
                        rating=provider.rating,
                        review_count=provider.review_count,
                        available_slots=slots,
                        attributes=_attributes(provider),
                    )
                )
        return results

    async def get_provider(self, provider_id: str) -> ProviderDetails:
        provider = CATALOG.get(provider_id)
        if provider is None:
            raise _not_found("provider")
        async with self._db.session_scope() as session:
            state = await MockRepository(session).read_state(self._default_seed)
            price = self._list_price(state, provider)
        return ProviderDetails(
            id=provider.id,
            name=provider.name,
            service=provider.service,
            price=price,
            currency=CURRENCY,
            rating=provider.rating,
            review_count=provider.review_count,
            attributes=_attributes(provider),
            reviews=[
                ReviewRead(author=review.author, rating=review.rating, text=review.text)
                for review in provider.reviews
            ],
        )

    async def list_bookings(self, user_id: str) -> list[BookingRead]:
        async with self._db.session_scope() as session:
            repository = MockRepository(session)
            state = await repository.read_state(self._default_seed)
            now = self._now(state)
            bookings = await repository.bookings_for_user(user_id)
            return [self._booking_read(booking, now) for booking in bookings]

    async def get_booking_status(
        self, user_id: str, hold_id: str | None, idempotency_key: str | None
    ) -> BookingStatusRead:
        """The source of truth. An unknown key means the call never took effect: not booked."""
        async with self._db.session_scope() as session:
            repository = MockRepository(session)
            if hold_id:
                return await self._status_by_hold(repository, user_id, hold_id)

            attempt = await repository.get_attempt(idempotency_key or "")
            if attempt is None:
                return BookingStatusRead(status="not_booked", idempotency_key=idempotency_key)
            if attempt.user_id != user_id:
                raise _not_found("booking")
            status = await self._status_by_hold(repository, user_id, attempt.hold_id)
            return status.model_copy(update={"idempotency_key": idempotency_key})

    async def _status_by_hold(
        self, repository: MockRepository, user_id: str, hold_id: str
    ) -> BookingStatusRead:
        hold = await repository.get_hold(hold_id)
        if hold is None or hold.user_id != user_id:
            raise _not_found("hold")
        booking = await repository.booking_for_hold(hold_id)
        if booking is None:
            return BookingStatusRead(status="not_booked", hold_id=hold_id, detail=hold.status)
        status = "booked" if booking.status == ACTIVE_BOOKING else booking.status
        return BookingStatusRead(status=status, booking_id=booking.id, hold_id=hold_id)

    # Side effects

    async def hold_slot(self, user_id: str, provider_id: str, slot: str) -> HoldRead:
        provider = CATALOG.get(provider_id)
        if provider is None:
            raise _not_found("provider")
        canonical = catalog.canonical_slot(slot)
        if canonical is None or not catalog.is_working_slot(provider, canonical):
            raise ToolError("invalid_slot", 422, "That provider does not work at that time.")

        try:
            async with self._db.session_scope() as session:
                repository = MockRepository(session)
                state = await repository.read_state(self._default_seed)
                now = self._now(state)
                if (catalog.parse_slot(canonical) or now) <= now:
                    raise ToolError("invalid_slot", 422, "That time has already passed.")

                expires_at = now + timedelta(minutes=HOLD_MINUTES)
                await repository.remove_expired_claim(provider_id, canonical, now)
                # Asking again for your own held slot replaces the hold, with a fresh quote.
                own = await repository.active_hold_for_slot(user_id, provider_id, canonical)
                if own is not None and now < own.expires_at:
                    own.status = "replaced"
                    await repository.release_claim(provider_id, canonical)
                await repository.add_claim(provider_id, canonical, "hold", expires_at)
                list_price = self._list_price(state, provider)
                hold = Hold(
                    id=f"hold-{uuid.uuid4().hex[:12]}",
                    user_id=user_id,
                    provider_id=provider_id,
                    slot=canonical,
                    quoted_price=list_price + self._quote_delta(state, provider_id),
                    search_price=list_price,
                    status="active",
                    expires_at=expires_at,
                    created_at=now,
                )
                await repository.add(hold)
        except IntegrityError as error:
            raise ToolError(
                "slot_unavailable", 409, "Someone else holds or has booked that slot."
            ) from error

        return HoldRead(
            hold_id=hold.id,
            provider_id=hold.provider_id,
            slot=hold.slot,
            search_price=hold.search_price,
            quoted_price=hold.quoted_price,
            currency=CURRENCY,
            expires_at=hold.expires_at,
        )

    def _list_price(self, state: MockState, provider: Provider) -> int:
        prices = state.scenario.get("provider_prices") or {}
        return int(prices.get(provider.id, provider.price))

    def _quote_delta(self, state: MockState, provider_id: str) -> int:
        deltas = state.scenario.get("quote_deltas")
        if deltas is None:
            deltas = catalog.DEFAULT_QUOTE_DELTAS
        return int(deltas.get(provider_id, 0))

    async def book(self, user_id: str, hold_id: str, idempotency_key: str) -> BookingRead:
        try:
            booking, simulate_timeout, hang_s = await self._book_once(
                user_id, hold_id, idempotency_key
            )
        except IntegrityError:
            # A concurrent call with this key won the race, so this is a replay.
            booking, simulate_timeout, hang_s = await self._book_once(
                user_id, hold_id, idempotency_key
            )
        if simulate_timeout:
            # The outcome is already committed. Only the reply is lost, as in a real timeout.
            await asyncio.sleep(hang_s)
            raise ToolError("timeout", 504, "The provider did not answer in time.")
        assert booking is not None
        return booking

    async def _book_once(
        self, user_id: str, hold_id: str, idempotency_key: str
    ) -> tuple[BookingRead | None, bool, float]:
        async with self._db.session_scope() as session:
            repository = MockRepository(session)
            state = await repository.state(self._default_seed)
            now = self._now(state)

            hold = await repository.get_hold(hold_id, lock=True)
            if hold is None or hold.user_id != user_id:
                raise _not_found("hold")

            attempt = await repository.get_attempt(idempotency_key)
            if attempt is not None:
                if attempt.hold_id != hold_id:
                    raise ToolError(
                        "idempotency_conflict", 409, "This key was used with a different hold."
                    )
                if attempt.result == BookOutcome.SLOT_TAKEN.value:
                    raise ToolError("slot_taken", 409, "Someone else took that slot.")
                if attempt.booking_id:
                    existing = await repository.get_booking(attempt.booking_id)
                    assert existing is not None
                    return self._booking_read(existing, now), False, 0.0

            if hold.status != "active":
                raise ToolError("hold_not_active", 409, "That hold has already been used.")
            if now >= hold.expires_at:
                raise ToolError("hold_expired", 410, "The hold has expired.")

            attempts = (attempt.attempts if attempt else 0) + 1
            outcome = self._next_outcome(state, idempotency_key, attempts)

            booking: Booking | None = None
            if outcome in (BookOutcome.SUCCESS, BookOutcome.TIMEOUT_BOOKED):
                booking = await self._create_booking(repository, hold, idempotency_key, now)
            elif outcome == BookOutcome.SLOT_TAKEN:
                hold.status = "lost"
                await repository.set_claim(hold.provider_id, hold.slot, "taken")

            if attempt is None:
                attempt = BookAttempt(
                    idempotency_key=idempotency_key,
                    hold_id=hold_id,
                    user_id=user_id,
                    attempts=attempts,
                    result=outcome.value,
                    booking_id=booking.id if booking else None,
                )
                await repository.add(attempt)
            else:
                attempt.attempts = attempts
                attempt.result = outcome.value
                attempt.booking_id = booking.id if booking else None

            read = self._booking_read(booking, now) if booking else None
            hang_s = float(state.scenario.get("timeout_hang_s") or self._timeout_hang_s)

        if outcome == BookOutcome.SLOT_TAKEN:
            raise ToolError("slot_taken", 409, "Someone else took that slot.")
        simulate_timeout = outcome in (BookOutcome.TIMEOUT_BOOKED, BookOutcome.TIMEOUT_NOT_BOOKED)
        return read, simulate_timeout, hang_s

    def _next_outcome(self, state: MockState, key: str, attempt: int) -> BookOutcome:
        script = state.scenario.get("book_outcomes") or []
        if state.book_outcomes_used < len(script):
            outcome = BookOutcome(script[state.book_outcomes_used])
            state.book_outcomes_used += 1
            return outcome
        return decide_book_outcome(state.seed, key, attempt)

    async def _create_booking(
        self, repository: MockRepository, hold: Hold, idempotency_key: str, now: datetime
    ) -> Booking:
        provider = CATALOG[hold.provider_id]
        booking = Booking(
            id=f"bkg-{uuid.uuid4().hex[:12]}",
            user_id=hold.user_id,
            provider_id=hold.provider_id,
            provider_name=provider.name,
            service=provider.service,
            slot=hold.slot,
            price=hold.quoted_price,
            status=ACTIVE_BOOKING,
            hold_id=hold.id,
            idempotency_key=idempotency_key,
            cancel_calls=0,
            created_at=now,
        )
        await repository.add(booking)
        hold.status = "used"
        await repository.set_claim(hold.provider_id, hold.slot, "booking")
        return booking

    async def cancel(self, user_id: str, booking_id: str, idempotency_key: str) -> BookingRead:
        async with self._db.session_scope() as session:
            repository = MockRepository(session)
            state = await repository.read_state(self._default_seed)
            now = self._now(state)

            booking = await repository.get_booking(booking_id, lock=True)
            if booking is None or booking.user_id != user_id:
                raise _not_found("booking")

            replay = await repository.get_keyed_result(idempotency_key)
            if replay is not None:
                self._check_replay(replay, "cancel", booking_id)
                return BookingRead(**replay.response)
            if booking.status != ACTIVE_BOOKING:
                raise ToolError("booking_not_active", 409, "That booking is not active.")

            booking.cancel_calls += 1
            first_call = booking.cancel_calls == 1
            if not first_call:
                booking.status = "cancelled"
                await repository.release_claim(booking.provider_id, booking.slot)
                read = self._booking_read(booking, now)
                await repository.add(
                    KeyedResult(
                        idempotency_key=idempotency_key,
                        operation="cancel",
                        booking_id=booking_id,
                        response=read.model_dump(),
                    )
                )

        if first_call:
            # Raised after the commit, so the retry sees this call was counted.
            raise ToolError("transient_error", 503, "Temporary failure. Please retry.")
        return read

    async def reschedule(
        self, user_id: str, booking_id: str, slot: str, idempotency_key: str
    ) -> BookingRead:
        try:
            async with self._db.session_scope() as session:
                repository = MockRepository(session)
                state = await repository.read_state(self._default_seed)
                now = self._now(state)

                booking = await repository.get_booking(booking_id, lock=True)
                if booking is None or booking.user_id != user_id:
                    raise _not_found("booking")

                replay = await repository.get_keyed_result(idempotency_key)
                if replay is not None:
                    self._check_replay(replay, "reschedule", booking_id)
                    return BookingRead(**replay.response)
                if booking.status != ACTIVE_BOOKING:
                    raise ToolError("booking_not_active", 409, "That booking is not active.")

                provider = CATALOG[booking.provider_id]
                canonical = catalog.canonical_slot(slot)
                if canonical is None or not catalog.is_working_slot(provider, canonical):
                    raise ToolError(
                        "invalid_slot", 422, "That provider does not work at that time."
                    )
                if (catalog.parse_slot(canonical) or now) <= now:
                    raise ToolError("invalid_slot", 422, "That time has already passed.")
                if self._is_configured_conflict(state, canonical):
                    raise ToolError("slot_conflict", 409, "That slot conflicts. Pick another.")

                if canonical != booking.slot:
                    await repository.remove_expired_claim(booking.provider_id, canonical, now)
                    await repository.add_claim(booking.provider_id, canonical, "booking", None)
                    await repository.release_claim(booking.provider_id, booking.slot)
                    booking.slot = canonical

                read = self._booking_read(booking, now)
                await repository.add(
                    KeyedResult(
                        idempotency_key=idempotency_key,
                        operation="reschedule",
                        booking_id=booking_id,
                        response=read.model_dump(),
                    )
                )
        except IntegrityError as error:
            raise ToolError("slot_conflict", 409, "That slot conflicts. Pick another.") from error
        return read

    def _check_replay(self, replay: KeyedResult, operation: str, booking_id: str) -> None:
        if replay.operation != operation or replay.booking_id != booking_id:
            raise ToolError(
                "idempotency_conflict", 409, "This key was used for a different action."
            )

    def _is_configured_conflict(self, state: MockState, canonical_slot: str) -> bool:
        configured = state.scenario.get("slot_conflicts")
        if configured is not None:
            return canonical_slot in {catalog.canonical_slot(slot) for slot in configured}
        parsed = catalog.parse_slot(canonical_slot)
        return parsed is not None and parsed.hour == catalog.DEFAULT_CONFLICT_HOUR

    # Eval-only controls

    async def configure_scenario(self, config: ScenarioConfig) -> None:
        async with self._db.session_scope() as session:
            state = await MockRepository(session).state(self._default_seed)
            if config.seed is not None:
                state.seed = config.seed
            if config.reset_clock:
                state.clock_offset_s = 0.0
            state.scenario = config.model_dump(
                mode="json", exclude={"seed", "reset_clock"}, exclude_none=True
            )
            state.book_outcomes_used = 0

    async def advance_clock(self, seconds: float) -> datetime:
        async with self._db.session_scope() as session:
            state = await MockRepository(session).state(self._default_seed)
            state.clock_offset_s += seconds
            return self._now(state)

    async def provider_cancel(self, booking_id: str) -> BookingRead:
        """The simulated event: the provider drops a booking without the user asking."""
        async with self._db.session_scope() as session:
            repository = MockRepository(session)
            state = await repository.read_state(self._default_seed)
            booking = await repository.get_booking(booking_id, lock=True)
            if booking is None:
                raise _not_found("booking")
            if booking.status != ACTIVE_BOOKING:
                raise ToolError("booking_not_active", 409, "That booking is not active.")
            booking.status = "cancelled_by_provider"
            await repository.release_claim(booking.provider_id, booking.slot)
            return self._booking_read(booking, self._now(state))

    async def reset(self) -> None:
        async with self._db.session_scope() as session:
            await MockRepository(session).wipe_all()
            await seed_demo_bookings(session, self._real_now())
