"""The limits only protect us if they agree with each other."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_blueprint_defaults_are_valid() -> None:
    settings = Settings(_env_file=None)
    assert settings.max_model_calls == 8
    assert settings.run_deadline_s == 120
    assert settings.model_request_timeout_s == 30
    assert settings.job_time_limit_s == 45
    assert settings.broker_idle_timeout_s == 60
    assert settings.sweep_interval_s == 15


def test_turn_lock_must_outlive_the_run() -> None:
    with pytest.raises(ValidationError, match="turn_lock_ttl_s"):
        Settings(turn_lock_ttl_s=100, run_deadline_s=120)


def test_lease_must_cover_the_job_limit() -> None:
    with pytest.raises(ValidationError, match="job_lease_s"):
        Settings(job_lease_s=30, job_time_limit_s=45)


def test_broker_must_not_redeliver_running_jobs() -> None:
    with pytest.raises(ValidationError, match="broker_idle_timeout_s"):
        Settings(broker_idle_timeout_s=45, job_time_limit_s=45, job_lease_s=60)


def test_request_timeout_respects_geminis_minimum() -> None:
    """Gemini answers 400 to any deadline under 10 s, which would fail every chat turn."""
    with pytest.raises(ValidationError, match="model_request_timeout_s"):
        Settings(model_request_timeout_s=5)


def test_blank_key_counts_as_no_key() -> None:
    settings = Settings(google_api_key="", model_mode="live")
    assert settings.google_api_key is None
    assert not settings.model_is_configured


def test_scripted_mode_needs_no_key() -> None:
    assert Settings(google_api_key="", model_mode="scripted").model_is_configured


def test_key_is_hidden_when_printed() -> None:
    settings = Settings(google_api_key="super-secret-value")
    assert "super-secret-value" not in repr(settings)
    assert settings.model_is_configured
