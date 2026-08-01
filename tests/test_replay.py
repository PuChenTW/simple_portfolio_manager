"""Rebuilding state from the journal.

The load-bearing property is agreement: replaying to the present must reproduce exactly what the
live `positions` and `cash_balances` projections hold. If the two ever diverge, every historical
number built on replay is wrong in a way no downstream test would catch.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_manager.corporate_actions import (
    ActionType,
    apply_corporate_action,
    record_corporate_action,
)
from portfolio_manager.journal import EventType
from portfolio_manager.models import CashBalance, Position
from portfolio_manager.postings import TransactionRequest, record_transaction, reverse_transaction
from portfolio_manager.replay import replay_state

FUTURE = datetime(2030, 1, 1, tzinfo=UTC)
DAY1 = datetime(2026, 3, 2, tzinfo=UTC)
DAY2 = datetime(2026, 3, 3, tzinfo=UTC)
DAY3 = datetime(2026, 3, 4, tzinfo=UTC)


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def post(session, portfolio_id: str, **kwargs):
    return record_transaction(session, portfolio_id, TransactionRequest(**kwargs))


@pytest.fixture
def funded(harness, session) -> str:
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    post(
        session,
        portfolio_id,
        request_id="dep-1",
        event_type=EventType.DEPOSIT,
        amount=Decimal("20000"),
        occurred_at=DAY1,
    )
    return portfolio_id


def position_for(result, ticker: str):
    return next(item for item in result.positions if item.ticker == ticker)


def test_replay_reproduces_the_live_projections(session, funded) -> None:
    """Replay to now must equal what posting wrote; a divergence invalidates all history."""
    post(
        session,
        funded,
        request_id="buy-1",
        event_type=EventType.BUY,
        ticker="AAPL",
        quantity=Decimal("10"),
        unit_price=Decimal("140"),
        fee=Decimal("1.75"),
        occurred_at=DAY2,
    )
    post(
        session,
        funded,
        request_id="sell-1",
        event_type=EventType.SELL,
        ticker="AAPL",
        quantity=Decimal("4"),
        unit_price=Decimal("150"),
        occurred_at=DAY3,
    )

    result = replay_state(session, funded, FUTURE)
    session.expire_all()

    assert result.cash == session.get(CashBalance, funded).amount
    stored = session.get(Position, (funded, "AAPL"))
    replayed = position_for(result, "AAPL")
    assert replayed.quantity == stored.quantity
    assert replayed.average_cost == stored.average_cost
    assert replayed.realized_pnl == stored.realized_pnl


def test_replay_stops_at_the_cutoff(session, funded) -> None:
    post(
        session,
        funded,
        request_id="buy-1",
        event_type=EventType.BUY,
        ticker="AAPL",
        quantity=Decimal("10"),
        unit_price=Decimal("140"),
        occurred_at=DAY3,
    )

    before = replay_state(session, funded, DAY2)
    assert before.cash == Decimal("20000"), "a later purchase must not affect an earlier cutoff"
    assert before.positions == []

    after = replay_state(session, funded, DAY3)
    assert position_for(after, "AAPL").quantity == Decimal("10")


def test_cutoff_includes_events_at_the_boundary_instant(session, funded) -> None:
    """The cutoff is inclusive: a snapshot for a date must contain that date's own activity."""
    post(
        session,
        funded,
        request_id="buy-1",
        event_type=EventType.BUY,
        ticker="AAPL",
        quantity=Decimal("5"),
        unit_price=Decimal("140"),
        occurred_at=DAY2,
    )
    assert position_for(replay_state(session, funded, DAY2), "AAPL").quantity == Decimal("5")


def test_a_reversed_event_leaves_no_trace(session, funded) -> None:
    """Reversal is how corrections happen, so replay must net them to zero."""
    baseline = replay_state(session, funded, FUTURE)

    event = post(
        session,
        funded,
        request_id="buy-1",
        event_type=EventType.BUY,
        ticker="AAPL",
        quantity=Decimal("10"),
        unit_price=Decimal("140"),
        fee=Decimal("1.75"),
        occurred_at=DAY2,
    )
    reverse_transaction(session, funded, event.id, request_id="rev-1")

    result = replay_state(session, funded, FUTURE)
    assert result.cash == baseline.cash
    assert position_for(result, "AAPL").quantity == Decimal("0")
    assert result.coverage.events_applied == 3, "both the event and its reversal are retained"


def test_reversing_a_deposit_reduces_external_inflow(session, harness) -> None:
    """A reversal inherits the flow meaning of what it undoes rather than becoming unknown."""
    portfolio_id = harness.portfolio()
    event = post(
        session,
        portfolio_id,
        request_id="dep-1",
        event_type=EventType.DEPOSIT,
        amount=Decimal("5000"),
        occurred_at=DAY1,
    )
    reverse_transaction(session, portfolio_id, event.id, request_id="rev-dep")

    result = replay_state(session, portfolio_id, FUTURE)
    assert result.flows.net_external == Decimal("0")
    assert result.coverage.unknown_flow_events == 0, "a reversal is not an unclassified event"
    assert result.coverage.is_complete


