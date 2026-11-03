"""The approval hash, and the check the gateway runs before any side effect."""

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.core.database import Database
from app.domains.bookings.models import Approval, ApprovalStatus, AttemptStatus
from app.domains.bookings.repository import BookingRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovedFacts:
    """Everything material the user agreed to. Change any of it and the hash no longer matches."""

    action: str
    user_id: str
    provider_id: str
    provider_name: str
    slot: str
    price: int
    list_price: int
    currency: str
    hold_id: str
    expires_at: str

    def hash(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def expiry_text(expires_at: datetime) -> str:
    return expires_at.astimezone(UTC).isoformat()


def facts_of(approval: Approval) -> ApprovedFacts:
    return ApprovedFacts(
        action=approval.action,
        user_id=approval.user_id,
        provider_id=approval.provider_id,
        provider_name=approval.provider_name,
        slot=approval.slot,
        price=approval.price,
        list_price=approval.list_price,
        currency=approval.currency,
        hold_id=approval.hold_id,
        expires_at=expiry_text(approval.expires_at),
    )


def idempotency_key(action: str, approval_id: uuid.UUID) -> str:
    return f"{action}:{approval_id}"


def token_for(approval: Approval, current_list_price: int) -> str:
    """Hash what the saga sees right now. A price that moved gives a token the gateway refuses."""
    current = replace(facts_of(approval), list_price=current_list_price)
    return f"{approval.id}:{current.hash()}"


class BookingApprovalVerifier:
    """Valid only for the consumed approval behind this exact call, with unchanged facts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_valid(
        self, token: str | None, tool: str, args: dict[str, Any], user_id: str
    ) -> bool:
        approval_id, _, current_hash = (token or "").partition(":")
        try:
            parsed_id = uuid.UUID(approval_id)
        except ValueError:
            return False

        async with self._db.session_scope() as session:
            repository = BookingRepository(session)
            approval = await repository.get_approval(parsed_id)
            if approval is None:
                return False
            attempt = await repository.attempt_for_approval(parsed_id)

        if attempt is None or attempt.status != AttemptStatus.OPEN.value:
            return False
        checks = {
            "consumed": approval.status == ApprovalStatus.CONSUMED.value,
            "user": approval.user_id == user_id == attempt.user_id,
            "action": approval.action == tool,
            "hold": args.get("hold_id") == approval.hold_id == attempt.hold_id,
            "key": args.get("idempotency_key")
            == attempt.idempotency_key
            == idempotency_key(tool, parsed_id),
            # The row itself has not been edited since the user approved it.
            "row": facts_of(approval).hash() == approval.approval_hash,
            # What the saga sees right now still matches what the user approved.
            "current": current_hash == approval.approval_hash,
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            logger.warning("approval %s refused for %s: %s", approval_id, tool, failed)
        return not failed
