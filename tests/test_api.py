import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path as FilePath

import pytest

from portfolio_manager import api, services
from portfolio_manager.models import QuoteCache
from portfolio_manager.sessions import is_market_open


def test_health_and_portfolios_are_isolated(harness) -> None:
    assert harness.client.get("/health").json()["status"] == "ok"
    first = harness.portfolio()
    second = harness.portfolio("Taiwan", "TWD")

    portfolios = harness.client.get("/api/v1/portfolios").json()
    assert {item["id"] for item in portfolios} == {first, second}
    assert harness.client.get(f"/api/v1/portfolios/{first}").json()["base_currency"] == "USD"

    duplicate = harness.client.post(
        "/api/v1/portfolios", json={"name": "USD portfolio", "base_currency": "USD"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "portfolio_name_exists"


def test_delete_portfolio_cascades_and_is_idempotent_on_missing_id(harness) -> None:
    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/cash-transactions",
        json={"request_id": "cash-1", "action": "deposit", "amount": "10000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/trades",
        json={
            "request_id": "trade-1",
            "ticker": "AAPL",
            "side": "buy",
            "quantity": "10",
            "unit_price": "140",
        },
    )

    delete = harness.client.delete(f"/api/v1/portfolios/{portfolio_id}")
    assert delete.status_code == 204
    assert delete.text == ""

    missing = harness.client.get(f"/api/v1/portfolios/{portfolio_id}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "portfolio_not_found"

    again = harness.client.delete(f"/api/v1/portfolios/{portfolio_id}")
    assert again.status_code == 404
    assert again.json()["code"] == "portfolio_not_found"


def test_delete_portfolio_succeeds_with_applied_corporate_actions_and_reversals(
    harness,
) -> None:
    """Two references into journal_events are RESTRICT, so a naive cascade fails.

    A corporate-action application cites the event it posted, and a reversal cites the
    event it undoes. Both deliberately block deleting that event, which used to make
    deleting the whole portfolio return 500 once it had either.
    """
    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "d", "transaction_type": "deposit", "amount": "20000"},
    )
    buy = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "b",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "100",
            "unit_price": "140",
        },
    )
    assert buy.status_code == 201, buy.text

    # Reverse a separate buy, so the reversal reference exists while a live AAPL
    # position remains for the split to apply to.
    mistake = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "b2",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "140",
        },
    )
    assert mistake.status_code == 201, mistake.text
    reversal = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}"
        f"/transactions/{mistake.json()['event']['id']}/reversal",
        json={"request_id": "rev-1"},
    )
    assert reversal.status_code == 201, reversal.text

    action = harness.client.post(
        "/api/v1/corporate-actions",
        json={
            "request_id": "ca-del-1",
            "ticker": "AAPL",
            "action_type": "split",
            "ex_date": "2026-06-01T00:00:00Z",
            "ratio": "2",
            "source": "issuer announcement",
        },
    )
    assert action.status_code == 201, action.text
    applied = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}"
        f"/corporate-actions/{action.json()['id']}/apply",
        json={"request_id": "apply-del-1"},
    )
    assert applied.status_code == 201, applied.text

    deleted = harness.client.delete(f"/api/v1/portfolios/{portfolio_id}")
    assert deleted.status_code == 204, deleted.text
    assert harness.client.get(f"/api/v1/portfolios/{portfolio_id}").status_code == 404


def test_dashboard_and_static_assets_are_available_without_changing_openapi(harness) -> None:
    dashboard = harness.client.get("/")
    assert dashboard.status_code == 200
    assert dashboard.headers["content-type"].startswith("text/html")
    assert 'id="portfolio-select"' in dashboard.text
    assert 'src="static/dashboard.js"' in dashboard.text
    # Must stay relative: an absolute base breaks either direct or sub-path access.
    assert '<base href="./">' in dashboard.text
    # The delete control and every id its handler queries; a renamed id would
    # otherwise fail silently in the browser.
    for node_id in (
        "delete-button",
        "delete-dialog",
        "delete-form",
        "delete-dialog-target",
        "delete-dialog-scale",
        "delete-confirm-name",
        "delete-dialog-error",
        "delete-cancel",
        "delete-submit",
    ):
        assert f'id="{node_id}"' in dashboard.text

    stylesheet = harness.client.get("/static/dashboard.css")
    script = harness.client.get("/static/dashboard.js")
    assert stylesheet.status_code == script.status_code == 200
    assert "summary-grid" in stylesheet.text
    assert "loadDashboard" in script.text

    assert "/" not in harness.client.get("/openapi.json").json()["paths"]




