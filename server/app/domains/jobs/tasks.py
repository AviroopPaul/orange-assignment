"""taskiq entrypoints. Decode, call the service, nothing else."""

import uuid

from taskiq import AsyncBroker, Context, TaskiqDepends

from app.core.heartbeats import SCHEDULER_HEARTBEAT
from app.domains.jobs.broker import PROVIDER_POLL_TASK, RUN_JOB_TASK, SWEEP_TASK


def register_tasks(
    broker: AsyncBroker, sweep_interval_s: int, provider_poll_interval_s: int = 20
) -> None:
    @broker.task(task_name=RUN_JOB_TASK)
    async def run_job(job_id: str, context: Context = TaskiqDepends()) -> None:
        await context.state.jobs.execute(uuid.UUID(job_id))

    @broker.task(task_name=SWEEP_TASK, schedule=[{"interval": sweep_interval_s}])
    async def sweep(context: Context = TaskiqDepends()) -> None:
        # The beat is written here, so it proves scheduler, Redis and worker all work together.
        await context.state.heartbeats.beat(SCHEDULER_HEARTBEAT)
        await context.state.jobs.sweep()

    @broker.task(task_name=PROVIDER_POLL_TASK, schedule=[{"interval": provider_poll_interval_s}])
    async def provider_poll(context: Context = TaskiqDepends()) -> None:
        # A lost poll needs no recovery. The next one reads the same answer from the provider.
        await context.state.provider_poll()
