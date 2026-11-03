from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse

from app.core.identifiers import IDENTIFIER_PATTERN
from app.domains.notifications.schemas import NotificationList, NotificationRead
from app.domains.notifications.service import NotificationService
from app.web import get_notification_service

router = APIRouter(tags=["notifications"])

RECONNECT_AFTER_MS = 3000


def _resume_from(after: int, last_event_id: str | None) -> int:
    """A browser sends Last-Event-ID only on its own reconnect. Its first request sends `after`."""
    try:
        return max(after, int(last_event_id or 0))
    except ValueError:
        return after


def _frame(item: NotificationRead | None) -> str:
    if item is None:
        return ": still here\n\n"
    return f"id: {item.id}\nevent: notification\ndata: {item.model_dump_json()}\n\n"


async def _frames(items: AsyncIterator[NotificationRead | None]) -> AsyncIterator[str]:
    yield f"retry: {RECONNECT_AFTER_MS}\n\n"
    async for item in items:
        yield _frame(item)


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    user_id: str = Query(pattern=IDENTIFIER_PATTERN),
    session_id: str = Query(pattern=IDENTIFIER_PATTERN),
    after: int = Query(default=0, ge=0),
    notifications: NotificationService = Depends(get_notification_service),
) -> NotificationList:
    rows = await notifications.list_after(user_id, session_id, after)
    return NotificationList(notifications=rows)


@router.get("/events")
async def events(
    user_id: str = Query(pattern=IDENTIFIER_PATTERN),
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    notifications: NotificationService = Depends(get_notification_service),
) -> StreamingResponse:
    """Stored first, so a reconnect replays what it missed. Then new ones as they are stored."""
    return StreamingResponse(
        _frames(notifications.stream(user_id, _resume_from(after, last_event_id))),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
