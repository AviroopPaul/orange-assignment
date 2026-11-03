# `docker compose` when the plugin works, otherwise the standalone binary.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

.PHONY: up down logs reset ps test test-web test-integration evals check smoke web-dev transcript

up:
	$(COMPOSE) up --build -d --wait
	@echo "App is ready at http://localhost:3000"
	@echo "API is at http://localhost:8000 (docs at /docs)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api worker scheduler

ps:
	$(COMPOSE) ps

reset:
	$(COMPOSE) down -v
	$(MAKE) up

test:
	cd server && uv run pytest

test-web:
	cd web && npm test

# Hot reload on port 5173, against the api from `make up`.
web-dev:
	cd web && npm run dev

test-integration:
	$(COMPOSE) --profile test build tests
	$(COMPOSE) --profile test run --rm tests

# Every scenario, offline, on its own throwaway PostgreSQL and Redis. Writes evals/results.*.
evals:
	mkdir -p evals
	chmod a+rwx evals
	$(COMPOSE) --profile evals rm -fsv postgres-eval redis-eval
	$(COMPOSE) --profile evals build evals
	$(COMPOSE) --profile evals run --rm evals; status=$$?; \
	$(COMPOSE) --profile evals rm -fsv postgres-eval redis-eval; exit $$status

check:
	cd server && ./scripts/check.sh
	cd web && npm run typecheck && npm test

smoke:
	cd server && uv run python scripts/smoke.py

# Append new Claude Code conversation entries to docs/transcript.md.
transcript:
	python3 scripts/append_transcript.py
