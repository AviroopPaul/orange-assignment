import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.domains.tracing.schemas import RunTraceRead
from app.domains.tracing.service import RunNotFound, TraceService
from app.web import get_trace_service

router = APIRouter(tags=["tracing"])


@router.get("/runs/{run_id}/trace", response_model=RunTraceRead)
async def get_run_trace(
    run_id: uuid.UUID, traces: TraceService = Depends(get_trace_service)
) -> RunTraceRead:
    try:
        return await traces.get_trace(run_id)
    except RunNotFound as error:
        raise HTTPException(status_code=404, detail="Run not found") from error
