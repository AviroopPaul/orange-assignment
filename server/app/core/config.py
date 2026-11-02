"""Process configuration. Every limit from the blueprint lives here."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", protected_namespaces=())

    errand_database_url: str = "postgresql+asyncpg://errand:errand@localhost:5432/errand"
    adk_database_url: str = "postgresql+asyncpg://errand:errand@localhost:5432/adk_sessions"
    redis_url: str = "redis://localhost:6379/0"

    google_api_key: SecretStr | None = None
    model_mode: Literal["live", "scripted"] = "live"
    model_name: str = "gemini-3.5-flash-lite"
    model_request_timeout_s: float = 30
    model_attempts: int = 2

    max_model_calls: int = 8
    run_deadline_s: float = 120
    turn_lock_ttl_s: float = 150
    turn_lock_wait_s: float = 125

    job_time_limit_s: float = 45
    job_lease_s: float = 60
    job_max_attempts: int = 5
    broker_idle_timeout_s: float = 60
    broker_queue_name: str = "errand.jobs"
    sweep_interval_s: int = 15
    heartbeat_interval_s: float = 10
    heartbeat_ttl_s: int = 45

    @field_validator("google_api_key", mode="before")
    @classmethod
    def _blank_key_is_no_key(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _limits_must_agree(self) -> Self:
        if self.model_request_timeout_s < 10:
            raise ValueError("model_request_timeout_s must be at least 10: Gemini rejects less")
        if self.turn_lock_ttl_s <= self.run_deadline_s:
            raise ValueError("turn_lock_ttl_s must exceed run_deadline_s, or two turns can collide")
        if self.job_lease_s < self.job_time_limit_s:
            raise ValueError("job_lease_s must cover job_time_limit_s, or a live job can be taken")
        if self.broker_idle_timeout_s <= self.job_time_limit_s:
            raise ValueError(
                "broker_idle_timeout_s must exceed job_time_limit_s, "
                "or the broker redelivers jobs that are still running"
            )
        if self.heartbeat_ttl_s <= self.sweep_interval_s:
            raise ValueError("heartbeat_ttl_s must exceed sweep_interval_s")
        return self

    @property
    def model_is_configured(self) -> bool:
        return self.model_mode == "scripted" or self.google_api_key is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
