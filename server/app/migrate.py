"""One-shot startup job: upgrade our schema, then have ADK create its own tables once.

ADK creates tables on first use. Doing it here stops several api copies racing at startup.
Safe to run again.
"""

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from google.adk.sessions import DatabaseSessionService

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import Database
from app.domains.memory.seed import seed_memories

logger = logging.getLogger(__name__)


def upgrade_errand_schema() -> None:
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")


async def prepare_adk_tables() -> None:
    service = DatabaseSessionService(db_url=get_settings().adk_database_url)
    await service.get_session(app_name="bootstrap", user_id="bootstrap", session_id="bootstrap")
    await service.db_engine.dispose()


async def seed() -> None:
    db = Database(get_settings().errand_database_url)
    await seed_memories(db, SystemClock())
    await db.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    upgrade_errand_schema()
    asyncio.run(prepare_adk_tables())
    asyncio.run(seed())
    logger.info("migrations finished")
