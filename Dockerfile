FROM ghcr.io/astral-sh/uv:0.9.0 AS uv
FROM python:3.13-slim

COPY --from=uv /uv /uvx /bin/

ENV PATH="/app/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies resolve from the lockfile alone, so this layer is keyed to pyproject.toml and
# uv.lock and survives every source edit. `--no-install-project` is what makes that possible:
# without it uv builds the project too, which needs `src/` and would tie the whole dependency
# install to any change in it.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Source changes invalidate only what follows. Installing the project alone is fast because its
# dependencies are already present in the venv above.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
