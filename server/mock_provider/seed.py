"""Past and upcoming bookings for the demo user, so "same as last time" has something to find."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from mock_provider.catalog import CATALOG, LOCAL_TZ
from mock_provider.models import Booking
from mock_provider.repository import MockRepository

DEMO_USER = "demo-user"

# Booking ID, provider, days from the seeding moment, hour. Fixed IDs make seeding repeatable.
_DEMO_BOOKINGS = (
    ("bkg-demo-electrician", "ele-02", -21, 10),
    ("bkg-demo-plumber", "plm-05", -60, 11),
    ("bkg-demo-ac-servicing", "acs-01", 4, 10),
)


async def seed_demo_bookings(session: AsyncSession, now: datetime) -> None:
    repository = MockRepository(session)
    for booking_id, provider_id, day_offset, hour in _DEMO_BOOKINGS:
        if await repository.get_booking(booking_id) is not None:
            continue
        provider = CATALOG[provider_id]
        day = (now + timedelta(days=day_offset)).astimezone(LOCAL_TZ).date()
        slot = datetime(day.year, day.month, day.day, hour, tzinfo=LOCAL_TZ).isoformat()
        await repository.add(
            Booking(
                id=booking_id,
                user_id=DEMO_USER,
                provider_id=provider_id,
                provider_name=provider.name,
                service=provider.service,
                slot=slot,
                price=provider.price,
                status="confirmed",
                hold_id=None,
                idempotency_key=None,
                cancel_calls=0,
                created_at=now,
            )
        )
        await repository.set_claim(provider_id, slot, "booking")
