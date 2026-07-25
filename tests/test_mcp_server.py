"""Tests for the MCP server, driven against the real FastAPI app in-memory.

The MCP tools are thin async wrappers over the HTTP API. We point the module's shared client
at the ASGI app (same DB + fake market provider as the API tests) and call the tool functions
directly, so no network or running server is required.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from portfolio_manager import mcp_server
from portfolio_manager.api import app
from portfolio_manager.mcp_server import (
    ApiError,
    create_portfolio,
    delete_portfolio,
    get_portfolio,
    list_portfolios,
    record_cash_transaction,
    record_trade,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def mcp_client(harness, monkeypatch) -> AsyncIterator[None]:
    """Point the MCP server's shared client at the same app/DB the API tests use.

    `harness` installs the DB + fake-provider dependency overrides on `app`; we just wrap an
    in-memory ASGI client around it so the tools exercise the real HTTP stack without a socket.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(mcp_server, "_client", client)
        yield


async def test_read_write_round_trip(mcp_client) -> None:
    portfolio = await create_portfolio(name="US long term", base_currency="USD")
    portfolio_id = portfolio["id"]

    listed = await list_portfolios()
    assert [item["id"] for item in listed] == [portfolio_id]

    fetched = await get_portfolio(portfolio_id)
    assert fetched["base_currency"] == "USD"

    await record_cash_transaction(
        portfolio_id=portfolio_id, request_id="cash-1", action="deposit", amount="10000"
    )
    trade = await record_trade(
        portfolio_id=portfolio_id,
        request_id="trade-1",
        ticker="AAPL",
        side="buy",
        quantity="10",
        unit_price="140",
        fee="1",
    )
    assert trade["ticker"] == "AAPL"
    assert trade["quantity"] == "10"


async def test_domain_error_preserves_envelope(mcp_client) -> None:
    with pytest.raises(ApiError) as excinfo:
        await get_portfolio("does-not-exist")
    error = excinfo.value
    assert error.status_code == 404
    assert error.code == "portfolio_not_found"
    assert error.details["id"] == "does-not-exist"


async def test_delete_portfolio_removes_it(mcp_client) -> None:
    portfolio = await create_portfolio(name="US", base_currency="USD")
    portfolio_id = portfolio["id"]

    result = await delete_portfolio(portfolio_id)
    assert result is None

    with pytest.raises(ApiError) as excinfo:
        await get_portfolio(portfolio_id)
    assert excinfo.value.code == "portfolio_not_found"


async def test_insufficient_position_error(mcp_client) -> None:
    portfolio = await create_portfolio(name="US", base_currency="USD")
    with pytest.raises(ApiError) as excinfo:
        await record_trade(
            portfolio_id=portfolio["id"],
            request_id="sell-1",
            ticker="AAPL",
            side="sell",
            quantity="5",
            unit_price="140",
        )
    assert excinfo.value.code == "insufficient_position"


async def test_write_is_idempotent(mcp_client) -> None:
    portfolio = await create_portfolio(name="US", base_currency="USD")
    portfolio_id = portfolio["id"]
    first = await record_cash_transaction(
        portfolio_id=portfolio_id, request_id="cash-9", action="deposit", amount="500"
    )
    second = await record_cash_transaction(
        portfolio_id=portfolio_id, request_id="cash-9", action="deposit", amount="500"
    )
    assert first["id"] == second["id"]
