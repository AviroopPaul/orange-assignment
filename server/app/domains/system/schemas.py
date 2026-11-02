import uuid

from pydantic import BaseModel


class Readiness(BaseModel):
    ready: bool
    postgres: bool
    redis: bool


class SystemStatus(Readiness):
    worker: bool
    scheduler: bool
    provider: bool
    model_configured: bool


class PingRead(BaseModel):
    id: uuid.UUID
    status: str
    attempts: int
    last_error: str | None
