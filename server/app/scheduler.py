"""The scheduler process: `taskiq scheduler app.scheduler:scheduler`. Run exactly one copy."""

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.worker import broker

scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])
