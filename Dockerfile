FROM ghcr.io/astral-sh/uv:0.9.0 AS uv

# The v2 dashboard is built here and copied into the runtime image as plain static files, so no
# JavaScript toolchain ships. The same layer-ordering rule as the Python install below applies:
# the lockfile is copied on its own, so editing a component does not reinstall dependencies.
FROM oven/bun:1.3-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile
COPY frontend ./
# vite.config.ts writes to ../src/portfolio_manager/static/v2, which is outside this stage's
# build directory. Redirect it here so the output lands somewhere the runtime stage can copy.
RUN bun run build --outDir dist --emptyOutDir

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

# After `COPY src`, which would otherwise overwrite this directory: the build output is
# gitignored and absent from the build context, so copying src on top would leave no v2 at all.
# api.py mounts /v2 only when this directory exists, so a build without it still serves the API.
COPY --from=frontend /build/dist ./src/portfolio_manager/static/v2

COPY alembic.ini ./
COPY alembic ./alembic
