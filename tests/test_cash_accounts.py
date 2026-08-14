"""Cash accounts: a portfolio that holds money and never a position.

The guardrails are the point of these tests. A cash account reuses the journal, the replay, and
the valuation machinery unchanged, so what needs proving is that nothing can put a security into
one, and that the shared machinery does the right thing when there are no positions at all.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventType, PortfolioKind
from portfolio_manager.models import CashBalance, Portfolio
from portfolio_manager.postings import TransactionRequest, record_transaction
from portfolio_manager.valuation import create_snapshot

# The valuation guard compares against the UTC date, so `date.today()` drifts a day ahead east of
# Greenwich between local midnight and UTC's, which can push a valuation date into the future.
TODAY = datetime.now(UTC).date()


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def cash_of(session, portfolio_id: str) -> Decimal:
    session.expire_all()
    balance = session.get(CashBalance, portfolio_id)
    return balance.amount if balance else Decimal("0")


def deposit(session, portfolio_id: str, amount: str, request_id: str = "seed") -> None:
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id=request_id,
            event_type=EventType.DEPOSIT,
            amount=Decimal(amount),
        ),
    )


def test_a_new_portfolio_is_an_investment_account(harness, session) -> None:
    """The default must be what every pre-existing portfolio already was."""
    portfolio_id = harness.portfolio()

    portfolio = session.get(Portfolio, portfolio_id)
    assert portfolio.kind == PortfolioKind.INVESTMENT.value
    assert portfolio.institution is None


def test_a_cash_account_rejects_a_buy(harness, session) -> None:
    account_id = harness.cash_account()
    harness.client.get("/api/v1/market/instruments/AAPL")
    deposit(session, account_id, "10000")

    with pytest.raises(DomainError) as excinfo:
        record_transaction(
            session,
            account_id,
            TransactionRequest(
                request_id="buy-1",
                event_type=EventType.BUY,
                ticker="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("140"),
            ),
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "not_a_securities_account"
    assert cash_of(session, account_id) == Decimal("10000"), "the rejection moved no money"


def test_a_cash_account_rejects_a_transaction_carrying_a_ticker(harness, session) -> None:
    """A ticker on an otherwise-legal event would attach an instrument to the leg."""
    account_id = harness.cash_account()
    harness.client.get("/api/v1/market/instruments/AAPL")

    with pytest.raises(DomainError) as excinfo:
        record_transaction(
            session,
            account_id,
            TransactionRequest(
                request_id="interest-1",
                event_type=EventType.INTEREST,
                amount=Decimal("12.50"),
                ticker="AAPL",
            ),
        )

    assert excinfo.value.code == "not_a_securities_account"


def test_a_cash_account_rejects_a_dividend(harness, session) -> None:
    """A dividend is paid by a holding, and a cash account has none."""
    account_id = harness.cash_account()

    with pytest.raises(DomainError) as excinfo:
        record_transaction(
            session,
            account_id,
            TransactionRequest(
                request_id="div-1",
                event_type=EventType.DIVIDEND,
                amount=Decimal("40"),
            ),
        )

    assert excinfo.value.code == "not_a_securities_account"


def test_a_cash_account_accepts_deposit_withdrawal_interest_and_fee(harness, session) -> None:
    account_id = harness.cash_account()

    deposit(session, account_id, "10000", "d-1")
    for request_id, event_type, amount in (
        ("i-1", EventType.INTEREST, "12.50"),
        ("f-1", EventType.FEE, "2.00"),
        ("w-1", EventType.WITHDRAWAL, "500"),
    ):
        record_transaction(
            session,
            account_id,
            TransactionRequest(
                request_id=request_id, event_type=event_type, amount=Decimal(amount)
            ),
        )

    # 10000 + 12.50 - 2.00 - 500
    assert cash_of(session, account_id) == Decimal("9510.50")


def test_an_investment_portfolio_still_holds_securities(harness, session) -> None:
    """Regression: the guard must not touch the accounts it does not apply to."""
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    deposit(session, portfolio_id, "10000")

    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id="buy-1",
            event_type=EventType.BUY,
            ticker="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("140"),
        ),
    )

    assert cash_of(session, portfolio_id) == Decimal("8600")


def test_an_investment_portfolio_may_hold_only_cash(harness, session) -> None:
    """A brokerage account holding nothing but uninvested cash is ordinary, not an error."""
    portfolio_id = harness.portfolio()
    deposit(session, portfolio_id, "5000")

    assert cash_of(session, portfolio_id) == Decimal("5000")


def test_a_cash_account_snapshots_as_complete(harness, session) -> None:
    """No positions means nothing could fail to price, so the snapshot is not partial.

    A cash account would otherwise carry a permanent `partial` flag that no user could ever
    clear, which is exactly the warning that teaches readers to ignore warnings.
    """
    account_id = harness.cash_account()
    valuation_day = TODAY - timedelta(days=1)
    record_transaction(
        session,
        account_id,
        TransactionRequest(
            request_id="d-1",
            event_type=EventType.DEPOSIT,
            amount=Decimal("7500"),
            # Before the valuation date: a snapshot must never see a later event.
            occurred_at=datetime.combine(valuation_day, time(9, 0), tzinfo=UTC),
        ),
    )

    snapshot = create_snapshot(session, account_id, valuation_day, harness.provider)

    assert snapshot.positions_total == 0
    assert snapshot.positions_priced == 0
    assert snapshot.status == "complete"
    assert snapshot.pricing_coverage_percent == Decimal("100")
    assert snapshot.cash_value == Decimal("7500")
    assert snapshot.total_value == Decimal("7500")
    assert snapshot.securities_value == Decimal("0")


def test_an_empty_cash_account_summarizes_without_dividing_by_zero(harness) -> None:
    account_id = harness.cash_account()

    response = harness.client.get(f"/api/v1/portfolios/{account_id}/summary")

    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["cash_value"]) == Decimal("0")
    assert Decimal(body["total_value"]) == Decimal("0")
    assert body["positions"] == []
    assert body["cash"]["weight_percent"] is None, "no total means no weight, not a zero weight"
