"""The api process."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.container import ApiContainer, build_api_container, build_core
from app.core.config import get_settings
from app.domains.bookings.router import router as bookings_router
from app.domains.chat.router import router as chat_router
from app.domains.jobs.broker import build_broker
from app.domains.memory.router import router as memory_router
from app.domains.notifications.router import router as notifications_router
from app.domains.system.router import router as system_router
from app.domains.tracing.router import router as tracing_router

logger = logging.getLogger(__name__)

ContainerFactory = Callable[[], Awaitable[ApiContainer]]


async def _build_live_container() -> ApiContainer:
    settings = get_settings()
    broker = build_broker(settings)
    await broker.startup()
    core = build_core(settings, broker=broker)
    return build_api_container(core, broker=broker)


async def _database_unavailable(request: Request, error: Exception) -> JSONResponse:
    logger.error("database unavailable on %s: %s", request.url.path, error)
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "message": "The database is not reachable. Nothing was changed. Please try again.",
        },
    )


def create_app(container_factory: ContainerFactory = _build_live_container) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = await container_factory()
        yield
        await app.state.container.close()

    app = FastAPI(title="Errand Agent", lifespan=lifespan)
    app.include_router(system_router)
    app.include_router(chat_router)
    app.include_router(bookings_router)
    app.include_router(notifications_router)
    app.include_router(memory_router)
    app.include_router(tracing_router)
    app.add_exception_handler(SQLAlchemyError, _database_unavailable)
    app.add_exception_handler(OSError, _database_unavailable)
    return app


logging.basicConfig(level=logging.INFO)
# Every status poll calls the provider, which would otherwise bury the useful log lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
