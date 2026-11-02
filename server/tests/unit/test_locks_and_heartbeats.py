import asyncio

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.heartbeats import Heartbeats
from app.core.locks import RedisTurnLock, TurnLockTimeout, TurnLockUnavailable


async def test_lock_serialises_turns_on_one_session(redis: Redis) -> None:
    lock = RedisTurnLock(redis, ttl_s=5, wait_s=2)
    order: list[str] = []

    async def turn(name: str) -> None:
        async with lock.hold("session-1"):
            order.append(f"{name}-start")
            await asyncio.sleep(0.02)
            order.append(f"{name}-end")

    await asyncio.gather(turn("a"), turn("b"))
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


async def test_other_sessions_do_not_wait(redis: Redis) -> None:
    lock = RedisTurnLock(redis, ttl_s=5, wait_s=0.05)
    async with lock.hold("session-1"), lock.hold("session-2"):
        pass


async def test_second_turn_gives_up_after_the_wait(redis: Redis) -> None:
    lock = RedisTurnLock(redis, ttl_s=5, wait_s=0.05)
    async with lock.hold("session-1"):
        with pytest.raises(TurnLockTimeout):
            async with lock.hold("session-1"):
                pass


async def test_lock_is_released_after_an_error(redis: Redis) -> None:
    lock = RedisTurnLock(redis, ttl_s=5, wait_s=0.05)
    with pytest.raises(ValueError):
        async with lock.hold("session-1"):
            raise ValueError("turn failed")
    async with lock.hold("session-1"):
        pass


async def test_expired_lock_does_not_break_the_turn_or_free_a_new_owner(redis: Redis) -> None:
    lock = RedisTurnLock(redis, ttl_s=0.02, wait_s=0.5)
    async with lock.hold("session-1"):
        await asyncio.sleep(0.05)
        async with RedisTurnLock(redis, ttl_s=5, wait_s=0.05).hold("session-1"):
            pass


async def test_redis_down_fails_closed() -> None:
    class DeadRedis(Redis):
        async def execute_command(self, *args: object, **kwargs: object) -> object:
            raise RedisConnectionError("redis is down")

        def register_script(self, script: object) -> object:
            return None

    lock = RedisTurnLock(DeadRedis(), ttl_s=5, wait_s=0.05)
    entered = False
    with pytest.raises(TurnLockUnavailable):
        async with lock.hold("session-1"):
            entered = True
    assert not entered


async def test_heartbeat_expires(redis: Redis) -> None:
    heartbeats = Heartbeats(redis, ttl_s=1)
    assert not await heartbeats.is_alive("scheduler")
    await heartbeats.beat("scheduler")
    assert await heartbeats.is_alive("scheduler")
    assert await redis.ttl("heartbeat:scheduler") == 1


async def test_any_worker_counts_as_alive(redis: Redis) -> None:
    heartbeats = Heartbeats(redis, ttl_s=30)
    assert not await heartbeats.any_alive("worker:")
    await heartbeats.beat("worker:host-b")
    assert await heartbeats.any_alive("worker:")
