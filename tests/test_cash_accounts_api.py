"""HTTP surface for cash accounts and transfers."""

from decimal import Decimal


def create_cash_account(harness, name="Cathay savings", currency="TWD", institution="Cathay"):
    response = harness.client.post(
        "/api/v1/cash-accounts",
        json={"name": name, "base_currency": currency, "institution": institution},
    )
    assert response.status_code == 201, response.text
    return response.json()


def deposit(harness, portfolio_id, amount, request_id="d-1"):
    response = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": request_id, "transaction_type": "deposit", "amount": amount},
    )
    assert response.status_code == 201, response.text


def test_creating_a_cash_account_records_its_kind_and_institution(harness) -> None:
    body = create_cash_account(harness)

    assert body["kind"] == "cash"
    assert body["institution"] == "Cathay"
    assert body["base_currency"] == "TWD"


def test_an_ordinary_portfolio_reads_as_an_investment_account(harness) -> None:
    """The added fields must have safe defaults for callers that never set them."""
    portfolio_id = harness.portfolio()

    body = harness.client.get(f"/api/v1/portfolios/{portfolio_id}").json()

    assert body["kind"] == "investment"
    assert body["institution"] is None


def test_listing_cash_accounts_excludes_investment_portfolios(harness) -> None:
    harness.portfolio("Broker", "USD")
    account = create_cash_account(harness)

    listed = harness.client.get("/api/v1/cash-accounts").json()

    assert [item["id"] for item in listed] == [account["id"]]


def test_cash_accounts_still_appear_in_list_portfolios(harness) -> None:
    """A cash account is a portfolio; the discovery endpoint must not hide it."""
    harness.portfolio("Broker", "USD")
    account = create_cash_account(harness)

    listed = harness.client.get("/api/v1/portfolios").json()

    assert account["id"] in [item["id"] for item in listed]
    kinds = {item["name"]: item["kind"] for item in listed}
    assert kinds == {"Broker": "investment", "Cathay savings": "cash"}


def test_a_cash_account_rejects_a_buy_over_http(harness) -> None:
    account = create_cash_account(harness, currency="USD")
    harness.client.get("/api/v1/market/instruments/AAPL")
    deposit(harness, account["id"], "10000")

    response = harness.client.post(
        f"/api/v1/portfolios/{account['id']}/transactions",
        json={
            "request_id": "buy-1",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "140",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "not_a_securities_account"


def test_a_transfer_moves_money_and_reports_both_sides(harness) -> None:
    bank = create_cash_account(harness, currency="USD")
    broker = harness.portfolio("Broker", "USD")
    deposit(harness, bank["id"], "10000")

    response = harness.client.post(
        "/api/v1/transfers",
        json={
            "request_id": "t-1",
            "from_portfolio_id": bank["id"],
            "to_portfolio_id": broker,
            "amount": "2500",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert Decimal(body["sent"]["amount"]) == Decimal("-2500")
    assert Decimal(body["received"]["amount"]) == Decimal("2500")
    assert body["sent"]["portfolio_id"] == bank["id"]
    assert body["received"]["portfolio_id"] == broker
    assert body["fx_rate"] is None, "same currency needs no rate"
    assert body["status"] == "posted"


def test_a_cross_currency_transfer_reports_its_rate(harness) -> None:
    bank = create_cash_account(harness, currency="TWD")
    broker = harness.portfolio("US broker", "USD")
    deposit(harness, bank["id"], "320000")

    body = harness.client.post(
        "/api/v1/transfers",
        json={
            "request_id": "t-1",
            "from_portfolio_id": bank["id"],
            "to_portfolio_id": broker,
            "amount": "32000",
            "fx_rate": "0.03125",
        },
    ).json()

    assert Decimal(body["sent"]["amount"]) == Decimal("-32000")
    assert body["sent"]["currency"] == "TWD"
    assert Decimal(body["received"]["amount"]) == Decimal("1000")
    assert body["received"]["currency"] == "USD"
    assert Decimal(body["fx_rate"]) == Decimal("0.03125")


def test_a_cross_currency_transfer_without_a_rate_is_refused(harness) -> None:
    bank = create_cash_account(harness, currency="TWD")
    broker = harness.portfolio("US broker", "USD")
    deposit(harness, bank["id"], "320000")

    response = harness.client.post(
        "/api/v1/transfers",
        json={
            "request_id": "t-1",
            "from_portfolio_id": bank["id"],
            "to_portfolio_id": broker,
            "amount": "32000",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "fx_rate_required"


def test_reading_a_transfer_returns_both_halves(harness) -> None:
    bank = create_cash_account(harness, currency="USD")
    broker = harness.portfolio("Broker", "USD")
    deposit(harness, bank["id"], "10000")
    created = harness.client.post(
        "/api/v1/transfers",
        json={
            "request_id": "t-1",
            "from_portfolio_id": bank["id"],
            "to_portfolio_id": broker,
            "amount": "2500",
        },
    ).json()

    body = harness.client.get(f"/api/v1/transfers/{created['transfer_id']}").json()

    assert body["transfer_id"] == created["transfer_id"]
    assert body["sent"]["event_id"] == created["sent"]["event_id"]
    assert body["received"]["event_id"] == created["received"]["event_id"]


def test_reading_an_unknown_transfer_is_404(harness) -> None:
    response = harness.client.get("/api/v1/transfers/no-such-id")

    assert response.status_code == 404
    assert response.json()["code"] == "transfer_not_found"


def test_reversing_a_transfer_restores_both_balances(harness) -> None:
    bank = create_cash_account(harness, currency="USD")
    broker = harness.portfolio("Broker", "USD")
    deposit(harness, bank["id"], "10000")
    created = harness.client.post(
        "/api/v1/transfers",
        json={
            "request_id": "t-1",
            "from_portfolio_id": bank["id"],
            "to_portfolio_id": broker,
            "amount": "2500",
        },
    ).json()

    response = harness.client.post(
        f"/api/v1/transfers/{created['transfer_id']}/reversal",
        json={"request_id": "rev-1"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "reversed"
    bank_summary = harness.client.get(f"/api/v1/portfolios/{bank['id']}/summary").json()
    broker_summary = harness.client.get(f"/api/v1/portfolios/{broker}/summary").json()
    assert Decimal(bank_summary["cash_value"]) == Decimal("10000")
    assert Decimal(broker_summary["cash_value"]) == Decimal("0")


def test_reversing_half_a_transfer_through_the_event_endpoint_is_refused(harness) -> None:
    bank = create_cash_account(harness, currency="USD")
    broker = harness.portfolio("Broker", "USD")
    deposit(harness, bank["id"], "10000")
    created = harness.client.post(
        "/api/v1/transfers",
        json={
            "request_id": "t-1",
            "from_portfolio_id": bank["id"],
            "to_portfolio_id": broker,
            "amount": "2500",
        },
    ).json()

    response = harness.client.post(
        f"/api/v1/portfolios/{bank['id']}/transactions/"
        f"{created['sent']['event_id']}/reversal",
        json={"request_id": "rev-half"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "reverse_the_transfer_instead"
