"""Settings for the mock provider. It stands in for an outside company, so it has its own."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    mock_database_url: str = "postgresql+asyncpg://errand:errand@localhost:5432/mock_provider"
    # Turns on /admin: scenarios, clock and simulated provider cancellations.
    mock_eval_mode: bool = False
    mock_default_seed: int = 42
    # How long `book` holds the connection when it simulates a timeout. Longer than a client waits.
    mock_timeout_hang_s: float = 15
    # Evals only: every eval process starts from the same fake day. Normally 0.
    mock_clock_offset_s: float = 0


@lru_cache
def get_mock_settings() -> MockSettings:
    return MockSettings()
