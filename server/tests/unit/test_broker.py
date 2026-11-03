import uuid
from collections.abc import AsyncGenerator
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from taskiq_redis import RedisStreamBroker

from app.core.config import Settings
from app.domains.jobs.broker import (
    PROVIDER_POLL_TASK,
    RUN_JOB_TASK,
    SWEEP_TASK,
    ResilientStreamBroker,
    TaskiqJobPublisher,
    build_broker,
)
from app.domains.jobs.tasks import register_tasks


def test_broker_uses_the_safe_settings() -> None:
    broker = build_broker(Settings(broker_idle_timeout_s=60, broker_queue_name="q"))
    assert broker.idle_timeout == 60_000
    assert broker.queue_name == "q"
    assert broker.consumer_id == "0"
    assert broker.unacknowledged_lock_timeout == 5.0


def test_sweep_is_scheduled_at_the_configured_interval() -> None:
    broker = build_broker(Settings())
    register_tasks(broker, sweep_interval_s=15)
    tasks = broker.get_all_tasks()
    assert set(tasks) == {RUN_JOB_TASK, SWEEP_TASK, PROVIDER_POLL_TASK}
    assert tasks[SWEEP_TASK].labels["schedule"] == [{"interval": 15}]


async def test_listen_survives_a_redis_wipe_and_a_redis_outage() -> None:
    """A wipe removes the consumer group, which raises NOGROUP out of the base broker."""
    broker = build_broker(Settings())
    broker.retry_delay_s = 0
    steps = iter(
        [
            ResponseError("NOGROUP No such consumer group"),
            RedisConnectionError("redis is down"),
            "message",
        ]
    )
    declared = 0

    async def fake_listen(self: RedisStreamBroker) -> AsyncGenerator[str, None]:
        step = next(steps)
        if isinstance(step, Exception):
            raise step
        yield step

    async def fake_declare(self: RedisStreamBroker) -> None:
        nonlocal declared
        declared += 1

    with (
        patch.object(RedisStreamBroker, "listen", fake_listen),
        patch.object(RedisStreamBroker, "_declare_consumer_group", fake_declare),
    ):
        stream = broker.listen()
        assert await anext(stream) == "message"
        await stream.aclose()

    assert declared == 1


async def test_listen_raises_other_redis_errors() -> None:
    broker = build_broker(Settings())

    async def fake_listen(self: RedisStreamBroker) -> AsyncGenerator[str, None]:
        raise ResponseError("WRONGTYPE not a stream")
        yield

    with patch.object(RedisStreamBroker, "listen", fake_listen):
        stream = broker.listen()
        try:
            await anext(stream)
        except ResponseError as error:
            assert "WRONGTYPE" in str(error)
        else:
            raise AssertionError("expected the error to surface")


async def test_publisher_sends_the_job_id_to_the_run_task() -> None:
    broker = build_broker(Settings())
    sent = []

    async def fake_kick(self: ResilientStreamBroker, message: object) -> None:
        sent.append(message)

    job_id = uuid.uuid4()
    with patch.object(ResilientStreamBroker, "kick", fake_kick):
        await TaskiqJobPublisher(broker).publish(job_id)

    assert len(sent) == 1
    assert sent[0].task_name == RUN_JOB_TASK
    assert str(job_id).encode() in sent[0].message
