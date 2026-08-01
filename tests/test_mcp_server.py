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
    get_instrument_profile,
    get_market_history,
    get_portfolio,
    get_technical_snapshot,
    list_portfolios,
    map_instrument_issuer,
    record_transaction,
    set_instrument_classification_override,
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

    await record_transaction(
        portfolio_id=portfolio_id,
        request_id="cash-1",
        transaction_type="deposit",
        amount="10000",
    )
    detail = await record_transaction(
        portfolio_id=portfolio_id,
        request_id="trade-1",
        transaction_type="buy",
        ticker="AAPL",
        quantity="10",
        unit_price="140",
        fee="1",
    )
    assert detail["event"]["event_type"] == "buy"
    assert detail["balance"]["balanced"] is True


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
        await record_transaction(
            portfolio_id=portfolio["id"],
            request_id="sell-1",
            transaction_type="sell",
            ticker="AAPL",
            quantity="5",
            unit_price="140",
        )
    assert excinfo.value.code == "insufficient_position"


async def test_write_is_idempotent(mcp_client) -> None:
    portfolio = await create_portfolio(name="US", base_currency="USD")
    portfolio_id = portfolio["id"]
    first = await record_transaction(
        portfolio_id=portfolio_id,
        request_id="cash-9",
        transaction_type="deposit",
        amount="500",
    )
    second = await record_transaction(
        portfolio_id=portfolio_id,
        request_id="cash-9",
        transaction_type="deposit",
        amount="500",
    )
    assert first["event"]["id"] == second["event"]["id"]


async def test_market_research_tools_forward_query_parameters(mcp_client) -> None:
    history = await get_market_history(
        "AAPL",
        start_date="2026-07-20",
        end_date="2026-07-24",
        interval="1wk",
        adjustment="unadjusted",
    )
    assert history["requested_start_date"] == "2026-07-20"
    assert history["requested_end_date"] == "2026-07-24"
    assert history["interval"] == "1wk"
    assert history["adjustment"] == "unadjusted"

    snapshot = await get_technical_snapshot(
        "AAPL",
        as_of="2026-07-24",
        benchmark="MSFT",
        event_date="2026-07-20",
        lookback_years=3,
    )
    assert snapshot["as_of"] == "2026-07-24"
    assert snapshot["relative_strength"]["benchmark"] == "MSFT"
    assert snapshot["event_analysis"]["requested_event_date"] == "2026-07-20"


async def test_market_research_error_preserves_api_envelope(mcp_client) -> None:
    with pytest.raises(ApiError) as excinfo:
        await get_technical_snapshot("UNKNOWN", as_of="2026-07-24")
    assert excinfo.value.code == "market_data_unavailable"
    assert excinfo.value.details["ticker"] == "UNKNOWN"
    assert excinfo.value.details["as_of"] == "2026-07-24"


async def test_registered_tools_cover_every_api_operation() -> None:
    """Each REST operation_id must have a matching MCP tool, per the repository guidelines."""
    tools = {tool.name for tool in await mcp_server.mcp.list_tools()}
    operations = {
        route.operation_id for route in app.routes if getattr(route, "operation_id", None)
    }
    assert operations <= tools, f"API operations without an MCP tool: {operations - tools}"


async def test_instrument_identity_tools_round_trip(mcp_client) -> None:
    profile = await get_instrument_profile("GLD")
    assert profile["classification"]["security_type"]["value"] == "etf"
    assert profile["classification"]["asset_class"]["value"] == "unclassified"

    corrected = await set_instrument_classification_override(
        reference="GLD",
        request_id="gld-mcp-1",
        field="asset_class",
        value="commodity",
        reason="SPDR Gold Shares holds allocated gold bullion",
    )
    assert corrected["classification"]["asset_class"]["value"] == "commodity"
    assert corrected["classification"]["asset_class"]["provenance"] == "manual_override"

    mapped = await map_instrument_issuer(
        reference="TSM",
        request_id="tsm-mcp-1",
        legal_name="Taiwan Semiconductor Manufacturing Company Limited",
        display_name="TSMC",
        country_of_domicile="TW",
    )
    assert mapped["issuer"]["display_name"] == "TSMC"


async def test_classification_override_error_preserves_api_envelope(mcp_client) -> None:
    with pytest.raises(ApiError) as excinfo:
        await set_instrument_classification_override(
            reference="AAPL",
            request_id="bad-mcp-1",
            field="asset_class",
            value="not_a_member",
            reason="typo protection",
        )
    assert excinfo.value.code == "invalid_classification_value"
