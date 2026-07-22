FROM ghcr.io/astral-sh/uv:0.9.0 AS uv
FROM python:3.13-slim

COPY --from=uv /uv /uvx /bin/

ENV PATH="/app/.venv/bin:$PATH" \
    PORTFOLIO_DB_PATH="/data/portfolio.db" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2)"]

CMD ["sh", "-c", "alembic upgrade head && uvicorn portfolio_manager.api:app --host 0.0.0.0 --port 8001"]
