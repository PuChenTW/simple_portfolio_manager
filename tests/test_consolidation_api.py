"""The consolidation surface as an agent drives it."""

import asyncio
from decimal import Decimal

import pytest

from portfolio_manager.taxonomy import AssetClass, Provenance


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


def test_renaming_a_group_leaves_its_membership_and_currency_intact(harness, group) -> None:
    """A group is a reporting lens: its name carries no accounting meaning."""
    before = harness.client.get(f"/api/v1/portfolio-groups/{group['id']}").json()

    renamed = harness.client.patch(
        f"/api/v1/portfolio-groups/{group['id']}", json={"name": "Everything (renamed)"}
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body["name"] == "Everything (renamed)"
    assert body["id"] == group["id"]
    assert body["reporting_currency"] == "USD", "a rename must not touch the reporting currency"
    assert set(body["portfolio_ids"]) == {group["usd"], group["twd"]}
    assert body["created_at"] == before["created_at"]
    assert body["updated_at"] >= before["updated_at"]


def test_two_groups_may_share_a_name(harness, group) -> None:
    """Group names are not unique, so a rename has no collision to report."""
    other = harness.client.post(
        "/api/v1/portfolio-groups",
        json={"name": "Other", "reporting_currency": "USD", "portfolio_ids": [group["usd"]]},
    ).json()

    renamed = harness.client.patch(
        f"/api/v1/portfolio-groups/{other['id']}", json={"name": "Everything"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Everything"


def test_renaming_a_missing_group_is_reported(harness) -> None:
    missing = harness.client.patch("/api/v1/portfolio-groups/nope", json={"name": "Anything"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "portfolio_group_not_found"


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


def test_summary_reports_each_holding_asset_class_with_its_provenance(harness, group) -> None:
    """The allocation view reads exposure from the summary, so it must travel with each row.

    2330.TW is a common stock the provider classifies itself, so it arrives `equity` at `derived`
    trust without anyone intervening.
    """
    body = harness.client.get(f"/api/v1/portfolio-groups/{group['id']}/summary").json()
    rows = {row["ticker"]: row for row in body["positions"]}

    assert rows["2330.TW"]["asset_class"] == AssetClass.EQUITY.value
    assert rows["2330.TW"]["asset_class_provenance"] == Provenance.DERIVED.value


def test_a_fund_holding_stays_unclassified_in_the_summary(harness) -> None:
    """The gap must reach the allocation view rather than being absorbed into equity.

    A provider states a fund's wrapper and never its contents, so GLD -- gold bullion -- would be
    silently misfiled as equity by any default. Reporting it unclassified is what makes the
    allocation view able to show the gap and someone able to close it.
    """
    portfolio_id = harness.portfolio("US", "USD")
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "d-1", "transaction_type": "deposit", "amount": "50000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "b-1", "transaction_type": "buy", "ticker": "GLD",
              "quantity": "10", "unit_price": "250"},
    )
    group_id = harness.client.post(
        "/api/v1/portfolio-groups",
        json={"name": "Funds", "reporting_currency": "USD", "portfolio_ids": [portfolio_id]},
    ).json()["id"]

    body = harness.client.get(f"/api/v1/portfolio-groups/{group_id}/summary").json()
    row = next(item for item in body["positions"] if item["ticker"] == "GLD")
    assert row["asset_class"] == AssetClass.UNCLASSIFIED.value
    # `derived`, not `unclassified`: the provider did answer -- it said "ETF" -- and that answer
    # names a wrapper, not an exposure. The provenance records that a source was consulted and
    # came up short, which is what tells a reader the gap needs a human rather than a refetch.
    assert row["asset_class_provenance"] == Provenance.DERIVED.value


def test_a_manual_override_reaches_the_summary_and_outranks_the_provider(harness) -> None:
    """Closing the gap is what the classification page does; the summary must reflect it."""
    portfolio_id = harness.portfolio("US", "USD")
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "d-1", "transaction_type": "deposit", "amount": "50000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "b-1", "transaction_type": "buy", "ticker": "GLD",
              "quantity": "10", "unit_price": "250"},
    )
    group_id = harness.client.post(
        "/api/v1/portfolio-groups",
        json={"name": "Funds", "reporting_currency": "USD", "portfolio_ids": [portfolio_id]},
    ).json()["id"]

    override = harness.client.put(
        "/api/v1/instruments/GLD/classification",
        json={"request_id": "o-1", "field": "asset_class",
              "value": AssetClass.COMMODITY.value, "reason": "holds gold bullion"},
    )
    assert override.status_code == 200, override.text

    body = harness.client.get(f"/api/v1/portfolio-groups/{group_id}/summary").json()
    row = next(item for item in body["positions"] if item["ticker"] == "GLD")
    assert row["asset_class"] == AssetClass.COMMODITY.value
    assert row["asset_class_provenance"] == Provenance.MANUAL_OVERRIDE.value

    # Retracting restores the provider's view rather than leaving the override behind.
    retract = harness.client.put(
        "/api/v1/instruments/GLD/classification",
        json={"request_id": "o-2", "field": "asset_class", "reason": "undo", "retract": True},
    )
    assert retract.status_code == 200, retract.text
    body = harness.client.get(f"/api/v1/portfolio-groups/{group_id}/summary").json()
    row = next(item for item in body["positions"] if item["ticker"] == "GLD")
    assert row["asset_class"] == AssetClass.UNCLASSIFIED.value


def test_resolving_asset_class_for_a_page_costs_one_query(harness) -> None:
    """Guards the N+1 the batch resolver exists to prevent.

    `_consolidate_position` runs per holding, so resolving classification inside it would cost a
    query per row -- the pattern `legs_for_events` already avoids elsewhere in this codebase.
    """
    from sqlalchemy import event

    portfolio_id = harness.portfolio("US", "USD")
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "d-1", "transaction_type": "deposit", "amount": "500000"},
    )
    for index, ticker in enumerate(("VOO", "VT", "SOXX", "GLD", "BOXX")):
        harness.client.post(
            f"/api/v1/portfolios/{portfolio_id}/transactions",
            json={"request_id": f"b-{index}", "transaction_type": "buy", "ticker": ticker,
                  "quantity": "10", "unit_price": "110"},
        )
    group_id = harness.client.post(
        "/api/v1/portfolio-groups",
        json={"name": "Funds", "reporting_currency": "USD", "portfolio_ids": [portfolio_id]},
    ).json()["id"]

    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "instrument_classifications" in statement:
            seen.append(statement)

    engine = harness.session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        body = harness.client.get(f"/api/v1/portfolio-groups/{group_id}/summary").json()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(body["positions"]) == 5
    assert len(seen) == 1, f"expected one batched query, got {len(seen)}"
