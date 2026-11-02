import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.domains.system.schemas import PingRead, Readiness, SystemStatus
from app.domains.system.service import PingNotFound, SystemService
from app.web import get_system_service

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", response_model=Readiness)
async def readyz(
    response: Response, system: SystemService = Depends(get_system_service)
) -> Readiness:
    readiness = await system.readiness()
    if not readiness.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return readiness


@router.get("/system/status", response_model=SystemStatus)
async def system_status(system: SystemService = Depends(get_system_service)) -> SystemStatus:
    return await system.status()


@router.post("/system/ping", response_model=PingRead, status_code=status.HTTP_202_ACCEPTED)
async def request_ping(system: SystemService = Depends(get_system_service)) -> PingRead:
    return await system.request_ping()


@router.get("/system/ping/{job_id}", response_model=PingRead)
async def get_ping(
    job_id: uuid.UUID, system: SystemService = Depends(get_system_service)
) -> PingRead:
    try:
        return await system.get_ping(job_id)
    except PingNotFound as error:
        raise HTTPException(status_code=404, detail="Ping not found") from error
