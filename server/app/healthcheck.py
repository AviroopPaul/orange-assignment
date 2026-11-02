"""Compose health check for processes with no HTTP port.

Usage: python -m app.healthcheck worker|scheduler
"""

import asyncio
import socket
import sys

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.heartbeats import SCHEDULER_HEARTBEAT, WORKER_HEARTBEAT_PREFIX, Heartbeats


async def is_healthy(target: str) -> bool:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    try:
        heartbeats = Heartbeats(redis, settings.heartbeat_ttl_s)
        if target == "worker":
            return await heartbeats.is_alive(f"{WORKER_HEARTBEAT_PREFIX}{socket.gethostname()}")
        return await heartbeats.is_alive(SCHEDULER_HEARTBEAT)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(is_healthy(sys.argv[1])) else 1)
