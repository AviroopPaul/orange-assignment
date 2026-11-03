import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.identifiers import IDENTIFIER_PATTERN
from app.domains.bookings.schemas import (
    ApprovalDecision,
    ApprovalRead,
    BookingState,
    DecisionRequest,
)
from app.domains.bookings.service import ApprovalNotFound, ApprovalService
from app.web import get_approval_service

router = APIRouter(tags=["approvals"])


@router.get("/bookings/current", response_model=BookingState)
async def current_booking(
    user_id: str = Query(pattern=IDENTIFIER_PATTERN),
    session_id: str = Query(pattern=IDENTIFIER_PATTERN),
    approvals: ApprovalService = Depends(get_approval_service),
) -> BookingState:
    view = await approvals.view(user_id, session_id)
    return BookingState(approval=view.card, attempt_open=view.attempt_open)


@router.get("/approvals/{approval_id}", response_model=ApprovalRead)
async def get_approval(
    approval_id: uuid.UUID,
    user_id: str = Query(pattern=IDENTIFIER_PATTERN),
    approvals: ApprovalService = Depends(get_approval_service),
) -> ApprovalRead:
    try:
        return await approvals.read(approval_id, user_id)
    except ApprovalNotFound as error:
        raise HTTPException(status_code=404, detail="approval not found") from error


@router.post("/approvals/{approval_id}/confirm", response_model=ApprovalDecision)
async def confirm_approval(
    approval_id: uuid.UUID,
    body: DecisionRequest,
    approvals: ApprovalService = Depends(get_approval_service),
) -> ApprovalDecision:
    try:
        return await approvals.confirm_click(approval_id, body.user_id)
    except ApprovalNotFound as error:
        raise HTTPException(status_code=404, detail="approval not found") from error


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalDecision)
async def reject_approval(
    approval_id: uuid.UUID,
    body: DecisionRequest,
    approvals: ApprovalService = Depends(get_approval_service),
) -> ApprovalDecision:
    try:
        return await approvals.reject_click(approval_id, body.user_id)
    except ApprovalNotFound as error:
        raise HTTPException(status_code=404, detail="approval not found") from error
