"""One JSONL file per run. api and worker append to the same file, so one booking is one trace.

A file write failure must never change a user's outcome. PostgreSQL still has the trace.
"""

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.core.redaction import scrub_value

logger = logging.getLogger(__name__)


class JsonlTraceFiles:
    def __init__(self, directory: str | Path, process: str) -> None:
        self._directory = Path(directory)
        self._process = process

    def path_for(self, run_id: uuid.UUID) -> Path:
        return self._directory / f"{run_id}.jsonl"

    def append(self, run_id: uuid.UUID, line: dict[str, Any]) -> None:
        record = {"run_id": str(run_id), "process": self._process, **line}
        data = (json.dumps(record, default=str, ensure_ascii=False) + "\n").encode()
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            # One write on an append-only descriptor, so two processes cannot mix their lines.
            descriptor = os.open(self.path_for(run_id), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                os.write(descriptor, data)
            finally:
                os.close(descriptor)
        except OSError:
            logger.warning("could not write the trace file for run %s", run_id, exc_info=True)

    def scrub(self, run_id: uuid.UUID, pattern: re.Pattern[str]) -> None:
        """A forgotten fact must not stay readable here either."""
        path = self.path_for(run_id)
        temporary = path.with_suffix(".scrub")
        try:
            if not path.exists():
                return
            lines = [
                json.dumps(scrub_value(json.loads(line), pattern), ensure_ascii=False)
                for line in path.read_text().splitlines()
            ]
            temporary.write_text("".join(f"{line}\n" for line in lines))
            os.replace(temporary, path)
        except (OSError, ValueError):
            logger.exception("could not scrub the trace file for run %s", run_id)
