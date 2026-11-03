"""The worker process: `taskiq worker app.worker:broker`."""

import asyncio
import logging
import socket

from taskiq import TaskiqEvents, TaskiqState

from app.container import Core, build_core
from app.core.config import get_settings
from app.core.heartbeats import WORKER_HEARTBEAT_PREFIX
from app.domains.jobs.broker import build_broker
from app.domains.jobs.tasks import register_tasks

logger = logging.getLogger(__name__)

settings = get_settings()
broker = build_broker(settings)
register_tasks(broker, settings.sweep_interval_s, settings.provider_poll_interval_s)


def worker_heartbeat_name() -> str:
    return f"{WORKER_HEARTBEAT_PREFIX}{socket.gethostname()}"


async def _beat_forever(core: Core) -> None:
    while True:
        try:
            await core.heartbeats.beat(worker_heartbeat_name())
        except Exception:
            logger.warning("could not write the worker heartbeat")
        await asyncio.sleep(settings.heartbeat_interval_s)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def on_worker_startup(state: TaskiqState) -> None:
    core = build_core(settings, broker=broker)
    state.core = core
    state.jobs = core.jobs
    state.provider_poll = core.proactive.poll
    state.heartbeats = core.heartbeats
    state.heartbeat_task = asyncio.create_task(_beat_forever(core))


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def on_worker_shutdown(state: TaskiqState) -> None:
    state.heartbeat_task.cancel()
    await state.core.close()
