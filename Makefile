# `docker compose` when the plugin works, otherwise the standalone binary.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

.PHONY: up down logs reset ps test test-integration check smoke

up:
	$(COMPOSE) up --build -d --wait
	@echo "API is ready at http://localhost:8000 (docs at /docs)"

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

test-integration:
	$(COMPOSE) --profile test build tests
	$(COMPOSE) --profile test run --rm tests

check:
	cd server && ./scripts/check.sh

smoke:
	cd server && uv run python scripts/smoke.py
