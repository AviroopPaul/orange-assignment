"""taskiq broker on a Redis stream, hardened for Redis loss."""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from taskiq import AckableMessage
from taskiq.kicker import AsyncKicker
from taskiq_redis import RedisStreamBroker

from app.core.config import Settings

logger = logging.getLogger(__name__)

RUN_JOB_TASK = "jobs.run"
SWEEP_TASK = "jobs.sweep"


class ResilientStreamBroker(RedisStreamBroker):
    """Keeps listening when Redis restarts or is wiped.

    A wipe removes the consumer group. The base broker then raises NOGROUP out of `listen`, which
    would end the worker's receive loop.
    """

    retry_delay_s = 1.0

    async def listen(self) -> AsyncGenerator[AckableMessage, None]:
        while True:
            try:
                async for message in super().listen():
                    yield message
            except ResponseError as error:
                if "NOGROUP" not in str(error):
                    raise
                logger.warning("consumer group is gone, declaring it again")
                await self._redeclare_group()
            except (RedisConnectionError, RedisTimeoutError, OSError):
                logger.warning("lost Redis, retrying in %.0f s", self.retry_delay_s)
                await asyncio.sleep(self.retry_delay_s)

    async def _redeclare_group(self) -> None:
        try:
            await self._declare_consumer_group()
        except (RedisConnectionError, RedisTimeoutError, OSError):
            await asyncio.sleep(self.retry_delay_s)


def build_broker(settings: Settings) -> ResilientStreamBroker:
    return ResilientStreamBroker(
        url=settings.redis_url,
        queue_name=settings.broker_queue_name,
        consumer_group_name="errand-workers",
        # "$" would skip jobs sent before the group exists. Repeats are safe, losses are not.
        consumer_id="0",
        idle_timeout=int(settings.broker_idle_timeout_s * 1000),
        # The default never expires, so a worker dying inside this lock could block reclaim.
        unacknowledged_lock_timeout=5.0,
        socket_connect_timeout=3,
    )


class TaskiqJobPublisher:
    def __init__(self, broker: RedisStreamBroker) -> None:
        self._broker = broker

    async def publish(self, job_id: uuid.UUID) -> None:
        kicker: AsyncKicker = AsyncKicker(task_name=RUN_JOB_TASK, broker=self._broker, labels={})
        await kicker.kiq(str(job_id))
