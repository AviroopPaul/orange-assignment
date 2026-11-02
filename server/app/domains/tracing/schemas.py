import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TraceEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    name: str | None
    data: dict
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    created_at: datetime


class RunTraceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: str
    session_id: str
    outcome: str | None
    reason: str | None
    reply: str | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    started_at: datetime
    finished_at: datetime | None
    events: list[TraceEventRead]
