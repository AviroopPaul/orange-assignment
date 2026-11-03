"""Per-session turn lock on Redis.

ADK rejects a write from anyone holding an older copy of a session, so two turns on one session
must not overlap. The lock has an owner token and an expiry longer than the run deadline. If it
ever expires early, ADK's StaleSessionError is the backstop.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import LockError, LockNotOwnedError, RedisError

logger = logging.getLogger(__name__)


class TurnLockTimeout(Exception):
    """Another turn held the session for the whole wait."""


class TurnLockUnavailable(Exception):
    """Redis could not be reached, so the turn fails closed."""


class RedisTurnLock:
    def __init__(self, redis: Redis, ttl_s: float, wait_s: float) -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        self._wait_s = wait_s

    @asynccontextmanager
    async def hold(self, key: str, wait_s: float | None = None) -> AsyncIterator[None]:
        """Background work passes `wait_s=0`, so it comes back later and never delays a turn."""
        lock = self._redis.lock(f"turn-lock:{key}", timeout=self._ttl_s, sleep=0.05)
        try:
            acquired = await lock.acquire(
                blocking_timeout=self._wait_s if wait_s is None else wait_s
            )
        except (RedisError, OSError) as error:
            raise TurnLockUnavailable(str(error)) from error
        if not acquired:
            raise TurnLockTimeout(key)
        try:
            yield
        finally:
            await self._release(lock, key)

    async def _release(self, lock: Any, key: str) -> None:
        try:
            await lock.release()
        except LockNotOwnedError:
            logger.warning("turn lock for %s expired before release", key)
        except (LockError, RedisError, OSError):
            logger.exception("could not release turn lock for %s", key)
