"""The tool gateway: the only path to any tool.

Every call passes these checks, in order: budget, allowlist, caller rights, schema, user binding,
approval. Only then does it reach the provider, with retries and a timeout. The result is sanitized
and traced. A refusal is a normal result, so the caller can explain it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol

from pydantic import ValidationError

from app.domains.gateway.client import TIMED_OUT, UNAVAILABLE, ProviderClient, ProviderResponse
from app.domains.gateway.registry import TOOLS, ToolSpec
from app.domains.gateway.sanitize import for_trace, sanitize_result
from app.domains.gateway.schemas import Caller, RefusalReason, ToolResult, ToolStatus
from app.domains.tracing.service import RunRecorder

logger = logging.getLogger(__name__)


class ToolBudgetExhausted(Exception):
    """The run spent its tool calls. This ends the run, so the model cannot talk around it."""


class ToolBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def spend(self) -> None:
        self.used += 1


@dataclass
class CallContext:
    """Set by code for each run. The model never supplies any of it."""

    user_id: str
    caller: Caller
    budget: ToolBudget
    recorder: RunRecorder | None = None
    approval_token: str | None = field(default=None, repr=False)
    guard: "ToolGuard | None" = None


class ToolGuard(Protocol):
    """A run's extra rule for a tool. Returns why a call must be refused, or None to allow it."""

    async def check(self, tool: str, args: dict[str, Any]) -> tuple[RefusalReason, str] | None: ...


class ApprovalVerifier(Protocol):
    async def is_valid(
        self, token: str | None, tool: str, args: dict[str, Any], user_id: str
    ) -> bool: ...


class DenyAllApprovals:
    """Used until the approvals domain exists: every side effect that needs approval is refused."""

    async def is_valid(
        self, token: str | None, tool: str, args: dict[str, Any], user_id: str
    ) -> bool:
        return False


