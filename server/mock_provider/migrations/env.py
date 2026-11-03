"""Alembic for the mock provider's own database, separate from the errand schema."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import UtcDateTime
from mock_provider.config import get_mock_settings
from mock_provider.models import MockBase

target_metadata = MockBase.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Migrations must not import app code, so render our column type as what it stores."""
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(get_mock_settings().mock_database_url)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
        await connection.commit()
    await engine.dispose()


asyncio.run(run_online())
