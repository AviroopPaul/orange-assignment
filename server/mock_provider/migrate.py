"""One-shot job for the mock provider: create its database if needed, upgrade, seed. Safe to repeat.

The database is created here as well as in the Postgres init script, because that script only runs
when the volume is first created.
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Database
from mock_provider.config import get_mock_settings
from mock_provider.seed import seed_demo_bookings

logger = logging.getLogger(__name__)


async def ensure_database(url: str) -> None:
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        return
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": parsed.database},
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        await admin.dispose()


def upgrade_schema() -> None:
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"), ini_section="mock")
    config.set_main_option("script_location", str(root / "mock_provider" / "migrations"))
    command.upgrade(config, "head")


async def seed(url: str) -> None:
    db = Database(url)
    async with db.session_scope() as session:
        await seed_demo_bookings(session, datetime.now(UTC))
    await db.dispose()


def main() -> None:
    url = get_mock_settings().mock_database_url
    asyncio.run(ensure_database(url))
    upgrade_schema()
    asyncio.run(seed(url))
    logger.info("mock provider migrations finished")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
