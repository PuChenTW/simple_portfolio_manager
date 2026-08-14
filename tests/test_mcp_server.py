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
    list_journal_events,
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


async def test_tool_return_annotations_match_the_endpoint_they_wrap() -> None:
    """A tool annotated `dict` over an endpoint returning a list fails only when called.

    FastMCP derives each tool's output schema from its return annotation, so the mismatch is
    invisible until an agent calls it and the response is validated against the wrong model --
    `list_portfolio_groups` shipped that way and failed with a `DictModel` validation error on a
    perfectly good array. Comparing the two declarations catches it at build time instead.
    """
    spec = app.openapi()
    returns_array = set()
    for operations in spec["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            responses = operation.get("responses", {})
            body = responses.get("200") or responses.get("201") or {}
            schema = body.get("content", {}).get("application/json", {}).get("schema", {})
            if schema.get("type") == "array":
                returns_array.add(operation["operationId"])

    mismatched = []
    for tool in await mcp_server.mcp.list_tools():
        if tool.name not in returns_array:
            continue
        # A list-returning tool is wrapped as {"result": [...]}; a dict-returning one is not.
        result = (tool.outputSchema or {}).get("properties", {}).get("result", {})
        if result.get("type") != "array":
            mismatched.append(tool.name)

    assert not mismatched, (
        f"tools whose endpoint returns an array but whose annotation does not: {mismatched}"
    )


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


# --- Prompts and resources --------------------------------------------------
#
# These carry what no single tool docstring can: the order operations go in, and the vocabulary
# a call must use. They are generated from the same enums the API validates against, so these
# tests exist to catch the two ways that guarantee breaks -- a value that stops being listed,
# and a workflow step that stops matching the tool it names.


async def test_every_prompt_and_resource_is_registered() -> None:
    prompts = {prompt.name for prompt in await mcp_server.mcp.list_prompts()}
    assert prompts == {
        "open_account_with_holdings",
        "record_daily_activity",
        "analyze_performance",
        "audit_data_quality",
    }

    resources = {str(resource.uri) for resource in await mcp_server.mcp.list_resources()}
    assert resources == {
        "portfolio://conventions",
        "portfolio://taxonomy",
        "portfolio://portfolios",
    }


async def test_taxonomy_resource_lists_every_accepted_value() -> None:
    """A vocabulary reference that omits a legal value sends agents to a rejected call."""
    from portfolio_manager.taxonomy import AssetClass, Provenance, SecurityType

    contents = await mcp_server.mcp.read_resource("portfolio://taxonomy")
    text = list(contents)[0].content

    for member in (*AssetClass, *SecurityType, *Provenance):
        assert f"`{member.value}`" in text, f"{member.value} missing from the taxonomy resource"


async def test_conventions_resource_documents_every_error_code() -> None:
    """Agents branch on `code`, so an undocumented code is one they cannot handle."""
    import re
    from pathlib import Path

    source = Path(mcp_server.__file__).parent
    raised = set()
    for path in source.glob("*.py"):
        text = path.read_text()
        raised |= set(re.findall(r'DomainError\(\s*\d+,\s*"([a-z_]+)"', text))
        raised |= set(re.findall(r'DomainError\(\s*\d+,\s*\n\s*"([a-z_]+)"', text))

    undocumented = raised - set(mcp_server.ERROR_CODES)
    assert not undocumented, f"error codes missing from portfolio://conventions: {undocumented}"

    contents = await mcp_server.mcp.read_resource("portfolio://conventions")
    text = list(contents)[0].content
    for code in mcp_server.ERROR_CODES:
        assert f"`{code}`" in text


async def test_prompts_only_reference_tools_that_exist() -> None:
    """A workflow naming a removed tool is worse than no workflow: it fails mid-task."""
    import re

    tools = {tool.name for tool in await mcp_server.mcp.list_tools()}
    known = tools | {"record_transaction", "reverse_transaction"}

    for prompt in await mcp_server.mcp.list_prompts():
        rendered = await mcp_server.mcp.get_prompt(prompt.name, {})
        text = " ".join(message.content.text for message in rendered.messages)
        # Tool names appear as `name(` in the workflow steps.
        for name in re.findall(r"`([a-z_]+)\(", text):
            assert name in known, f"{prompt.name} references unknown tool {name}"


async def test_the_opening_balance_prompt_states_the_rule_that_prevents_the_error() -> None:
    """The prompt exists for one reason: opening cash must cover the holdings' cost.

    An agent that misses this hits `journal_out_of_balance` and may then invent a
    `settlement_amount` to force the event through, writing a wrong cost basis silently.
    """
    rendered = await mcp_server.mcp.get_prompt(
        "open_account_with_holdings",
        {"account_name": "Broker", "base_currency": "USD", "opening_date": "2026-01-02"},
    )
    text = " ".join(message.content.text for message in rendered.messages)

    assert "transfer_in" in text and "deposit" in text, "must contrast the two cash types"
    assert "original cost" in text
    assert "journal_out_of_balance" in text
    assert "Broker" in text and "USD" in text, "arguments must reach the rendered prompt"


async def test_the_inventory_resource_survives_an_unreachable_api(monkeypatch) -> None:
    """A resource that raises leaves a client with no listing at all; this degrades instead."""
    monkeypatch.setattr(mcp_server, "_client", None)
    contents = await mcp_server.mcp.read_resource("portfolio://portfolios")
    text = list(contents)[0].content
    assert "Portfolios" in text


async def test_list_journal_events_forwards_include_legs(mcp_client) -> None:
    """Agents ask "what did I trade that day"; without legs the answer needs a call per row."""
    portfolio = await create_portfolio(name="Legs", base_currency="USD")
    portfolio_id = portfolio["id"]
    await record_transaction(
        portfolio_id=portfolio_id,
        request_id="legs-cash",
        transaction_type="deposit",
        amount="10000",
    )
    await record_transaction(
        portfolio_id=portfolio_id,
        request_id="legs-buy",
        transaction_type="buy",
        ticker="AAPL",
        quantity="10",
        unit_price="140",
    )

    headers = await list_journal_events(portfolio_id=portfolio_id)
    assert all(event["legs"] is None for event in headers["items"])

    detailed = await list_journal_events(portfolio_id=portfolio_id, include_legs=True)
    buy_event = next(item for item in detailed["items"] if item["event_type"] == "buy")
    security = next(leg for leg in buy_event["legs"] if leg["leg_type"] == "security")
    assert security["ticker"] == "AAPL"
