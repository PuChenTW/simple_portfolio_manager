"""The valuation surface as an agent drives it: HTTP round trips and MCP tool registration."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

TODAY = datetime.now(UTC).date()
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture
def held(harness) -> str:
    """A portfolio holding 10 AAPL, funded well above the purchase price."""
    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "d", "transaction_type": "deposit", "amount": "5000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "b",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "140",
        },
    )
    return portfolio_id


def test_snapshot_round_trip_over_http(harness, held) -> None:
    response = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots",
        json={"valuation_date": TODAY.isoformat()},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] in {"complete", "partial"}
    assert body["calculation_version"] == "v1"
    assert body["calculation_method"], "every derived value states how it was produced"
    assert Decimal(body["cash_value"]) == Decimal("3600")
    assert len(body["positions"]) == 1
    assert body["positions"][0]["ticker_at_time"] == "AAPL"


def test_decimal_values_are_returned_as_strings(harness, held) -> None:
    """Plan principle 4: monetary values cross the API boundary as decimal strings."""
    body = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots",
        json={"valuation_date": TODAY.isoformat()},
    ).json()

    for field in ("securities_value", "cash_value", "total_value", "cost_basis"):
        assert isinstance(body[field], str), f"{field} must be a decimal string"


def test_repeating_a_snapshot_request_is_idempotent(harness, held) -> None:
    first = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots",
        json={"valuation_date": TODAY.isoformat()},
    ).json()
    second = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots",
        json={"valuation_date": TODAY.isoformat()},
    ).json()
    assert first["id"] == second["id"]


def test_a_future_valuation_date_is_refused(harness, held) -> None:
    response = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots",
        json={"valuation_date": (TODAY + timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "valuation_date_in_future"


def test_unknown_portfolio_returns_a_machine_readable_error(harness) -> None:
    response = harness.client.post(
        "/api/v1/portfolios/missing/valuation-snapshots",
        json={"valuation_date": TODAY.isoformat()},
    )
    assert response.status_code == 404
    assert set(response.json()) == {"code", "message", "details"}


def test_nav_history_reports_gaps_instead_of_filling_them(harness, held) -> None:
    harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots",
        json={"valuation_date": TODAY.isoformat()},
    )

    body = harness.client.get(
        f"/api/v1/portfolios/{held}/nav-history",
        params={
            "start_date": (TODAY - timedelta(days=3)).isoformat(),
            "end_date": TODAY.isoformat(),
        },
    ).json()

    assert len(body["snapshots"]) == 1
    assert len(body["missing_dates"]) == 3, "unbuilt dates are reported, never interpolated"
    assert any("interpolated" in warning for warning in body["warnings"])


def test_nav_history_rejects_an_inverted_range(harness, held) -> None:
    response = harness.client.get(
        f"/api/v1/portfolios/{held}/nav-history",
        params={"start_date": TODAY.isoformat(), "end_date": YESTERDAY.isoformat()},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_date_range"


def test_rebuild_then_read_produces_a_complete_series(harness, held) -> None:
    start = TODAY - timedelta(days=2)
    rebuilt = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots/rebuild",
        json={"start_date": start.isoformat(), "end_date": TODAY.isoformat()},
    )
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["created"] == 3

    history = harness.client.get(
        f"/api/v1/portfolios/{held}/nav-history",
        params={"start_date": start.isoformat(), "end_date": TODAY.isoformat()},
    ).json()
    assert len(history["snapshots"]) == 3
    assert history["missing_dates"] == []


def test_rebuilding_twice_skips_existing_dates(harness, held) -> None:
    start = TODAY - timedelta(days=2)
    payload = {"start_date": start.isoformat(), "end_date": TODAY.isoformat()}
    harness.client.post(f"/api/v1/portfolios/{held}/valuation-snapshots/rebuild", json=payload)
    second = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots/rebuild", json=payload
    ).json()

    assert second["created"] == 0
    assert second["skipped_existing"] == 3


def test_an_unpriceable_holding_yields_a_partial_snapshot(harness, held) -> None:
    harness.provider.fail = True
    body = harness.client.post(
        f"/api/v1/portfolios/{held}/valuation-snapshots",
        json={"valuation_date": TODAY.isoformat()},
    ).json()

    assert body["status"] == "partial"
    assert Decimal(body["securities_value"]) == Decimal("0")
    assert Decimal(body["unpriced_market_value"]) == Decimal("1400")
    assert body["positions"][0]["price"] is None
    assert body["positions"][0]["market_value"] is None
    assert body["warnings"]


def test_the_new_valuation_tools_are_registered(harness) -> None:
    from portfolio_manager.mcp_server import mcp

    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "create_valuation_snapshot",
        "rebuild_valuation_snapshots",
        "get_nav_history",
    } <= names


def test_every_valuation_route_has_an_mcp_tool(harness) -> None:
    """AGENTS.md requires a matching tool whenever an endpoint is added."""
    from portfolio_manager.api import app
    from portfolio_manager.mcp_server import mcp

    schema = app.openapi()
    valuation_ops = {
        route["operationId"]
        for item in schema["paths"].values()
        for route in item.values()
        if isinstance(route, dict) and "valuation" in route.get("tags", [])
    }
    tools = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert valuation_ops <= tools, f"endpoints without a tool: {sorted(valuation_ops - tools)}"
