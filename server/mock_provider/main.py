"""The mock-provider process: one route per contract tool, plus /admin in eval mode only."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.core.database import Database
from mock_provider.config import MockSettings, get_mock_settings
from mock_provider.schemas import (
    AdvanceClock,
    BookingRead,
    BookingStatusRead,
    BookInput,
    CancelInput,
    GetBookingStatusInput,
    GetProviderInput,
    HoldRead,
    HoldSlotInput,
    ListBookingsInput,
    ListBookingsOutput,
    ProviderCancel,
    ProviderDetails,
    RescheduleInput,
    ScenarioConfig,
    SearchProvidersInput,
    SearchProvidersOutput,
)
from mock_provider.service import MockProviderService, ToolError

tools = APIRouter(prefix="/tools", tags=["tools"])
admin = APIRouter(prefix="/admin", tags=["admin"])


def get_service(request: Request) -> MockProviderService:
    return request.app.state.service


# The caller's identity travels like an auth header. It is not a tool input, so the contract's
# inputs stay exactly as the brief fixes them.
async def require_user(x_user_id: str = Header(min_length=1, max_length=64)) -> str:
    return x_user_id


@tools.post("/search_providers", response_model=SearchProvidersOutput)
async def search_providers(
    body: SearchProvidersInput, service: MockProviderService = Depends(get_service)
) -> SearchProvidersOutput:
    return SearchProvidersOutput(providers=await service.search_providers(body))


@tools.post("/get_provider", response_model=ProviderDetails)
async def get_provider(
    body: GetProviderInput, service: MockProviderService = Depends(get_service)
) -> ProviderDetails:
    return await service.get_provider(body.provider_id)


@tools.post("/hold_slot", response_model=HoldRead)
async def hold_slot(
    body: HoldSlotInput,
    user_id: str = Depends(require_user),
    service: MockProviderService = Depends(get_service),
) -> HoldRead:
    return await service.hold_slot(user_id, body.provider_id, body.slot)


@tools.post("/book", response_model=BookingRead)
async def book(
    body: BookInput,
    user_id: str = Depends(require_user),
    service: MockProviderService = Depends(get_service),
) -> BookingRead:
    return await service.book(user_id, body.hold_id, body.idempotency_key)


@tools.post("/get_booking_status", response_model=BookingStatusRead)
async def get_booking_status(
    body: GetBookingStatusInput,
    user_id: str = Depends(require_user),
    service: MockProviderService = Depends(get_service),
) -> BookingStatusRead:
    return await service.get_booking_status(user_id, body.hold_id, body.idempotency_key)


@tools.post("/cancel", response_model=BookingRead)
async def cancel(
    body: CancelInput,
    user_id: str = Depends(require_user),
    service: MockProviderService = Depends(get_service),
) -> BookingRead:
    return await service.cancel(user_id, body.booking_id, body.idempotency_key)


@tools.post("/reschedule", response_model=BookingRead)
async def reschedule(
    body: RescheduleInput,
    user_id: str = Depends(require_user),
    service: MockProviderService = Depends(get_service),
) -> BookingRead:
    return await service.reschedule(user_id, body.booking_id, body.slot, body.idempotency_key)


@tools.post("/list_bookings", response_model=ListBookingsOutput)
async def list_bookings(
    body: ListBookingsInput,
    user_id: str = Depends(require_user),
    service: MockProviderService = Depends(get_service),
) -> ListBookingsOutput:
    if body.user_id != user_id:
        raise ToolError("forbidden", 403, "You can only list your own bookings.")
    return ListBookingsOutput(bookings=await service.list_bookings(body.user_id))


@admin.post("/scenario", status_code=204)
async def set_scenario(
    body: ScenarioConfig, service: MockProviderService = Depends(get_service)
) -> None:
    await service.configure_scenario(body)


@admin.post("/clock/advance")
async def advance_clock(
    body: AdvanceClock, service: MockProviderService = Depends(get_service)
) -> dict[str, str]:
    return {"now": (await service.advance_clock(body.seconds)).isoformat()}


@admin.post("/provider-cancel", response_model=BookingRead)
async def provider_cancel(
    body: ProviderCancel, service: MockProviderService = Depends(get_service)
) -> BookingRead:
    return await service.provider_cancel(body.booking_id)


@admin.post("/reset", status_code=204)
async def reset(service: MockProviderService = Depends(get_service)) -> None:
    await service.reset()


async def _tool_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ToolError)
    return JSONResponse(
        status_code=error.status_code, content={"error": error.code, "message": error.message}
    )


def create_app(
    settings: MockSettings | None = None,
    db: Database | None = None,
    real_now: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or get_mock_settings()
    database = db or Database(settings.mock_database_url, pool_size=5, pool_pre_ping=True)
    service = MockProviderService(
        database, settings.mock_default_seed, settings.mock_timeout_hang_s, real_now
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await database.dispose()

    app = FastAPI(title="Mock Provider", lifespan=lifespan)
    app.state.service = service
    app.state.db = database
    app.include_router(tools)
    if settings.mock_eval_mode:
        app.include_router(admin)
    app.add_exception_handler(ToolError, _tool_error)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        await database.ping()
        return {"status": "ok"}

    return app


app = create_app()
