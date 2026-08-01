"""The consolidation surface as an agent drives it."""

import asyncio
from decimal import Decimal

import pytest


@pytest.fixture
def group(harness) -> dict:
    """A USD portfolio and a TWD portfolio reported together in USD."""
    usd = harness.portfolio("US", "USD")
    twd = harness.portfolio("TW", "TWD")
    for portfolio_id, ticker, price in ((usd, "AAPL", "140"), (twd, "2330.TW", "1100")):
        harness.client.post(
            f"/api/v1/portfolios/{portfolio_id}/transactions",
            json={"request_id": f"d-{portfolio_id}", "transaction_type": "deposit",
                  "amount": "500000"},
        )
        harness.client.post(
            f"/api/v1/portfolios/{portfolio_id}/transactions",
            json={"request_id": f"b-{portfolio_id}", "transaction_type": "buy",
                  "ticker": ticker, "quantity": "10", "unit_price": price},
        )
    response = harness.client.post(
        "/api/v1/portfolio-groups",
        json={"name": "Everything", "reporting_currency": "USD",
              "portfolio_ids": [usd, twd]},
    )
    assert response.status_code == 201, response.text
    return {"id": response.json()["id"], "usd": usd, "twd": twd}


def test_group_round_trip_over_http(harness, group) -> None:
    body = harness.client.get(f"/api/v1/portfolio-groups/{group['id']}").json()
    assert body["reporting_currency"] == "USD"
    assert set(body["portfolio_ids"]) == {group["usd"], group["twd"]}


def test_consolidated_summary_reports_its_conversions(harness, group) -> None:
    body = harness.client.get(f"/api/v1/portfolio-groups/{group['id']}/summary").json()

    assert body["calculation_method"], "how the total was produced must travel with it"
    assert isinstance(body["total_value"], str), "decimals cross the boundary as strings"
    assert "converted_value_coverage_percent" in body
    for row in body["positions"]:
        assert row["local_currency"]
        assert "reporting_market_value" in row


def test_an_unconvertible_currency_is_reported_not_hidden(harness, group) -> None:
    """The fake provider serves no FX pair, so the TWD side cannot convert."""
    body = harness.client.get(f"/api/v1/portfolio-groups/{group['id']}/summary").json()

    twd = [item for item in body["unconverted"] if item["currency"] == "TWD"]
    assert twd, "unconvertible value must be listed"
    assert twd[0]["reason"]
    assert Decimal(body["converted_value_coverage_percent"]) < Decimal("100")
    assert body["warnings"]


def test_members_can_be_replaced(harness, group) -> None:
    body = harness.client.put(
        f"/api/v1/portfolio-groups/{group['id']}/members",
        json={"portfolio_ids": [group["usd"]]},
    ).json()
    assert body["portfolio_ids"] == [group["usd"]]


def test_an_unknown_group_returns_a_machine_readable_error(harness) -> None:
    response = harness.client.get("/api/v1/portfolio-groups/missing/summary")
    assert response.status_code == 404
    assert set(response.json()) == {"code", "message", "details"}


def test_a_group_needs_a_portfolio(harness) -> None:
    response = harness.client.post(
        "/api/v1/portfolio-groups",
        json={"name": "Empty", "reporting_currency": "USD", "portfolio_ids": []},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "empty_group"


def test_every_consolidation_route_has_an_mcp_tool(harness) -> None:
    from portfolio_manager.api import app
    from portfolio_manager.mcp_server import mcp

    schema = app.openapi()
    operations = {
        route["operationId"]
        for item in schema["paths"].values()
        for route in item.values()
        if isinstance(route, dict) and "consolidation" in route.get("tags", [])
    }
    tools = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert operations <= tools, f"endpoints without a tool: {sorted(operations - tools)}"


def test_a_group_can_be_deleted_over_http(harness, group) -> None:
    assert harness.client.delete(f"/api/v1/portfolio-groups/{group['id']}").status_code == 204
    assert harness.client.get(f"/api/v1/portfolio-groups/{group['id']}").status_code == 404
    assert harness.client.get("/api/v1/portfolio-groups").json() == []


def test_deleting_a_group_does_not_touch_its_portfolios(harness, group) -> None:
    """The portfolios outlive the lens used to report them together."""
    before = harness.client.get(f"/api/v1/portfolios/{group['usd']}/summary").json()

    harness.client.delete(f"/api/v1/portfolio-groups/{group['id']}")

    after = harness.client.get(f"/api/v1/portfolios/{group['usd']}/summary")
    assert after.status_code == 200
    assert after.json()["total_value"] == before["total_value"]
    assert len(harness.client.get("/api/v1/portfolios").json()) == 2


def test_deleting_an_unknown_group_is_a_machine_readable_404(harness) -> None:
    response = harness.client.delete("/api/v1/portfolio-groups/missing")
    assert response.status_code == 404
    assert response.json()["code"] == "portfolio_group_not_found"
