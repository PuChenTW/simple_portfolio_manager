"""Journal endpoints driven over HTTP, including the decimal-string contract.

Monetary fields are compared as `Decimal`, not as text: the API guarantees an exact value carried
as a string, not a particular trailing-zero scale, so "0.00" and "0" are both correct for zero.
"""

from decimal import Decimal


def deposit(harness, portfolio_id: str, amount: str = "10000", request_id: str = "d-1"):
    return harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": request_id, "transaction_type": "deposit", "amount": amount},
    )


def buy(harness, portfolio_id: str, request_id: str = "b-1"):
    return harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": request_id,
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "140",
            "fee": "1.50",
            "tax": "0.25",
            "source_reference": "BROKER-8842",
        },
    )


def test_buy_returns_balanced_legs_as_decimal_strings(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    response = buy(harness, portfolio_id)
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["balance"]["balanced"] is True
    assert Decimal(body["balance"]["residual"]) == Decimal("0")
    assert body["flow_classification"] == "internal"

    legs = {leg["leg_type"]: leg for leg in body["legs"]}
    assert Decimal(legs["security"]["quantity_delta"]) == Decimal("10")
    # Costs capitalize into basis: 10 * 140 + 1.50 + 0.25
    assert Decimal(legs["security"]["amount_delta"]) == Decimal("1401.75")
    assert Decimal(legs["cash"]["amount_delta"]) == Decimal("-1401.75")
    assert isinstance(legs["cash"]["amount_delta"], str), "money must serialize as a string"


def test_summary_reflects_the_journal_posting(harness) -> None:
    """Positions and cash are projections of the journal; both must move on one call."""
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    buy(harness, portfolio_id)

    summary = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    assert Decimal(summary["cash_value"]) == Decimal("8598.25")
    position = summary["positions"][0]
    assert position["ticker"] == "AAPL"
    assert Decimal(position["quantity"]) == Decimal("10")
    assert Decimal(position["average_cost"]) == Decimal("140.175")


def test_deposit_is_external_and_dividend_is_internal(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit_body = deposit(harness, portfolio_id).json()
    assert deposit_body["flow_classification"] == "external"

    harness.client.get("/api/v1/market/instruments/AAPL")
    dividend = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "div-1",
            "transaction_type": "dividend",
            "ticker": "AAPL",
            "amount": "100",
            "tax": "30",
        },
    )
    assert dividend.status_code == 201, dividend.text
    body = dividend.json()
    assert body["flow_classification"] == "internal"

    legs = {leg["leg_type"]: leg["amount_delta"] for leg in body["legs"]}
    assert Decimal(legs["income"]) == Decimal("-100")
    assert Decimal(legs["tax"]) == Decimal("30")
    assert Decimal(legs["cash"]) == Decimal("70")


def test_reversal_is_visible_in_both_directions(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    original_id = buy(harness, portfolio_id).json()["event"]["id"]

    reversal = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/{original_id}/reversal",
        json={"request_id": "rev-1", "memo": "booked to the wrong account"},
    )
    assert reversal.status_code == 201, reversal.text
    reversal_id = reversal.json()["event"]["id"]
    assert reversal.json()["reverses_event_id"] == original_id

    original = harness.client.get(
        f"/api/v1/portfolios/{portfolio_id}/transactions/{original_id}"
    ).json()
    assert original["event"]["status"] == "reversed"
    assert original["reversed_by_event_id"] == reversal_id

    summary = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    assert Decimal(summary["cash_value"]) == Decimal("10000")
    assert summary["positions"] == [], "a fully reversed buy leaves no open position"


