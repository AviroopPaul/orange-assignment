"""Tells the api that a user has news. Only a wake-up: the news itself is in PostgreSQL.

A lost wake-up costs a short delay, because every reader also looks again on a timer. So Redis being
down slows delivery and loses nothing.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def _channel(user_id: str) -> str:
    return f"news:{user_id}"


class WakeupListener:
    def __init__(self, redis: Redis, channel: str) -> None:
        self._redis = redis
        self._channel = channel
        self._pubsub: Any | None = None

    async def _subscribe(self) -> None:
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(self._channel)
            self._pubsub = pubsub
        except (RedisError, OSError):
            logger.warning("could not subscribe to %s", self._channel)
            self._pubsub = None

    async def wait(self, timeout_s: float) -> bool:
        """True if a wake-up came. False can also mean Redis is down, which the timer covers."""
        if self._pubsub is None:
            await self._subscribe()
        if self._pubsub is None:
            await asyncio.sleep(timeout_s)
            return False
        deadline = monotonic() + timeout_s
        try:
            # The answer to the subscribe also comes back as nothing, so keep waiting after it.
            while (left := deadline - monotonic()) > 0:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=left
                )
                if message is not None:
                    return True
        except (RedisError, OSError):
            logger.warning("lost the subscription to %s", self._channel)
            await self.close()
            await asyncio.sleep(timeout_s)
        return False

    async def close(self) -> None:
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except (RedisError, OSError):
                logger.warning("could not close the subscription to %s", self._channel)


class Wakeups:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, user_id: str) -> None:
        try:
            await self._redis.publish(_channel(user_id), "1")
        except (RedisError, OSError):
            logger.warning("could not send a wake-up for %s", user_id)

    @asynccontextmanager
    async def listen(self, user_id: str) -> AsyncIterator[WakeupListener]:
        listener = WakeupListener(self._redis, _channel(user_id))
        # Subscribed before the caller's first read, so nothing falls between the two.
        await listener._subscribe()
        try:
            yield listener
        finally:
            await listener.close()
