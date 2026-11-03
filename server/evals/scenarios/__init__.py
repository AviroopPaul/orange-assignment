"""Importing this registers every scenario, in table order."""

from evals.scenario import REGISTRY
from evals.scenarios import (  # noqa: F401
    adversarial,
    booking_failures,
    changes,
    core,
    infrastructure,
    limits,
    memory,
    proactive,
    restarts,
)

REGISTRY.sort(key=lambda item: item.id)
