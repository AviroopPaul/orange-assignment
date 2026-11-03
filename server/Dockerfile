# One Python image for api, worker, scheduler, migrate and mock-provider, with different commands.
FROM python:3.12-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir "uv>=0.5,<1"
WORKDIR /srv
COPY pyproject.toml uv.lock ./

FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY mock_provider ./mock_provider
RUN useradd --create-home --uid 1000 errand && chown -R errand /srv
USER errand

FROM base AS test
RUN uv sync --frozen --no-install-project
COPY . .
RUN useradd --create-home --uid 1000 errand && chown -R errand /srv
USER errand
