#!/usr/bin/env bash
# Run once, at the end of a change.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports
uv run pytest
