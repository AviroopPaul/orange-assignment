"""The composition root. ADK is imported only when the api process needs it."""

from dataclasses import dataclass
from typing import Any

import httpx
from redis.asyncio import Redis
from taskiq import AsyncBroker

from app.core.clock import LOCAL_TZ, Clock, SystemClock
from app.core.config import Settings
from app.core.database import Database
from app.core.heartbeats import Heartbeats
from app.core.locks import RedisTurnLock
from app.domains.chat.service import ChatService
from app.domains.gateway.client import ProviderClient
from app.domains.gateway.service import ApprovalVerifier, DenyAllApprovals, ToolGateway
from app.domains.jobs.broker import TaskiqJobPublisher
from app.domains.jobs.service import JobHandler, JobPublisher, JobService
from app.domains.system.service import PING_JOB, SystemService, handle_ping
from app.domains.tracing.service import TraceService
from app.domains.workflow.service import WorkflowService

JOB_HANDLERS: dict[str, JobHandler] = {PING_JOB: handle_ping}


@dataclass
class Core:
    """What both api and worker need."""

    settings: Settings
    db: Database
    redis: Redis
    clock: Clock
    heartbeats: Heartbeats
    jobs: JobService
    system: SystemService
    gateway: ToolGateway
    provider_client: ProviderClient

    async def close(self) -> None:
        await self.provider_client.close()
        await self.redis.aclose()
        await self.db.dispose()


@dataclass
class ApiContainer:
    core: Core
    chat: ChatService
    traces: TraceService
    broker: AsyncBroker | None = None

    async def close(self) -> None:
        if self.broker is not None:
            await self.broker.shutdown()
        await self.core.close()


def build_core(
    settings: Settings,
    publisher: JobPublisher | None = None,
    broker: Any | None = None,
    db: Database | None = None,
    redis: Redis | None = None,
    clock: Clock | None = None,
    handlers: dict[str, JobHandler] | None = None,
    provider_transport: httpx.AsyncBaseTransport | None = None,
    approvals: ApprovalVerifier | None = None,
) -> Core:
    db = db or Database(settings.errand_database_url, pool_size=5, pool_pre_ping=True)
    redis = redis or Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=10)
    clock = clock or SystemClock()
    heartbeats = Heartbeats(redis, settings.heartbeat_ttl_s)
    if publisher is None:
        if broker is None:
            raise ValueError("build_core needs a publisher or a broker")
        publisher = TaskiqJobPublisher(broker)
    jobs = JobService(
        db=db,
        publisher=publisher,
        handlers=JOB_HANDLERS if handlers is None else handlers,
        clock=clock,
        time_limit_s=settings.job_time_limit_s,
        lease_s=settings.job_lease_s,
        max_attempts=settings.job_max_attempts,
    )
    provider_client = ProviderClient(
        settings.mock_provider_url, settings.tool_timeout_s, transport=provider_transport
    )
    gateway = ToolGateway(
        provider_client,
        # Until the approvals domain exists, nothing that needs approval can run.
        approvals or DenyAllApprovals(),
        settings.tool_retry_backoff_s,
    )
    system = SystemService(
        db, redis, heartbeats, jobs, gateway.is_provider_reachable, settings.model_is_configured
    )
    return Core(settings, db, redis, clock, heartbeats, jobs, system, gateway, provider_client)


def build_api_container(
    core: Core,
    broker: AsyncBroker | None = None,
    model: Any | None = None,
    session_service: Any | None = None,
) -> ApiContainer:
    from google.adk.sessions import DatabaseSessionService

    from app.domains.agent.llm import build_model, build_request_config
    from app.domains.agent.runtime import AdkAgentRuntime

    settings = core.settings
    traces = TraceService(core.db, core.clock)
    workflows = WorkflowService(core.db, core.clock)
    runtime = AdkAgentRuntime(
        model=model or build_model(settings),
        session_service=session_service or DatabaseSessionService(db_url=settings.adk_database_url),
        gateway=core.gateway,
        workflow=workflows,
        max_model_calls=settings.max_model_calls,
        max_tool_calls=settings.max_tool_calls,
        today=lambda: core.clock.now().astimezone(LOCAL_TZ).date(),
        request_config=build_request_config(settings),
    )
    chat = ChatService(
        runtime=runtime,
        turn_lock=RedisTurnLock(
            core.redis, ttl_s=settings.turn_lock_ttl_s, wait_s=settings.turn_lock_wait_s
        ),
        traces=traces,
        workflows=workflows,
        run_deadline_s=settings.run_deadline_s,
        model_is_configured=settings.model_is_configured,
    )
    return ApiContainer(core, chat, traces, broker)
