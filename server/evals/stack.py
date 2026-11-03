"""Starts the real stack for a run: mock, worker and scheduler processes, and the app in process.

The app runs here so that each scenario can plug in its own model. Everything else is a real
process, reached over real connections. The app and its workers reach PostgreSQL and Redis through
proxies the harness can switch off. The checker reads them directly, so it still works in an outage.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select, text

from app.container import ApiContainer, build_api_container, build_core
from app.core.config import Settings
from app.core.database import Base, Database
from app.domains.agent.scripted import ScriptedLlm
from app.domains.bookings.models import Approval, AttemptStatus, BookingAttempt
from app.domains.jobs.broker import build_broker
from app.domains.jobs.models import JobStatus, OutboxJob
from app.domains.memory.models import ContextLog, TurnView
from app.domains.notifications.models import Notification
from app.domains.proactive.models import BookingDisruption, DisruptionStatus
from app.domains.tracing.models import Run, TraceEvent
from app.main import create_app
from evals.invariants import Snapshot
from evals.models import Switchboard
from evals.proxy import TcpProxy
from mock_provider.models import BookAttempt, Booking, KeyedResult

logger = logging.getLogger(__name__)

# A Monday noon in India, far enough ahead that the clock only ever moves forward.
FAKE_START = datetime(2035, 1, 1, 6, 30, tzinfo=UTC)
MODEL_NAME = "gemini-3-flash-preview"
PG_PROXY_PORT = 15432
REDIS_PROXY_PORT = 16379
MOCK_PORT = 18100
API_PORT = 18000
JOB_LEASE_S = 6
# Tighter than production, so a recovery shows up in seconds. Set for every process.
SHORT_LIMITS = {
    "JOB_TIME_LIMIT_S": "5",
    "JOB_LEASE_S": str(JOB_LEASE_S),
    "TOOL_TIMEOUT_S": "3",
    "TOOL_RETRY_BACKOFF_S": "0.05",
    "BROKER_IDLE_TIMEOUT_S": "30",
    "BROKER_QUEUE_NAME": "errand.jobs.eval",
    "SWEEP_INTERVAL_S": "2",
    "PROVIDER_POLL_INTERVAL_S": "2",
    "HEARTBEAT_INTERVAL_S": "2",
    "RUN_DEADLINE_S": "15",
    "TURN_LOCK_TTL_S": "20",
    "TURN_LOCK_WAIT_S": "25",
    "MODEL_RATE_LIMIT_MAX_WAIT_S": "1",
    "MODEL_MODE": "scripted",
    "GOOGLE_API_KEY": "",
}


def _pg_url(host: str, port: int, name: str) -> str:
    return f"postgresql+asyncpg://errand:errand@{host}:{port}/{name}"


class Stack:
    def __init__(self, results_dir: Path) -> None:
        self.pg_host = os.environ.get("EVAL_POSTGRES_HOST", "postgres-eval")
        self.redis_host = os.environ.get("EVAL_REDIS_HOST", "redis-eval")
        self.results_dir = results_dir
        self.logs_dir = results_dir / "logs"
        self.trace_dir = results_dir / "traces"
        self.offset_s = (FAKE_START - datetime.now(UTC)).total_seconds()
        self.pg_proxy = TcpProxy(PG_PROXY_PORT, self.pg_host, 5432)
        self.redis_proxy = TcpProxy(REDIS_PROXY_PORT, self.redis_host, 6379)
        self.env = self._app_env()
        self.db = Database(_pg_url(self.pg_host, 5432, "errand_eval"))
        self.adk_db = Database(_pg_url(self.pg_host, 5432, "adk_sessions_eval"))
        self.mock_db = Database(_pg_url(self.pg_host, 5432, "mock_provider_eval"))
        self.redis = Redis.from_url(f"redis://{self.redis_host}:6379/0")
        self.switchboard = Switchboard(model=MODEL_NAME, active=ScriptedLlm(model=MODEL_NAME))
        self.mock_process: subprocess.Popen | None = None
        self.worker: subprocess.Popen | None = None
        self.worker_is_default = False
        self.scheduler: subprocess.Popen | None = None
        self.api_process: subprocess.Popen | None = None
        self.settings: Settings
        self.container: ApiContainer
        self.http: httpx.AsyncClient
        self.provider: httpx.AsyncClient

    def _app_env(self) -> dict[str, str]:
        return {
            **os.environ,
            **SHORT_LIMITS,
            "ERRAND_DATABASE_URL": _pg_url("127.0.0.1", PG_PROXY_PORT, "errand_eval"),
            "ADK_DATABASE_URL": _pg_url("127.0.0.1", PG_PROXY_PORT, "adk_sessions_eval"),
            "REDIS_URL": f"redis://127.0.0.1:{REDIS_PROXY_PORT}/0",
            "MOCK_PROVIDER_URL": f"http://127.0.0.1:{MOCK_PORT}",
            "CLOCK_OFFSET_S": str(self.offset_s),
            "TRACE_DIR": str(self.trace_dir),
            "PROCESS_NAME": "api",
        }

    def _spawn(self, name: str, command: list[str], env: dict[str, str]) -> subprocess.Popen:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = (self.logs_dir / f"{name}.log").open("ab")
        return subprocess.Popen(command, env=env, start_new_session=True, stdout=log, stderr=log)

    async def start(self) -> None:
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        for old in self.trace_dir.glob("*.jsonl"):
            old.unlink()
        await self.pg_proxy.start()
        await self.redis_proxy.start()
        await self._migrate()
        await self._start_mock()

        os.environ.update(self.env)
        self.settings = Settings()
        broker = build_broker(self.settings)
        await broker.startup()
        core = build_core(self.settings, broker=broker)
        self.container = build_api_container(core, broker=broker, model=self.switchboard)
        app = create_app(self._container_factory)
        app.state.container = self.container
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.http = httpx.AsyncClient(transport=transport, base_url="http://api", timeout=60)
        self.provider = httpx.AsyncClient(base_url=f"http://127.0.0.1:{MOCK_PORT}", timeout=20)

        await self.start_worker()
        self.scheduler = self._spawn(
            "scheduler",
            [sys.executable, "-m", "taskiq", "scheduler", "app.scheduler:scheduler"],
            {**self.env, "PROCESS_NAME": "scheduler"},
        )

    async def _container_factory(self) -> ApiContainer:
        return self.container

    async def _migrate(self) -> None:
        env = {
            **self.env,
            "ERRAND_DATABASE_URL": _pg_url(self.pg_host, 5432, "errand_eval"),
            "ADK_DATABASE_URL": _pg_url(self.pg_host, 5432, "adk_sessions_eval"),
            "MOCK_DATABASE_URL": _pg_url(self.pg_host, 5432, "mock_provider_eval"),
        }
        for module in ("app.migrate", "mock_provider.migrate"):
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", module, env=env)
            if await process.wait() != 0:
                raise RuntimeError(f"{module} failed")

    async def _start_mock(self) -> None:
        self.mock_process = self._spawn(
            "mock",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mock_provider.main:app",
                "--port",
                str(MOCK_PORT),
                "--log-level",
                "warning",
            ],
            {
                **self.env,
                "MOCK_DATABASE_URL": _pg_url(self.pg_host, 5432, "mock_provider_eval"),
                "MOCK_EVAL_MODE": "true",
                "MOCK_CLOCK_OFFSET_S": str(self.offset_s),
                "MOCK_TIMEOUT_HANG_S": "8",
            },
        )
        async with httpx.AsyncClient(timeout=1) as client:
            for _ in range(200):
                try:
                    response = await client.get(f"http://127.0.0.1:{MOCK_PORT}/healthz")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        raise RuntimeError("the mock provider did not start")

    async def start_worker(self, **overrides: str) -> None:
        """A new worker process. It is ready once its first heartbeat is in Redis."""
        await self.kill_worker()
        async for key in self.redis.scan_iter(match="heartbeat:worker:*"):
            await self.redis.delete(key)
        self.worker = self._spawn(
            "worker",
            [sys.executable, "-m", "taskiq", "worker", "app.worker:broker", "--workers", "1"],
            {**self.env, "PROCESS_NAME": "worker", **overrides},
        )
        self.worker_is_default = not overrides
        for _ in range(300):
            async for _key in self.redis.scan_iter(match="heartbeat:worker:*"):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("the worker did not start")

    async def kill_worker(self) -> None:
        self._kill(self.worker)
        self.worker = None

    def _kill(self, process: subprocess.Popen | None) -> None:
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    async def stop(self) -> None:
        for process in (self.worker, self.scheduler, self.mock_process, self.api_process):
            self._kill(process)
        await self.http.aclose()
        await self.provider.aclose()
        await self.container.close()
        await self.pg_proxy.stop()
        await self.redis_proxy.stop()
        for db in (self.db, self.adk_db, self.mock_db):
            await db.dispose()
        await self.redis.aclose()

    async def reset(self) -> None:
        """Each scenario starts clean: outages over, default worker up, no rows, a fresh mock."""
        self.pg_proxy.up()
        self.redis_proxy.up()
        if self.worker is None or self.worker.poll() is not None or not self.worker_is_default:
            await self.start_worker()
        if self.scheduler is None or self.scheduler.poll() is not None:
            raise RuntimeError("the scheduler process died")
        self._kill(self.api_process)
        self.api_process = None
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        async with self.db.session_scope() as session:
            await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        assert (await self.provider.post("/admin/reset")).status_code == 204
        self.switchboard.active = ScriptedLlm(model=MODEL_NAME)

    async def open_work(self) -> int:
        """How many things are still under way. Read directly, so an outage cannot hide them."""
        async with self.db.session_scope() as session:
            jobs = await session.scalars(
                select(OutboxJob).where(
                    OutboxJob.status.not_in([JobStatus.COMPLETED.value, JobStatus.FAILED.value])
                )
            )
            attempts = await session.scalars(
                select(BookingAttempt).where(BookingAttempt.status == AttemptStatus.OPEN.value)
            )
            disruptions = await session.scalars(
                select(BookingDisruption).where(
                    BookingDisruption.status == DisruptionStatus.DETECTED.value
                )
            )
            return len(jobs.all()) + len(attempts.all()) + len(disruptions.all())

    async def settle(self, limit_s: float = 45) -> bool:
        deadline = asyncio.get_running_loop().time() + limit_s
        while asyncio.get_running_loop().time() < deadline:
            if await self.open_work() == 0:
                return True
            await asyncio.sleep(0.2)
        return False

    async def snapshot(self, forbidden: tuple[str, ...] = ()) -> Snapshot:
        async def rows(session: Any, model: Any) -> list:
            return list(await session.scalars(select(model)))

        async with self.db.session_scope() as session:
            errand = {
                "approvals": await rows(session, Approval),
                "attempts": await rows(session, BookingAttempt),
                "outbox": await rows(session, OutboxJob),
                "runs": await rows(session, Run),
                "events": await rows(session, TraceEvent),
                "notifications": await rows(session, Notification),
                "contexts": await rows(session, ContextLog),
                "turns": await rows(session, TurnView),
                "disruptions": await rows(session, BookingDisruption),
            }
        async with self.mock_db.session_scope() as session:
            provider = {
                "bookings": await rows(session, Booking),
                "book_attempts": await rows(session, BookAttempt),
                "keyed_results": await rows(session, KeyedResult),
            }
        return Snapshot(
            **errand,
            **provider,
            trace_dir=self.trace_dir,
            budget=self.settings.context_budget_tokens,
            forbidden=forbidden,
        )