def test_cash_balance_guards_and_idempotency(harness) -> None:
    portfolio_id = harness.portfolio()
    endpoint = f"/api/v1/portfolios/{portfolio_id}/transactions"
    deposit = {
        "request_id": "cash-1",
        "transaction_type": "deposit",
        "amount": "1000.25",
    }

    first = harness.client.post(endpoint, json=deposit)
    retry = harness.client.post(endpoint, json=deposit)
    assert first.status_code == retry.status_code == 201
    assert first.json()["event"]["id"] == retry.json()["event"]["id"]

    conflict = harness.client.post(endpoint, json={**deposit, "amount": "2"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    excessive = harness.client.post(
        endpoint,
        json={
            "request_id": "cash-2",
            "transaction_type": "withdrawal",
            "amount": "1001",
        },
    )
    assert excessive.status_code == 422
    assert excessive.json()["code"] == "insufficient_cash"

    summary = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    assert summary["cash_value"] == "1000.25"
    assert summary["total_value"] == "1000.25"
    assert summary["cash"]["weight_percent"] == "100"


def test_moving_average_pnl_fees_and_weights(harness) -> None:
    """Buy fees raise average cost; sell fees reduce realized P&L, and cash follows every leg."""
    portfolio_id = harness.portfolio()
    endpoint = f"/api/v1/portfolios/{portfolio_id}/transactions"
    harness.client.post(
        endpoint,
        json={"request_id": "cash", "transaction_type": "deposit", "amount": "5000"},
    )
    payloads = [
        {
            "request_id": "buy-1",
            "transaction_type": "buy",
            "ticker": "aapl",
            "quantity": "10",
            "unit_price": "100",
            "fee": "10",
        },
        {
            "request_id": "buy-2",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "120",
            "fee": "10",
        },
        {
            "request_id": "sell-1",
            "transaction_type": "sell",
            "ticker": "AAPL",
            "quantity": "5",
            "unit_price": "130",
            "fee": "5",
        },
    ]
    for payload in payloads:
        response = harness.client.post(endpoint, json=payload)
        assert response.status_code == 201, response.text

    summary = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    position = summary["positions"][0]
    assert position["quantity"] == "15"
    assert position["average_cost"] == "111"
    assert position["market_value"] == "2100"
    assert position["realized_pnl"] == "90"
    assert position["unrealized_pnl"] == "435"
    # Unlike the removed trade ledger, every leg settles: cash reflects what the trades consumed.
    assert summary["cash_value"] == "3425"
    assert Decimal(position["weight_percent"]) == Decimal("2100") / Decimal("5525") * 100
    assert Decimal(summary["cash"]["weight_percent"]) == Decimal("3425") / Decimal("5525") * 100

    retry = harness.client.post(endpoint, json=payloads[0])
    assert retry.status_code == 201
    events = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json()
    assert events["total"] == 4


def test_fractional_crypto_and_sell_guard(harness) -> None:
    portfolio_id = harness.portfolio()
    endpoint = f"/api/v1/portfolios/{portfolio_id}/transactions"
    harness.client.post(
        endpoint,
        json={"request_id": "seed", "transaction_type": "deposit", "amount": "10"},
    )
    buy = harness.client.post(
        endpoint,
        json={
            "request_id": "btc-buy",
            "transaction_type": "buy",
            "ticker": "BTC-USD",
            "quantity": "0.00000001",
            "unit_price": "90000.12345678",
        },
    )
    assert buy.status_code == 201, buy.text
    excessive = harness.client.post(
        endpoint,
        json={
            "request_id": "btc-sell",
            "transaction_type": "sell",
            "ticker": "BTC-USD",
            "quantity": "0.00000002",
            "unit_price": "100000",
        },
    )
    assert excessive.status_code == 422
    assert excessive.json()["code"] == "insufficient_position"


def test_currency_mismatch(harness) -> None:
    portfolio_id = harness.portfolio()
    response = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "wrong-currency",
            "transaction_type": "buy",
            "ticker": "2330.TW",
            "quantity": "1",
            "unit_price": "1000",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "currency_mismatch"


def test_tags_normalization_and_filters(harness) -> None:
    portfolio_id = harness.portfolio()
    endpoint = f"/api/v1/portfolios/{portfolio_id}/transactions"
    harness.client.post(
        endpoint,
        json={"request_id": "seed", "transaction_type": "deposit", "amount": "1000"},
    )
    for request_id, ticker in [("a", "AAPL"), ("m", "MSFT")]:
        response = harness.client.post(
            endpoint,
            json={
                "request_id": request_id,
                "transaction_type": "buy",
                "ticker": ticker,
                "quantity": "1",
                "unit_price": "100",
            },
        )
        assert response.status_code == 201

    tags_endpoint = f"/api/v1/portfolios/{portfolio_id}/positions/AAPL/tags"
    tags = harness.client.put(tags_endpoint, json={"tags": [" Core ", "CORE", "核心"]})
    assert tags.json()["tags"] == ["core", "核心"]
    harness.client.put(
        f"/api/v1/portfolios/{portfolio_id}/positions/MSFT/tags",
        json={"tags": ["core"]},
    )

    positions = f"/api/v1/portfolios/{portfolio_id}/positions"
    any_match = harness.client.get(positions, params=[("tag", "核心")]).json()["items"]
    all_match = harness.client.get(
        positions,
        params=[("tag", "core"), ("tag", "核心"), ("tag_mode", "all")],
    ).json()["items"]
    assert [item["ticker"] for item in any_match] == ["AAPL"]
    assert [item["ticker"] for item in all_match] == ["AAPL"]


def test_market_cache_stale_fallback_and_history(harness, monkeypatch) -> None:
    # Pinned to a Tuesday midday in New York. This test is about the provider-failure fallback,
    # and a 10-minute-old quote only reaches the provider while the market is open -- outside a
    # session it is legitimately still fresh, so an unpinned clock would make this pass or fail
    # depending on the hour it ran.
    # Expressed in UTC, not with a -04:00 offset: SQLite stores the wall clock and drops the
    # offset, so an offset-carrying value reads back shifted and the freshness check goes wrong.
    midday = datetime(2026, 8, 11, 16, 0, tzinfo=UTC)  # 12:00 in New York
    assert is_market_open("US", midday)
    monkeypatch.setattr(services, "utc_now", lambda: midday)

    endpoint = "/api/v1/market/instruments/AAPL"
    fresh = harness.client.get(endpoint)
    assert fresh.status_code == 200
    assert fresh.json()["quote"]["stale"] is False
    assert fresh.json()["indicators"]["rsi14"] == "55"
    assert harness.client.get(endpoint).status_code == 200
    assert harness.provider.calls == ["AAPL"]

    with harness.session_factory() as session:
        quote = session.get(QuoteCache, "AAPL")
        quote.fetched_at = midday - timedelta(minutes=10)
        session.commit()
    harness.provider.fail = True
    stale = harness.client.get(endpoint)
    assert stale.status_code == 200
    assert stale.json()["quote"]["stale"] is True
    assert stale.json()["warnings"]

    missing = harness.client.get("/api/v1/market/instruments/UNKNOWN")
    assert missing.status_code == 503
    assert missing.json()["code"] == "market_data_unavailable"

    harness.provider.fail = False
    history = harness.client.get("/api/v1/market/instruments/AAPL/history?days=30")
    assert history.status_code == 200
    assert len(history.json()["bars"]) == 30
    assert history.json()["adjustment"] == "yfinance_auto_adjust"
    assert history.json()["adjusted"] is True


def test_history_range_parameters_and_inclusive_end(harness) -> None:
    response = harness.client.get(
        "/api/v1/market/instruments/AAPL/history",
        params={
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
            "interval": "1wk",
            "adjustment": "unadjusted",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["provider"] == "Fake Market"
    assert data["interval"] == "1wk"
    assert data["adjustment"] == "unadjusted"
    assert data["adjusted"] is False
    assert data["requested_start_date"] == "2026-07-20"
    assert data["requested_end_date"] == "2026-07-24"
    assert data["actual_last_observation"] == "2026-07-24"
    assert [bar["timestamp"] for bar in data["bars"]] == sorted(
        bar["timestamp"] for bar in data["bars"]
    )


def test_history_period_parameters_are_mutually_exclusive(harness) -> None:
    response = harness.client.get(
        "/api/v1/market/instruments/AAPL/history",
        params={"days": 30, "start_date": "2026-01-01"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_technical_snapshot_benchmark_event_and_as_of(harness) -> None:
    response = harness.client.get(
        "/api/v1/market/instruments/AAPL/technical-snapshot",
        params={
            "as_of": "2026-07-24",
            "benchmark": "MSFT",
            "event_date": "2026-07-20",
            "lookback_years": 5,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ticker"] == "AAPL"
    assert data["provider"] == "Fake Market"
    assert data["as_of"] == "2026-07-24"
    assert data["actual_end_date"] == "2026-07-24"
    assert data["bar_count"] == 320
    assert data["trend"]["sma200"] is not None
    assert data["momentum"]["return_252d_percent"] is not None
    assert data["relative_strength"]["benchmark"] == "MSFT"
    assert data["relative_strength"]["common_observation_count"] == 320
    assert data["event_analysis"]["requested_event_date"] == "2026-07-20"
    assert data["event_analysis"]["effective_anchor_date"] == "2026-07-20"
    assert data["event_analysis"]["anchored_vwap"] is not None


def test_technical_snapshot_keeps_partial_results_with_warnings(harness) -> None:
    harness.provider.history_limit = 10
    response = harness.client.get(
        "/api/v1/market/instruments/AAPL/technical-snapshot",
        params={"as_of": "2026-07-24", "benchmark": "MSFT"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["trend"]["sma20"] is None
    assert data["momentum"]["return_20d_percent"] is None
    assert data["relative_strength"]["return_20d_percent"] is None
    assert data["warnings"]


def test_validation_error_shape(harness) -> None:
    response = harness.client.post(
        "/api/v1/portfolios", json={"name": "bad", "base_currency": "US"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["details"]["errors"]


def test_openapi_is_self_describing_for_agents(harness) -> None:
    response = harness.client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()

    operations = [
        operation
        for methods in schema["paths"].values()
        for operation in methods.values()
    ]
    operation_ids = [operation["operationId"] for operation in operations]
    assert len(operation_ids) == len(set(operation_ids))
    assert {
        "create_portfolio",
        "get_market_instrument",
        "get_market_history",
        "get_technical_snapshot",
        "record_transaction",
        "reverse_transaction",
        "replace_position_tags",
        "get_portfolio_summary",
    }.issubset(operation_ids)
    assert all(
        operation.get("summary") and operation.get("description") for operation in operations
    )

    assert "Recommended workflow" in schema["info"]["description"]
    assert schema["servers"][0]["url"] == "/"
    assert schema["x-agent-skill"]["workflow"]
    technical = schema["paths"][
        "/api/v1/market/instruments/{ticker}/technical-snapshot"
    ]["get"]
    assert "as_of" in technical["description"]
    event_schema = schema["components"]["schemas"]["EventAnalysisRead"]
    assert "typical price" in event_schema["properties"]["anchored_vwap"]["description"]
    transaction = schema["components"]["schemas"]["TransactionCreate"]
    assert transaction["examples"]
    assert "idempotency" in transaction["properties"]["request_id"]["description"]
    assert (
        "actual execution price"
        in transaction["properties"]["unit_price"]["description"].lower()
    )


def test_v2_dashboard_is_served_with_relative_assets(harness) -> None:
    """The built Svelte dashboard mounts at /v2 with paths that survive a stripping proxy.

    Skipped when `frontend/` has not been built, since the mount is conditional on the build
    output existing -- a developer who has not run `bun run build` still gets a working API.
    """
    built = FilePath(api.__file__).parent / "static" / "v2" / "index.html"
    if not built.is_file():
        pytest.skip("frontend/ has not been built; run `bun run build` in frontend/")

    page = harness.client.get("/v2/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")

    # Relative asset paths are the whole sub-path deployment mechanism. A leading slash here
    # serves a page whose assets 404 under a proxy that mounts the app below the root.
    assets = re.findall(r'(?:src|href)="([^"]+)"', page.text)
    assert assets, "built page referenced no assets"
    assert all(path.startswith("./") for path in assets), assets

    # Bare /v2 must reach the same page, or the relative paths resolve one directory too high.
    redirect = harness.client.get("/v2", follow_redirects=False)
    assert redirect.status_code in (301, 307, 308)
    assert redirect.headers["location"].endswith("/v2/")