class ToolGateway:
    def __init__(
        self,
        client: ProviderClient,
        approvals: ApprovalVerifier,
        retry_backoff_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._approvals = approvals
        self._retry_backoff_s = retry_backoff_s
        self._sleep = sleep

    async def call(self, tool: str, args: dict[str, Any], context: CallContext) -> ToolResult:
        admitted = await self._admit(tool, args, context)
        if isinstance(admitted, ToolResult):
            return admitted
        spec, payload = admitted
        if spec.local:
            return await self._refuse(tool, args, context, RefusalReason.NOT_ALLOWED)
        return await self._execute(spec, payload, context)

    async def call_local(
        self,
        tool: str,
        args: dict[str, Any],
        context: CallContext,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> ToolResult:
        """A tool our own code answers. It passes the same checks and is counted and traced."""
        admitted = await self._admit(tool, args, context)
        if isinstance(admitted, ToolResult):
            return admitted
        spec, payload = admitted
        if not spec.local:
            return await self._refuse(tool, args, context, RefusalReason.NOT_ALLOWED)
        started_at = monotonic()
        data = await handler(payload)
        if "error" in data:
            result = ToolResult(
                tool=tool,
                status=ToolStatus.ERROR,
                error_code=str(data["error"]),
                message=str(data.get("message", "")),
                attempts=1,
            )
        else:
            result = ToolResult(tool=tool, status=ToolStatus.OK, data=data, attempts=1)
        await self._record_call(tool, payload, context, result, started_at)
        return result

    async def _admit(
        self, tool: str, args: dict[str, Any], context: CallContext
    ) -> tuple[ToolSpec, dict[str, Any]] | ToolResult:
        if context.budget.exhausted:
            await self._refuse(tool, args, context, RefusalReason.BUDGET_EXHAUSTED)
            raise ToolBudgetExhausted(f"limit of {context.budget.limit} tool calls reached")
        # Refused and invented calls count too, or a looping model would never hit the limit.
        context.budget.spend()

        spec = TOOLS.get(tool)
        if spec is None:
            return await self._refuse(tool, args, context, RefusalReason.UNKNOWN_TOOL)
        if context.caller not in spec.callers:
            return await self._refuse(tool, args, context, RefusalReason.NOT_ALLOWED)
        if context.guard is not None:
            blocked = await context.guard.check(tool, args)
            if blocked is not None:
                reason, detail = blocked
                return await self._refuse(tool, args, context, reason, detail=detail)

        try:
            validated = spec.args_model.model_validate(args)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in error.errors()
            )
            return await self._refuse(
                tool, args, context, RefusalReason.INVALID_ARGUMENTS, detail=details[:300]
            )

        payload = validated.model_dump(mode="json", exclude_none=True)
        if payload.get("user_id", context.user_id) != context.user_id:
            return await self._refuse(tool, args, context, RefusalReason.WRONG_USER)
        if spec.requires_approval and not await self._approved(tool, payload, context):
            return await self._refuse(tool, payload, context, RefusalReason.APPROVAL_REQUIRED)

        return spec, payload

    async def is_provider_reachable(self) -> bool:
        return await self._client.is_reachable()

    async def _approved(self, tool: str, payload: dict[str, Any], context: CallContext) -> bool:
        try:
            return await self._approvals.is_valid(
                context.approval_token, tool, payload, context.user_id
            )
        except Exception:
            # A broken check must never read as a yes.
            logger.exception("approval check failed for %s", tool)
            return False

    async def _execute(
        self, spec: ToolSpec, payload: dict[str, Any], context: CallContext
    ) -> ToolResult:
        started_at = monotonic()
        response, attempts = await self._send_with_retries(spec, payload, context)
        result = self._to_result(spec, response, attempts)
        await self._record_call(spec.name, payload, context, result, started_at)
        return result

    async def _send_with_retries(
        self, spec: ToolSpec, payload: dict[str, Any], context: CallContext
    ) -> tuple[ProviderResponse, int]:
        attempt = 0
        while True:
            attempt += 1
            response = await self._client.call(spec.name, payload, context.user_id)
            if attempt >= spec.retry.attempts or not self._should_retry(spec, response):
                return response, attempt
            await self._sleep(self._retry_backoff_s * 2 ** (attempt - 1))

    def _should_retry(self, spec: ToolSpec, response: ProviderResponse) -> bool:
        if response.status_code in (TIMED_OUT, UNAVAILABLE):
            return spec.retry.on_timeout
        return response.status_code in spec.retry.on_statuses

    def _to_result(self, spec: ToolSpec, response: ProviderResponse, attempts: int) -> ToolResult:
        code = response.status_code
        if code == TIMED_OUT:
            return ToolResult(
                tool=spec.name,
                status=ToolStatus.TIMEOUT,
                error_code="timeout",
                message="The provider did not answer in time.",
                attempts=attempts,
                ambiguous=spec.side_effect,
            )
        if code == UNAVAILABLE:
            return ToolResult(
                tool=spec.name,
                status=ToolStatus.ERROR,
                error_code="provider_unavailable",
                message="The provider could not be reached.",
                attempts=attempts,
                # The request may have left before the connection failed.
                ambiguous=spec.side_effect,
            )
        if 200 <= code < 300 and "error" not in response.body:
            data, flags = sanitize_result(response.body)
            return ToolResult(
                tool=spec.name,
                status=ToolStatus.OK,
                data=data,
                attempts=attempts,
                injection_flags=flags,
            )

        error_code = str(response.body.get("error") or self._code_for_status(code))
        return ToolResult(
            tool=spec.name,
            status=ToolStatus.TIMEOUT if code == 504 else ToolStatus.ERROR,
            error_code=error_code,
            message=str(response.body.get("message") or "The provider returned an error."),
            attempts=attempts,
            # A 504 from the provider means the same as a client timeout: the outcome is unknown.
            ambiguous=spec.side_effect and code == 504,
        )

    def _code_for_status(self, code: int) -> str:
        if code == 422:
            return "invalid_arguments"
        return "provider_error"

    async def _refuse(
        self,
        tool: str,
        args: dict[str, Any],
        context: CallContext,
        reason: RefusalReason,
        detail: str | None = None,
    ) -> ToolResult:
        if context.recorder is not None:
            await context.recorder.event(
                "tool_refused",
                name=tool[:128],
                data={
                    "caller": context.caller.value,
                    "reason": reason.value,
                    "args": for_trace(args),
                    "detail": detail,
                },
            )
        message = f"Tool '{tool}' is not available. No action was taken."
        if reason == RefusalReason.INVALID_ARGUMENTS:
            message = f"Invalid arguments for '{tool}': {detail}. No action was taken."
        elif reason == RefusalReason.APPROVAL_REQUIRED:
            message = f"'{tool}' needs the user's approval. No action was taken."
        elif reason in (RefusalReason.MISSING_DETAILS, RefusalReason.REQUEST_MISMATCH):
            message = detail or message
        return ToolResult(
            tool=tool, status=ToolStatus.REFUSED, error_code=reason.value, message=message
        )

    async def _record_call(
        self,
        tool: str,
        payload: dict[str, Any],
        context: CallContext,
        result: ToolResult,
        started_at: float,
    ) -> None:
        if context.recorder is None:
            return
        await context.recorder.event(
            "tool_call",
            name=tool,
            latency_ms=(monotonic() - started_at) * 1000,
            data={
                "caller": context.caller.value,
                "args": for_trace(payload),
                "status": result.status.value,
                "error_code": result.error_code,
                "attempts": result.attempts,
                "ambiguous": result.ambiguous,
                "injection_flags": list(result.injection_flags),
                "result": for_trace(result.data),
            },
        )
