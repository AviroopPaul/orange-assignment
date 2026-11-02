"""taskiq entrypoints. Decode, call the service, nothing else."""

import uuid

from taskiq import AsyncBroker, Context, TaskiqDepends

from app.core.heartbeats import SCHEDULER_HEARTBEAT
from app.domains.jobs.broker import RUN_JOB_TASK, SWEEP_TASK


def register_tasks(broker: AsyncBroker, sweep_interval_s: int) -> None:
    @broker.task(task_name=RUN_JOB_TASK)
    async def run_job(job_id: str, context: Context = TaskiqDepends()) -> None:
        await context.state.jobs.execute(uuid.UUID(job_id))

    @broker.task(task_name=SWEEP_TASK, schedule=[{"interval": sweep_interval_s}])
    async def sweep(context: Context = TaskiqDepends()) -> None:
        # The beat is written here, so it proves scheduler, Redis and worker all work together.
        await context.state.heartbeats.beat(SCHEDULER_HEARTBEAT)
        await context.state.jobs.sweep()
