"""Liability accounts: a book whose balance is money owed rather than money held.

A loan needed almost no new machinery, because the journal was always signed -- cash legs carry a
direction and every total is a plain sum, so a negative balance flows through replay, valuation,
and consolidation arithmetically. What needed building was permission for that balance to exist
in one kind of book and nowhere else, and a refusal to report a return on it.

So these tests are mostly about the boundary. The guard that lets a loan run negative must not
have loosened the same guard for an investment or cash account, and the split of a group total
into assets and liabilities must agree with the total it came from.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventType, PortfolioKind
from portfolio_manager.models import CashBalance, Portfolio
from portfolio_manager.performance import calculate_performance
from portfolio_manager.postings import TransactionRequest, record_transaction
from portfolio_manager.transfers import transfer_cash
from portfolio_manager.valuation import create_snapshot

ZERO = Decimal("0")

# The valuation guard compares against the UTC date, so `date.today()` fails east of Greenwich
# for the hours after local midnight but before UTC's -- a snapshot for the local today looks
# future-dated and is refused. Every other suite uses the UTC date for the same reason.
TODAY = datetime.now(UTC).date()


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def cash_of(session, portfolio_id: str) -> Decimal:
    session.expire_all()
    balance = session.get(CashBalance, portfolio_id)
    return balance.amount if balance else ZERO


def post(session, portfolio_id: str, event_type: EventType, amount: str, request_id: str) -> None:
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id=request_id,
            event_type=event_type,
            amount=Decimal(amount),
        ),
    )


def test_a_liability_account_is_created_with_the_liability_kind(harness, session) -> None:
    account_id = harness.liability_account(institution="Cathay United Bank")

    portfolio = session.get(Portfolio, account_id)
    assert portfolio.kind == PortfolioKind.LIABILITY.value
    assert portfolio.institution == "Cathay United Bank"


def test_a_liability_balance_may_go_negative(harness, session) -> None:
    """The whole point: a loan is negative from its first day to its last."""
    loan_id = harness.liability_account()

    post(session, loan_id, EventType.WITHDRAWAL, "1000000", "drawdown")

    assert cash_of(session, loan_id) == Decimal("-1000000")


def test_an_investment_account_still_refuses_to_go_negative(harness, session) -> None:
    """The regression that matters most: the overdraft guard was narrowed, not removed."""
    portfolio_id = harness.portfolio()

    with pytest.raises(DomainError) as excinfo:
        post(session, portfolio_id, EventType.WITHDRAWAL, "100", "overdraw")

    assert excinfo.value.code == "insufficient_cash"


def test_a_cash_account_still_refuses_to_go_negative(harness, session) -> None:
    account_id = harness.cash_account()

    with pytest.raises(DomainError) as excinfo:
        post(session, account_id, EventType.WITHDRAWAL, "100", "overdraw")

    assert excinfo.value.code == "insufficient_cash"


def test_a_liability_account_rejects_a_buy(harness, session) -> None:
    """A loan holds no position, and the guard it shares with cash accounts must see it."""
    loan_id = harness.liability_account()
    harness.client.get("/api/v1/market/instruments/AAPL")

    with pytest.raises(DomainError) as excinfo:
        record_transaction(
            session,
            loan_id,
            TransactionRequest(
                request_id="buy",
                event_type=EventType.BUY,
                ticker="AAPL",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
            ),
        )

    assert excinfo.value.code == "not_a_securities_account"
    assert "loan" in excinfo.value.message


def test_a_drawdown_moves_the_debt_to_the_bank_account(harness, session) -> None:
    """A disbursement is an ordinary transfer; the loan side is simply allowed to go negative."""
    loan_id = harness.liability_account("Credit loan", "USD")
    bank_id = harness.cash_account("Bank", "USD")

    transfer_cash(session, loan_id, bank_id, "drawdown", Decimal("1000000"))

    assert cash_of(session, loan_id) == Decimal("-1000000")
    assert cash_of(session, bank_id) == Decimal("1000000")
    # Nothing was created or destroyed: the pair nets to zero.
    assert cash_of(session, loan_id) + cash_of(session, bank_id) == ZERO


def test_a_repayment_moves_the_balance_toward_zero(harness, session) -> None:
    loan_id = harness.liability_account("Credit loan", "USD")
    bank_id = harness.cash_account("Bank", "USD")
    transfer_cash(session, loan_id, bank_id, "drawdown", Decimal("1000000"))

    transfer_cash(session, bank_id, loan_id, "repay-1", Decimal("50000"))

    assert cash_of(session, loan_id) == Decimal("-950000")
    assert cash_of(session, bank_id) == Decimal("950000")


def test_interest_charged_is_recorded_as_a_fee(harness, session) -> None:
    """`interest` credits cash, so a charge must be a fee -- it deepens the debt, not repays it."""
    loan_id = harness.liability_account()
    post(session, loan_id, EventType.WITHDRAWAL, "1000000", "drawdown")

    post(session, loan_id, EventType.FEE, "4000", "interest-jan")

    assert cash_of(session, loan_id) == Decimal("-1004000")


def test_a_liability_snapshot_is_complete_and_negative(harness, session) -> None:
    """No positions means nothing to price, so the snapshot is complete rather than partial."""
    loan_id = harness.liability_account()
    occurred = datetime.combine(TODAY - timedelta(days=2), time(12, 0), tzinfo=UTC)
    record_transaction(
        session,
        loan_id,
        TransactionRequest(
            request_id="drawdown",
            event_type=EventType.WITHDRAWAL,
            amount=Decimal("1000000"),
            occurred_at=occurred,
        ),
    )

    snapshot = create_snapshot(session, loan_id, TODAY, harness.provider)

    assert snapshot.status == "complete"
    assert snapshot.cash_value == Decimal("-1000000")
    assert snapshot.total_value == Decimal("-1000000")


def test_a_liability_account_reports_no_return(harness, session) -> None:
    """A repayment is not a gain, so the honest answer is no rate at all -- with the reason."""
    loan_id = harness.liability_account()
    post(session, loan_id, EventType.WITHDRAWAL, "1000000", "drawdown")

    result = calculate_performance(session, loan_id, TODAY - timedelta(days=30), TODAY)

    assert result.twr_percent is None
    assert result.xirr_percent is None
    assert "liability" in result.xirr_unavailable_reason.lower()


def test_a_group_splits_its_total_into_assets_and_liabilities(harness, session) -> None:
    """The three figures must reconcile, or a reader cannot trust either half."""
    loan_id = harness.liability_account("Credit loan", "USD")
    bank_id = harness.cash_account("Bank", "USD")
    transfer_cash(session, loan_id, bank_id, "drawdown", Decimal("1000000"))
    post(session, bank_id, EventType.DEPOSIT, "500000", "salary")

    response = harness.client.post(
        "/api/v1/portfolio-groups",
        json={
            "name": "Everything",
            "reporting_currency": "USD",
            "portfolio_ids": [loan_id, bank_id],
        },
    )
    assert response.status_code == 201, response.text
    group_id = response.json()["id"]

    summary = harness.client.get(
        f"/api/v1/portfolio-groups/{group_id}/summary",
        params={"reporting_currency": "USD"},
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()

    assert Decimal(body["assets_value"]) == Decimal("1500000")
    assert Decimal(body["liabilities_value"]) == Decimal("-1000000")
    assert Decimal(body["net_value"]) == Decimal("500000")
    # net_value is the same number total_value always was, not a second opinion on it.
    assert Decimal(body["net_value"]) == Decimal(body["total_value"])
    assert (
        Decimal(body["assets_value"]) + Decimal(body["liabilities_value"])
        == Decimal(body["net_value"])
    )


def test_a_group_without_debt_reports_zero_liabilities(harness, session) -> None:
    """The split must not disturb the ordinary case it was added alongside."""
    bank_id = harness.cash_account("Bank", "USD")
    post(session, bank_id, EventType.DEPOSIT, "500000", "salary")

    response = harness.client.post(
        "/api/v1/portfolio-groups",
        json={
            "name": "Assets only",
            "reporting_currency": "USD",
            "portfolio_ids": [bank_id],
        },
    )
    assert response.status_code == 201, response.text
    group_id = response.json()["id"]

    body = harness.client.get(
        f"/api/v1/portfolio-groups/{group_id}/summary",
        params={"reporting_currency": "USD"},
    ).json()

    assert Decimal(body["liabilities_value"]) == ZERO
    assert Decimal(body["assets_value"]) == Decimal(body["net_value"])


def test_liability_accounts_are_listed_separately(harness) -> None:
    loan_id = harness.liability_account("Credit loan", "USD")
    harness.cash_account("Bank", "USD")

    listed = harness.client.get("/api/v1/liability-accounts")

    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [loan_id]
    assert listed.json()[0]["kind"] == "liability"
