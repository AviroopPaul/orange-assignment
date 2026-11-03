import uuid
from typing import Any

from pydantic import BaseModel

from app.domains.workflow.models import WorkflowState


class RequestSnapshot(BaseModel):
    workflow_id: uuid.UUID | None
    state: WorkflowState
    details: dict[str, Any]
    missing_required: list[str]
    optional_unset: list[str]
    dates_to_show: dict[str, Any] | None = None

    def for_prompt(self) -> str:
        saved = ", ".join(f"{name}={value}" for name, value in self.details.items()) or "nothing"
        missing = ", ".join(self.missing_required) or "nothing"
        optional = ", ".join(self.optional_unset) or "nothing"
        return (
            f"Saved so far: {saved}.\n"
            f"Still needed before searching: {missing}.\n"
            f"Not given (ask once at most, only if it helps): {optional}."
        )