def test_double_reversal_is_rejected(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    event_id = buy(harness, portfolio_id).json()["event"]["id"]

    first = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/{event_id}/reversal",
        json={"request_id": "rev-a"},
    )
    assert first.status_code == 201
    second = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions/{event_id}/reversal",
        json={"request_id": "rev-b"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "already_reversed"


def test_idempotency_conflict_surfaces_as_409(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    assert buy(harness, portfolio_id, "same-id").status_code == 201

    conflicting = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "same-id",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "999",
            "unit_price": "140",
        },
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "idempotency_conflict"


def test_overdraft_is_rejected_before_anything_is_written(harness) -> None:
    portfolio_id = harness.portfolio()
    response = buy(harness, portfolio_id)  # no cash deposited
    assert response.status_code == 422
    assert response.json()["code"] == "insufficient_cash"

    listed = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json()
    assert listed["total"] == 0, "a rejected transaction must not appear in the ledger"


def test_journal_filters_by_type_instrument_and_source_reference(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    buy(harness, portfolio_id)

    base = f"/api/v1/portfolios/{portfolio_id}/transactions"
    assert harness.client.get(base).json()["total"] == 2
    assert harness.client.get(f"{base}?event_type=buy").json()["total"] == 1
    assert harness.client.get(f"{base}?ticker=AAPL").json()["total"] == 1
    assert harness.client.get(f"{base}?source_reference=BROKER-8842").json()["total"] == 1
    assert harness.client.get(f"{base}?source_reference=NOPE").json()["total"] == 0


def test_reversal_is_classified_by_the_event_it_undoes(harness) -> None:
    """`reversal` says nothing on its own; only the event it undoes decides the flow category.

    Reporting `unknown` here badges a fully recorded correction as a data-quality problem the
    reader can never clear, which is how the warnings that do matter get ignored.
    """
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    base = f"/api/v1/portfolios/{portfolio_id}/transactions"

    deposit_id = deposit(harness, portfolio_id, amount="5000", request_id="d-2").json()["event"][
        "id"
    ]
    harness.client.post(f"{base}/{deposit_id}/reversal", json={"request_id": "rev-deposit"})
    buy_id = buy(harness, portfolio_id).json()["event"]["id"]
    harness.client.post(f"{base}/{buy_id}/reversal", json={"request_id": "rev-buy"})

    listed = harness.client.get(base).json()["items"]
    flows = {
        event["reverses_event_id"]: event["flow_classification"]
        for event in listed
        if event["reverses_event_id"]
    }
    assert flows[deposit_id] == "external", "reversing a deposit still moves investor capital"
    assert flows[buy_id] == "internal", "reversing a buy is portfolio activity, not capital"

    # The detail endpoint must not disagree with the page it was reached from.
    for event in listed:
        detail = harness.client.get(f"{base}/{event['id']}").json()
        assert detail["flow_classification"] == event["flow_classification"]
        assert detail["event"]["flow_classification"] == event["flow_classification"]


def test_resolving_reversals_costs_no_query_per_event(harness) -> None:
    from sqlalchemy import event as sa_event

    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id, amount="100000")
    base = f"/api/v1/portfolios/{portfolio_id}/transactions"

    def count_queries() -> int:
        statements = []
        engine = harness.session_factory.kw["bind"]

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        sa_event.listen(engine, "before_cursor_execute", record)
        try:
            assert harness.client.get(f"{base}?limit=200").status_code == 200
        finally:
            sa_event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    def reverse_a_buy(index: int) -> None:
        event_id = buy(harness, portfolio_id, request_id=f"rb-{index}").json()["event"]["id"]
        harness.client.post(f"{base}/{event_id}/reversal", json={"request_id": f"rr-{index}"})

    reverse_a_buy(0)
    small = count_queries()
    for index in range(1, 8):
        reverse_a_buy(index)
    large = count_queries()

    assert small == large, f"query count grew from {small} to {large} as reversals were added"


def test_legs_are_absent_unless_requested(harness) -> None:
    """The default response must stay exactly what callers built against before `include_legs`."""
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    buy(harness, portfolio_id)

    listed = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/transactions").json()
    assert [event["legs"] for event in listed["items"]] == [None, None]


def test_include_legs_matches_the_detail_endpoint(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    buy(harness, portfolio_id)

    base = f"/api/v1/portfolios/{portfolio_id}/transactions"
    for event in harness.client.get(f"{base}?include_legs=true").json()["items"]:
        detail = harness.client.get(f"{base}/{event['id']}").json()
        assert event["legs"] == detail["legs"], "inline legs must not differ from the detail view"


def test_include_legs_resolves_the_ticker_and_leaves_cash_legs_unnamed(harness) -> None:
    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id)
    buy(harness, portfolio_id)

    base = f"/api/v1/portfolios/{portfolio_id}/transactions"
    events = harness.client.get(f"{base}?event_type=buy&include_legs=true").json()["items"]
    legs = {leg["leg_type"]: leg for leg in events[0]["legs"]}

    security = legs["security"]
    assert security["ticker"] == "AAPL", "an opaque instrument_id is unreadable on its own"
    assert Decimal(security["quantity_delta"]) == Decimal("10")
    assert Decimal(security["unit_price"]) == Decimal("140")

    # A cash leg names no instrument, so it reports no ticker rather than borrowing the trade's.
    assert legs["cash"]["ticker"] is None
    assert legs["cash"]["quantity_delta"] is None


def test_include_legs_cost_does_not_grow_with_the_number_of_events(harness) -> None:
    """The whole point of the flag: one page costs the same whether it holds 2 events or 12."""
    from sqlalchemy import event as sa_event

    portfolio_id = harness.portfolio()
    deposit(harness, portfolio_id, amount="100000")
    base = f"/api/v1/portfolios/{portfolio_id}/transactions"

    def count_queries() -> int:
        statements = []
        engine = harness.session_factory.kw["bind"]

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        sa_event.listen(engine, "before_cursor_execute", record)
        try:
            assert harness.client.get(f"{base}?include_legs=true&limit=200").status_code == 200
        finally:
            sa_event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    buy(harness, portfolio_id, request_id="b-first")
    small = count_queries()

    for index in range(10):
        buy(harness, portfolio_id, request_id=f"b-{index}")
    large = count_queries()

    assert harness.client.get(f"{base}").json()["total"] == 12, "the page really did grow"
    assert small == large, f"query count grew from {small} to {large} as events were added"


def test_missing_required_field_names_the_field(harness) -> None:
    portfolio_id = harness.portfolio()
    response = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "bad-1", "transaction_type": "buy", "ticker": "AAPL"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "missing_field"
    assert response.json()["details"]["field"] == "quantity"
