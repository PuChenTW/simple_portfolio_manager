import asyncio

import pytest

from portfolio_manager.api import StripPrefixMiddleware
from portfolio_manager.config import load_settings


def test_load_settings_normalizes_trailing_slash(monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_URL_PREFIX", "/portfolio-manager/")
    assert load_settings().url_prefix == "/portfolio-manager"


def test_load_settings_defaults_url_prefix_to_empty(monkeypatch) -> None:
    monkeypatch.delenv("PORTFOLIO_URL_PREFIX", raising=False)
    assert load_settings().url_prefix == ""


def test_load_settings_rejects_prefix_without_leading_slash(monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_URL_PREFIX", "portfolio-manager")
    with pytest.raises(ValueError, match="PORTFOLIO_URL_PREFIX"):
        load_settings()


def _run_middleware(prefix: str, path: str) -> dict:
    seen_scopes = []

    async def inner_app(scope, receive, send) -> None:
        seen_scopes.append(scope)

    async def run() -> None:
        middleware = StripPrefixMiddleware(inner_app, prefix=prefix)
        await middleware({"type": "http", "path": path}, None, None)

    asyncio.run(run())
    return seen_scopes[0]


def test_strip_prefix_middleware_rewrites_matching_path() -> None:
    scope = _run_middleware("/portfolio-manager", "/portfolio-manager/api/v1/portfolios")
    assert scope["path"] == "/api/v1/portfolios"


def test_strip_prefix_middleware_reduces_bare_prefix_to_root() -> None:
    scope = _run_middleware("/portfolio-manager", "/portfolio-manager")
    assert scope["path"] == "/"


def test_strip_prefix_middleware_passes_through_unmatched_path() -> None:
    scope = _run_middleware("/portfolio-manager", "/health")
    assert scope["path"] == "/health"
