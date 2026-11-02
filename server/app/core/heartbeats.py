"""Liveness keys for processes that have no HTTP port."""

from redis.asyncio import Redis

WORKER_HEARTBEAT_PREFIX = "worker:"
SCHEDULER_HEARTBEAT = "scheduler"


class Heartbeats:
    def __init__(self, redis: Redis, ttl_s: int) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    async def beat(self, name: str) -> None:
        await self._redis.set(f"heartbeat:{name}", "1", ex=self._ttl_s)

    async def is_alive(self, name: str) -> bool:
        return bool(await self._redis.exists(f"heartbeat:{name}"))

    async def any_alive(self, prefix: str) -> bool:
        async for _ in self._redis.scan_iter(match=f"heartbeat:{prefix}*", count=100):
            return True
        return False
