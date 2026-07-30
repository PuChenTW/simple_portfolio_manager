"""Legacy backfill: migrate history without inventing settlements that were never recorded."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from portfolio_manager.backfill import (
    backfill_all,
    backfill_portfolio,
    verify_projection_consistency,
)
from portfolio_manager.journal import LegType
from portfolio_manager.models import CashBalance, JournalEvent, JournalLeg, Portfolio, Position


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


@pytest.fixture
def legacy_portfolio(harness) -> str:
    """A portfolio built entirely through the pre-journal endpoints."""
    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/cash-transactions",
        json={"request_id": "legacy-cash-1", "action": "deposit", "amount": "10000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/trades",
        json={
            "request_id": "legacy-trade-1",
            "ticker": "AAPL",
            "side": "buy",
            "quantity": "10",
            "unit_price": "140",
            "fee": "1.75",
        },
    )
    return portfolio_id


def events_of(session, portfolio_id: str) -> list[JournalEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(JournalEvent)
            .where(JournalEvent.portfolio_id == portfolio_id)
            .order_by(JournalEvent.occurred_at)
        ).all()
    )


def test_backfill_migrates_trades_and_cash_as_separate_events(session, legacy_portfolio) -> None:
    portfolio = session.get(Portfolio, legacy_portfolio)
    report = backfill_portfolio(session, portfolio)
    session.commit()

    assert report.trades_migrated == 1
    assert report.cash_transactions_migrated == 1

    events = events_of(session, legacy_portfolio)
    assert len(events) == 2, "each legacy row becomes its own event; none are merged"
    assert {event.event_type for event in events} == {"deposit", "buy"}


def test_every_migrated_event_is_marked_unlinked(session, legacy_portfolio) -> None:
    """The trade-to-cash linkage was never recorded, so it must not be asserted now."""
    portfolio = session.get(Portfolio, legacy_portfolio)
    report = backfill_portfolio(session, portfolio)
    session.commit()

    events = events_of(session, legacy_portfolio)
    assert all(event.is_unlinked_legacy for event in events)
    assert report.unlinked_events == 2
    assert any("cannot be reconstructed and was not guessed" in w for w in report.warnings)


def test_migrated_trade_carries_no_invented_cash_leg(session, legacy_portfolio) -> None:
    """The original trade did not move cash; the migration must not manufacture a settlement."""
    portfolio = session.get(Portfolio, legacy_portfolio)
    backfill_portfolio(session, portfolio)
    session.commit()

    buy = next(event for event in events_of(session, legacy_portfolio) if event.event_type == "buy")
    legs = session.scalars(select(JournalLeg).where(JournalLeg.event_id == buy.id)).all()

    assert [leg.leg_type for leg in legs] == [LegType.SECURITY.value]
    security = legs[0]
    assert security.quantity_delta == Decimal("10")
    assert security.amount_delta == Decimal("1401.75"), "consideration plus the recorded fee"


def test_backfill_does_not_restate_positions_or_cash(session, legacy_portfolio) -> None:
    """Projections already reflect these rows; replaying them would double-count."""
    before_cash = session.get(CashBalance, legacy_portfolio).amount
    before_quantity = session.get(Position, (legacy_portfolio, "AAPL")).quantity

    portfolio = session.get(Portfolio, legacy_portfolio)
    backfill_portfolio(session, portfolio)
    session.commit()
    session.expire_all()

    assert session.get(CashBalance, legacy_portfolio).amount == before_cash
    assert session.get(Position, (legacy_portfolio, "AAPL")).quantity == before_quantity


def test_backfill_is_idempotent(session, legacy_portfolio) -> None:
    portfolio = session.get(Portfolio, legacy_portfolio)
    backfill_portfolio(session, portfolio)
    session.commit()

    second = backfill_portfolio(session, portfolio)
    session.commit()

    assert second.trades_migrated == 0
    assert second.cash_transactions_migrated == 0
    assert second.already_migrated == 2
    assert len(events_of(session, legacy_portfolio)) == 2


def test_backfill_all_covers_every_portfolio(session, harness) -> None:
    first = harness.portfolio("One", "USD")
    second = harness.portfolio("Two", "TWD")
    for portfolio_id, amount in ((first, "500"), (second, "9000")):
        harness.client.post(
            f"/api/v1/portfolios/{portfolio_id}/cash-transactions",
            json={"request_id": f"c-{portfolio_id}", "action": "deposit", "amount": amount},
        )

    report = backfill_all(session)
    assert report.cash_transactions_migrated == 2
    assert len(events_of(session, first)) == 1
    assert len(events_of(session, second)) == 1


def test_consistency_check_explains_a_legacy_discrepancy(session, legacy_portfolio) -> None:
    """A migrated trade has no cash leg, so the journal and stored cash legitimately differ."""
    portfolio = session.get(Portfolio, legacy_portfolio)
    backfill_portfolio(session, portfolio)
    session.commit()

    result = verify_projection_consistency(session, legacy_portfolio)
    assert result["stored_cash"] == Decimal("10000")
    assert result["journal_cash"] == Decimal("10000")
    assert result["consistent"] is True
    assert result["has_unlinked_legacy_events"] is True


def test_consistency_check_reports_a_real_mismatch(session, legacy_portfolio) -> None:
    portfolio = session.get(Portfolio, legacy_portfolio)
    backfill_portfolio(session, portfolio)
    session.commit()

    balance = session.get(CashBalance, legacy_portfolio)
    balance.amount = Decimal("9999")  # simulate drift
    session.commit()

    result = verify_projection_consistency(session, legacy_portfolio)
    assert result["consistent"] is False
    assert result["difference"] == Decimal("-1")
    assert any("differs from the journal" in warning for warning in result["warnings"])
