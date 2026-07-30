"""Atomic journal posting: legs, projections, idempotency, and reversal.

These tests exercise the service against a real session so the transaction boundary itself is
under test, not just leg arithmetic.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventStatus, EventType, LegType
from portfolio_manager.models import CashBalance, JournalEvent, JournalLeg, Position
from portfolio_manager.postings import (
    TransactionRequest,
    record_transaction,
    reverse_transaction,
)


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


@pytest.fixture
def funded_portfolio(harness, session) -> str:
    """A portfolio holding 10000 USD, established through the journal itself."""
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")  # resolve instrument identity
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id="seed-cash",
            event_type=EventType.DEPOSIT,
            amount=Decimal("10000"),
        ),
    )
    return portfolio_id


def cash_of(session, portfolio_id: str) -> Decimal:
    session.expire_all()
    balance = session.get(CashBalance, portfolio_id)
    return balance.amount if balance else Decimal("0")


def position_of(session, portfolio_id: str, ticker: str) -> Position | None:
    session.expire_all()
    return session.get(Position, (portfolio_id, ticker))


def buy_request(request_id: str = "buy-1") -> TransactionRequest:
    return TransactionRequest(
        request_id=request_id,
        event_type=EventType.BUY,
        ticker="AAPL",
        quantity=Decimal("10"),
        unit_price=Decimal("140"),
        fee=Decimal("1.50"),
        tax=Decimal("0.25"),
    )


def test_buy_moves_position_and_cash_in_one_event(session, funded_portfolio) -> None:
    event = record_transaction(session, funded_portfolio, buy_request())

    assert event.event_type == EventType.BUY.value
    assert cash_of(session, funded_portfolio) == Decimal("8598.25")  # 10000 - 1400 - 1.75

    position = position_of(session, funded_portfolio, "AAPL")
    assert position.quantity == Decimal("10")
    # Costs capitalize into the position: (1400 + 1.75) / 10
    assert position.average_cost == Decimal("140.175")

    legs = session.scalars(select(JournalLeg).where(JournalLeg.event_id == event.id)).all()
    assert {leg.leg_type for leg in legs} == {
        LegType.SECURITY.value,
        LegType.FEE.value,
        LegType.TAX.value,
        LegType.CASH.value,
    }


def test_failed_posting_leaves_no_legs_position_or_cash_change(session, funded_portfolio) -> None:
    """The whole point of the journal: a rejected transaction must leave nothing behind."""
    before_cash = cash_of(session, funded_portfolio)

    with pytest.raises(DomainError) as excinfo:
        record_transaction(
            session,
            funded_portfolio,
            TransactionRequest(
                request_id="oversell-1",
                event_type=EventType.SELL,
                ticker="AAPL",
                quantity=Decimal("5"),  # nothing is held yet
                unit_price=Decimal("150"),
            ),
        )
    assert excinfo.value.code == "insufficient_position"

    session.expire_all()
    assert cash_of(session, funded_portfolio) == before_cash
    assert position_of(session, funded_portfolio, "AAPL") is None
    events = session.scalars(
        select(JournalEvent).where(JournalEvent.request_id == "oversell-1")
    ).all()
    assert events == [], "a rejected transaction must not leave an event behind"


def test_overdraft_is_rejected_and_rolls_back(session, harness) -> None:
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")

    with pytest.raises(DomainError) as excinfo:
        record_transaction(session, portfolio_id, buy_request("broke-1"))
    assert excinfo.value.code == "insufficient_cash"

    session.expire_all()
    assert cash_of(session, portfolio_id) == Decimal("0")
    assert position_of(session, portfolio_id, "AAPL") is None


def test_repeating_a_request_id_does_not_post_twice(session, funded_portfolio) -> None:
    first = record_transaction(session, funded_portfolio, buy_request("dup-1"))
    second = record_transaction(session, funded_portfolio, buy_request("dup-1"))

    assert first.id == second.id
    assert cash_of(session, funded_portfolio) == Decimal("8598.25")
    assert position_of(session, funded_portfolio, "AAPL").quantity == Decimal("10")


def test_reusing_a_request_id_with_different_data_is_a_conflict(session, funded_portfolio) -> None:
    record_transaction(session, funded_portfolio, buy_request("conflict-1"))
    changed = buy_request("conflict-1")
    changed = TransactionRequest(**{**changed.__dict__, "quantity": Decimal("99")})

    with pytest.raises(DomainError) as excinfo:
        record_transaction(session, funded_portfolio, changed)
    assert excinfo.value.code == "idempotency_conflict"


def test_sell_realizes_pnl_against_average_cost(session, funded_portfolio) -> None:
    record_transaction(session, funded_portfolio, buy_request("buy-for-sell"))
    record_transaction(
        session,
        funded_portfolio,
        TransactionRequest(
            request_id="sell-1",
            event_type=EventType.SELL,
            ticker="AAPL",
            quantity=Decimal("4"),
            unit_price=Decimal("150"),
        ),
    )

    position = position_of(session, funded_portfolio, "AAPL")
    assert position.quantity == Decimal("6")
    # Proceeds 600 less 4 * 140.175 average cost.
    assert position.realized_pnl == Decimal("39.30")
    assert cash_of(session, funded_portfolio) == Decimal("9198.25")


def test_dividend_separates_gross_withholding_and_net_cash(session, funded_portfolio) -> None:
    """Net-only recording would lose the withholding figure permanently."""
    event = record_transaction(
        session,
        funded_portfolio,
        TransactionRequest(
            request_id="div-1",
            event_type=EventType.DIVIDEND,
            ticker="AAPL",
            amount=Decimal("100"),
            tax=Decimal("30"),
        ),
    )

    legs = session.scalars(select(JournalLeg).where(JournalLeg.event_id == event.id)).all()
    by_type = {leg.leg_type: leg.amount_delta for leg in legs}
    assert by_type[LegType.INCOME.value] == Decimal("-100")
    assert by_type[LegType.TAX.value] == Decimal("30")
    assert by_type[LegType.CASH.value] == Decimal("70")
    assert cash_of(session, funded_portfolio) == Decimal("10070")


def test_reversal_restores_position_and_cash_without_deleting_the_original(
    session, funded_portfolio
) -> None:
    before_cash = cash_of(session, funded_portfolio)
    original = record_transaction(session, funded_portfolio, buy_request("reversible-1"))

    reversal = reverse_transaction(
        session, funded_portfolio, original.id, request_id="rev-1", memo="wrong account"
    )

    session.expire_all()
    assert cash_of(session, funded_portfolio) == before_cash
    assert position_of(session, funded_portfolio, "AAPL").quantity == Decimal("0")

    stored = session.get(JournalEvent, original.id)
    assert stored is not None, "the original event must survive its reversal"
    assert stored.status == EventStatus.REVERSED.value
    assert reversal.reverses_event_id == original.id


def test_an_event_cannot_be_reversed_twice(session, funded_portfolio) -> None:
    original = record_transaction(session, funded_portfolio, buy_request("once-1"))
    reverse_transaction(session, funded_portfolio, original.id, request_id="rev-a")

    with pytest.raises(DomainError) as excinfo:
        reverse_transaction(session, funded_portfolio, original.id, request_id="rev-b")
    assert excinfo.value.code == "already_reversed"


def test_a_reversal_cannot_itself_be_reversed(session, funded_portfolio) -> None:
    original = record_transaction(session, funded_portfolio, buy_request("mirror-1"))
    reversal = reverse_transaction(session, funded_portfolio, original.id, request_id="rev-c")

    with pytest.raises(DomainError) as excinfo:
        reverse_transaction(session, funded_portfolio, reversal.id, request_id="rev-d")
    assert excinfo.value.code == "cannot_reverse_a_reversal"


def test_trade_in_a_foreign_currency_portfolio_is_rejected(session, harness) -> None:
    portfolio_id = harness.portfolio("Taiwan", "TWD")
    harness.client.get("/api/v1/market/instruments/AAPL")
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id="twd-cash", event_type=EventType.DEPOSIT, amount=Decimal("100000")
        ),
    )

    with pytest.raises(DomainError) as excinfo:
        record_transaction(session, portfolio_id, buy_request("twd-buy"))
    assert excinfo.value.code == "currency_mismatch"
