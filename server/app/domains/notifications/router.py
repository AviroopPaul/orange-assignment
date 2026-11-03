from fastapi import APIRouter, Depends, Query

from app.core.identifiers import IDENTIFIER_PATTERN
from app.domains.notifications.schemas import NotificationList
from app.domains.notifications.service import NotificationService
from app.web import get_notification_service

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    user_id: str = Query(pattern=IDENTIFIER_PATTERN),
    session_id: str = Query(pattern=IDENTIFIER_PATTERN),
    after: int = Query(default=0, ge=0),
    notifications: NotificationService = Depends(get_notification_service),
) -> NotificationList:
    rows = await notifications.list_after(user_id, session_id, after)
    return NotificationList(notifications=rows)
