from datetime import UTC, datetime

from portfolio_manager import api, consolidation


def group_for(harness):
    portfolio = harness.portfolio("Time", "TWD")
    group = harness.client.post("/api/v1/portfolio-groups", json={
        "name": "Time", "reporting_currency": "TWD", "portfolio_ids": [portfolio],
    }).json()
    return portfolio, group["id"]


def test_transaction_rejects_a_time_without_offset(harness):
    portfolio = harness.portfolio("Time", "TWD")
    response = harness.client.post(f"/api/v1/portfolios/{portfolio}/transactions", json={
        "request_id": "naive", "transaction_type": "deposit", "amount": "1000",
        "occurred_at": "2020-09-17T07:00:00",
    })
    assert response.status_code == 422


def test_transaction_normalizes_offset_to_utc(harness):
    portfolio = harness.portfolio("Time", "TWD")
    response = harness.client.post(f"/api/v1/portfolios/{portfolio}/transactions", json={
        "request_id": "aware", "transaction_type": "deposit", "amount": "1000",
        "occurred_at": "2020-09-17T07:00:00+08:00",
    })
    assert response.status_code == 201
    assert response.json()["event"]["occurred_at"] == "2020-09-16T23:00:00Z"


def test_transaction_rejects_future_time_with_server_clock(harness, monkeypatch):
    monkeypatch.setattr(api, "utc_now", lambda: datetime(2026, 9, 16, 23, tzinfo=UTC))
    portfolio = harness.portfolio("Time", "TWD")
    response = harness.client.post(f"/api/v1/portfolios/{portfolio}/transactions", json={
        "request_id": "future", "transaction_type": "deposit", "amount": "1000",
        "occurred_at": "2026-09-17T08:01:00+08:00",
    })
    assert response.status_code == 422
    assert response.json()["code"] == "transaction_time_in_future"
    assert response.json()["details"]["server_time"] == "2026-09-16T23:00:00+00:00"


def test_taipei_morning_summary_uses_now_and_local_date(harness, monkeypatch):
    portfolio, group = group_for(harness)
    monkeypatch.setattr(api, "utc_now", lambda: datetime(2031, 1, 1, tzinfo=UTC))
    seeded = harness.client.post(f"/api/v1/portfolios/{portfolio}/transactions", json={
        "request_id": "morning", "transaction_type": "deposit", "amount": "1000",
        "occurred_at": "2030-09-17T07:00:00+08:00",
    })
    assert seeded.status_code == 201
    # Seed through the real API before setting the clock back.
    monkeypatch.setattr(consolidation, "utc_now", lambda: datetime(2030, 9, 16, 23, 30, tzinfo=UTC))
    response = harness.client.get(f"/api/v1/portfolio-groups/{group}/summary",
                                  params={"report_timezone": "Asia/Taipei"})
    assert response.status_code == 200
    assert response.json()["as_of"] == "2030-09-17"
    assert response.json()["cash_value"] == "1000"
    assert response.json()["report_timezone"] == "Asia/Taipei"


def test_snapshots_isolate_timezone_and_local_midnight(harness):
    portfolio, group = group_for(harness)
    seeded = harness.client.post(f"/api/v1/portfolios/{portfolio}/transactions", json={
        "request_id": "boundary", "transaction_type": "deposit", "amount": "1000",
        "occurred_at": "2020-09-17T01:00:00+08:00",
    })
    assert seeded.status_code == 201
    endpoint = f"/api/v1/portfolios/{portfolio}/valuation-snapshots"
    utc = harness.client.post(endpoint, json={"valuation_date": "2020-09-16"})
    taipei = harness.client.post(endpoint, json={
        "valuation_date": "2020-09-16", "report_timezone": "Asia/Taipei",
    })
    assert utc.status_code == taipei.status_code == 201
    assert utc.json()["cash_value"] == "1000"
    assert taipei.json()["cash_value"] == "0"
    assert utc.json()["id"] != taipei.json()["id"]
    history = harness.client.get(f"/api/v1/portfolio-groups/{group}/nav-history", params={
        "start_date": "2020-09-16", "end_date": "2020-09-16",
        "report_timezone": "Asia/Taipei",
    }).json()
    assert history["report_timezone"] == "Asia/Taipei"
    assert history["points"][0]["net_value"] == "0"


def test_current_summary_excludes_later_transactions(harness, monkeypatch):
    portfolio, group = group_for(harness)
    monkeypatch.setattr(api, "utc_now", lambda: datetime(2031, 1, 1, tzinfo=UTC))
    for request_id, when, amount in [
        ("before", "2030-09-16T23:00:00Z", "1000"),
        ("after", "2030-09-16T23:45:00Z", "2000"),
    ]:
        response = harness.client.post(f"/api/v1/portfolios/{portfolio}/transactions", json={
            "request_id": request_id, "transaction_type": "deposit", "amount": amount,
            "occurred_at": when,
        })
        assert response.status_code == 201
    monkeypatch.setattr(consolidation, "utc_now", lambda: datetime(2030, 9, 16, 23, 30, tzinfo=UTC))
    result = harness.client.get(f"/api/v1/portfolio-groups/{group}/summary").json()
    assert result["cash_value"] == "1000"


def test_taipei_snapshot_does_not_use_a_later_us_close(harness):
    portfolio = harness.portfolio("US close", "USD")
    base = f"/api/v1/portfolios/{portfolio}"
    for request_id, kind, fields in [
        ("deposit", "deposit", {"amount": "1000"}),
        ("buy", "buy", {"ticker": "AAPL", "quantity": "1", "unit_price": "100"}),
    ]:
        response = harness.client.post(base + "/transactions", json={
            "request_id": request_id, "transaction_type": kind,
            "occurred_at": "2020-09-15T00:00:00Z", **fields,
        })
        assert response.status_code == 201
    snapshot = harness.client.post(base + "/valuation-snapshots", json={
        "valuation_date": "2020-09-16", "report_timezone": "Asia/Taipei",
    }).json()
    # Taipei midnight precedes the September 16 US close.
    assert snapshot["positions"][0]["price_as_of"].startswith("2020-09-15")


def test_invalid_report_timezone_is_machine_readable(harness):
    _, group = group_for(harness)
    response = harness.client.get(f"/api/v1/portfolio-groups/{group}/summary",
                                  params={"report_timezone": "Not/AZone"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_report_timezone"
