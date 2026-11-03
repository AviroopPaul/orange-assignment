"""The real api as its own process, for E37. Its model holds the turn open so it can be killed."""

import os

from fastapi import FastAPI

from app.container import ApiContainer, build_api_container, build_core
from app.core.config import Settings
from app.domains.agent.scripted import Pause, ScriptedLlm
from app.domains.jobs.broker import build_broker
from app.main import create_app


async def _container() -> ApiContainer:
    settings = Settings()
    broker = build_broker(settings)
    await broker.startup()
    core = build_core(settings, broker=broker)
    pause = Pause(float(os.environ["EVAL_PAUSE_S"]), "too late")
    return build_api_container(
        core, broker=broker, model=ScriptedLlm(model="gemini-3-flash-preview", script=[pause])
    )


def create() -> FastAPI:
    return create_app(_container)