def test_dividends_are_income_not_investor_capital(session, funded) -> None:
    """The distinction TWR depends on: a dividend raises cash without being a contribution."""
    post(
        session,
        funded,
        request_id="div-1",
        event_type=EventType.DIVIDEND,
        ticker="AAPL",
        amount=Decimal("120"),
        occurred_at=DAY2,
    )

    result = replay_state(session, funded, FUTURE)
    assert result.flows.income == Decimal("120")
    assert result.flows.net_external == Decimal("20000"), "only the deposit is external"


def test_income_and_costs_are_reported_as_magnitudes(session, funded) -> None:
    """Legs are signed to balance to zero; the breakdown reports sizes, not bookkeeping signs."""
    post(
        session,
        funded,
        request_id="div-1",
        event_type=EventType.DIVIDEND,
        ticker="AAPL",
        amount=Decimal("120"),
        tax=Decimal("18"),
        occurred_at=DAY2,
    )
    post(
        session,
        funded,
        request_id="fee-1",
        event_type=EventType.FEE,
        amount=Decimal("9"),
        occurred_at=DAY2,
    )

    flows = replay_state(session, funded, FUTURE).flows
    assert flows.income == Decimal("120"), "gross dividend, before withholding"
    assert flows.taxes == Decimal("18")
    assert flows.fees == Decimal("9")


def test_capitalized_trade_fees_are_still_reported(session, funded) -> None:
    """A trade's fee has no monetary leg -- it sits inside the security leg -- but stays visible."""
    post(
        session,
        funded,
        request_id="buy-1",
        event_type=EventType.BUY,
        ticker="AAPL",
        quantity=Decimal("10"),
        unit_price=Decimal("140"),
        fee=Decimal("1.75"),
        tax=Decimal("0.25"),
        occurred_at=DAY2,
    )

    result = replay_state(session, funded, FUTURE)
    assert result.flows.fees == Decimal("1.75")
    assert result.flows.taxes == Decimal("0.25")
    assert result.cash == Decimal("20000") - Decimal("1402"), "costs are not charged twice"


def test_flow_window_bounds_totals_but_not_balances(session, funded) -> None:
    """Balances are cumulative; flows are per-period. Mixing the two understates the portfolio."""
    post(
        session,
        funded,
        request_id="dep-2",
        event_type=EventType.DEPOSIT,
        amount=Decimal("5000"),
        occurred_at=DAY3,
    )

    result = replay_state(session, funded, FUTURE, since=DAY2)
    assert result.cash == Decimal("25000"), "cash carries the pre-window deposit"
    assert result.flows.external_in == Decimal("5000"), "flows count only the window"


def test_a_split_preserves_total_cost_basis_through_replay(session, funded) -> None:
    post(
        session,
        funded,
        request_id="buy-1",
        event_type=EventType.BUY,
        ticker="AAPL",
        quantity=Decimal("100"),
        unit_price=Decimal("140"),
        occurred_at=DAY1,
    )
    action = record_corporate_action(
        session,
        request_id="split-1",
        instrument_reference="AAPL",
        action_type=ActionType.SPLIT,
        ex_date=DAY2,
        ratio=Decimal("2"),
        source="issuer announcement",
    )
    apply_corporate_action(session, funded, action.id, request_id="apply-split")

    replayed = position_for(replay_state(session, funded, FUTURE), "AAPL")
    assert replayed.quantity == Decimal("200")
    assert replayed.average_cost == Decimal("70")
    assert replayed.cost_basis == Decimal("14000")


def test_a_journaled_portfolio_replays_complete(session, harness) -> None:
    """Every posted event classifies itself, so an ordinary portfolio reports no gaps.

    Coverage exists to surface what the service cannot determine. If it flagged a portfolio whose
    record is complete, the signal would be noise and readers would learn to ignore it.
    """
    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "c-1", "transaction_type": "deposit", "amount": "10000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "t-1",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "140",
        },
    )

    result = replay_state(session, portfolio_id, FUTURE)

    # The buy settled against cash in the same event: 10,000 - 1,400.
    assert result.cash == Decimal("8600")
    assert result.coverage.unknown_flow_events == 0
    assert result.coverage.is_complete is True
    assert result.coverage.warnings == []


def test_an_empty_portfolio_replays_to_zero(session, harness) -> None:
    result = replay_state(session, harness.portfolio(), FUTURE)
    assert result.cash == Decimal("0")
    assert result.positions == []
    assert result.coverage.is_complete, "nothing recorded is not the same as something unknown"


def test_replay_is_deterministic(session, funded) -> None:
    """Same journal, same answer: rebuilds must be reproducible per plan principle 10."""
    for index in range(5):
        post(
            session,
            funded,
            request_id=f"buy-{index}",
            event_type=EventType.BUY,
            ticker="AAPL",
            quantity=Decimal("3"),
            unit_price=Decimal("140") + index,
            occurred_at=DAY2 + timedelta(seconds=index),
        )

    first = replay_state(session, funded, FUTURE)
    second = replay_state(session, funded, FUTURE)
    assert first.cash == second.cash
    assert position_for(first, "AAPL").average_cost == position_for(second, "AAPL").average_cost
