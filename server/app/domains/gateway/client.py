"""The HTTP client for the provider. Nothing else in the app talks to the provider."""

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

UNAVAILABLE = 0
TIMED_OUT = -1


@dataclass(frozen=True)
class ProviderResponse:
    """`status_code` is the HTTP status, or UNAVAILABLE or TIMED_OUT if no reply arrived."""

    status_code: int
    body: dict[str, Any]


class ProviderClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)

    async def call(self, tool: str, payload: dict[str, Any], user_id: str) -> ProviderResponse:
        try:
            # httpx only bounds each read. This bounds the whole call.
            async with asyncio.timeout(self._timeout_s):
                response = await self._http.post(
                    f"/tools/{tool}", json=payload, headers={"X-User-Id": user_id}
                )
        except (TimeoutError, httpx.TimeoutException):
            return ProviderResponse(TIMED_OUT, {})
        except httpx.TransportError:
            return ProviderResponse(UNAVAILABLE, {})

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return ProviderResponse(response.status_code, {"error": "bad_response"})
        return ProviderResponse(response.status_code, body)

    async def is_reachable(self) -> bool:
        try:
            response = await self._http.get("/healthz", timeout=3)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def close(self) -> None:
        await self._http.aclose()
