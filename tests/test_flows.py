"""Reclassifying legacy cash: a ruling, not a guess.

The pre-journal model had only `deposit` and `withdraw`, so an operator settling a day's trading
recorded something indistinguishable from investor capital. Left alone, TWR would neutralize
those trading proceeds as contributions and understate the return. These tests cover the ruling
that fixes it, and the discipline that keeps it honest: the posted event is never edited, the
reason is mandatory, and retracting restores the original reading.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from portfolio_manager.backfill import backfill_portfolio
from portfolio_manager.errors import DomainError
from portfolio_manager.flows import (
    resolve_flow,
    set_flow_override,
    suggest_reclassifications,
)
from portfolio_manager.journal import FlowClassification
from portfolio_manager.models import JournalEvent, Portfolio
from portfolio_manager.replay import replay_state

FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


@pytest.fixture
def legacy(harness, session) -> str:
    """A portfolio mirroring the operator's own data: a real deposit, then a settled trade.

    The 1400 withdrawal is not money leaving the portfolio -- it settles the AAPL purchase on the
    same day, exactly as the migrated production data does.
    """
    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/cash-transactions",
        json={"request_id": "opening-cash", "action": "deposit", "amount": "10000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/trades",
        json={
            "request_id": "t-1",
            "ticker": "AAPL",
            "side": "buy",
            "quantity": "10",
            "unit_price": "140",
        },
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/cash-transactions",
        json={"request_id": "cash-adjust-to-8600", "action": "withdraw", "amount": "1400"},
    )
    backfill_portfolio(session, session.get(Portfolio, portfolio_id))
    session.commit()
    return portfolio_id


def event_by_request(session, portfolio_id: str, request_id: str) -> JournalEvent:
    import sqlalchemy as sa

    return session.scalar(
        sa.select(JournalEvent).where(
            JournalEvent.portfolio_id == portfolio_id,
            JournalEvent.source_reference == request_id,
        )
    )


def test_migrated_cash_starts_classified_by_type(session, legacy) -> None:
    """The starting point: a settlement recorded as a withdrawal reads as investor capital."""
    flows = replay_state(session, legacy, FUTURE).flows
    assert flows.external_in == Decimal("10000")
    assert flows.external_out == Decimal("-1400"), "the settlement is miscounted as a withdrawal"
    assert flows.net_external == Decimal("8600")


def test_an_override_moves_a_settlement_out_of_external_flow(session, legacy) -> None:
    """The whole point: after the ruling, TWR no longer sees the settlement as a contribution."""
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    set_flow_override(
        session,
        settlement.id,
        classification="internal",
        reason="Settles the same-day AAPL purchase; the amounts match exactly.",
    )
    session.commit()

    flows = replay_state(session, legacy, FUTURE).flows
    assert flows.net_external == Decimal("10000"), "only the real deposit remains external"
    assert flows.external_out == Decimal("0")


def test_an_override_does_not_change_the_cash_balance(session, legacy) -> None:
    """Reclassifying says what a movement meant, never that it did not happen."""
    before = replay_state(session, legacy, FUTURE).cash
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    set_flow_override(
        session, settlement.id, classification="internal", reason="Same-day settlement."
    )
    session.commit()

    assert replay_state(session, legacy, FUTURE).cash == before == Decimal("8600")


def test_the_posted_event_is_never_modified(session, legacy) -> None:
    """Plan principle 6: posted events are immutable, so a ruling lives beside them."""
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    original_type, original_status = settlement.event_type, settlement.status

    set_flow_override(
        session, settlement.id, classification="internal", reason="Same-day settlement."
    )
    session.commit()
    session.expire_all()

    after = session.get(JournalEvent, settlement.id)
    assert (after.event_type, after.status) == (original_type, original_status)


def test_retracting_restores_the_derived_classification(session, legacy) -> None:
    """Plan principle: an override outranks the original rather than replacing it."""
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    set_flow_override(
        session, settlement.id, classification="internal", reason="Same-day settlement."
    )
    session.commit()
    assert resolve_flow(session, settlement) is FlowClassification.INTERNAL

    set_flow_override(
        session,
        settlement.id,
        classification="internal",
        reason="Retracting: needs re-checking against the broker statement.",
        retract=True,
    )
    session.commit()

    assert resolve_flow(session, settlement) is FlowClassification.EXTERNAL
    assert replay_state(session, legacy, FUTURE).flows.net_external == Decimal("8600")


def test_a_ruling_requires_a_reason(session, legacy) -> None:
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    with pytest.raises(DomainError) as excinfo:
        set_flow_override(session, settlement.id, classification="internal", reason="   ")
    assert excinfo.value.code == "missing_reason"


def test_an_unknown_classification_is_rejected(session, legacy) -> None:
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    with pytest.raises(DomainError) as excinfo:
        set_flow_override(session, settlement.id, classification="settlement", reason="x")
    assert excinfo.value.code == "unknown_flow_classification"


def test_a_ruling_on_a_missing_event_is_not_found(session) -> None:
    with pytest.raises(DomainError) as excinfo:
        set_flow_override(session, "nope", classification="internal", reason="x")
    assert excinfo.value.status_code == 404


def test_a_ruled_event_stops_counting_as_an_unexplained_gap(session, legacy) -> None:
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    before = replay_state(session, legacy, FUTURE).coverage
    assert before.unlinked_legacy_events == 3

    set_flow_override(
        session, settlement.id, classification="internal", reason="Same-day settlement."
    )
    session.commit()

    after = replay_state(session, legacy, FUTURE).coverage
    assert after.unlinked_legacy_events == 2
    assert after.reclassified_legacy_events == 1
    assert any("reclassified by hand" in warning for warning in after.warnings)


def test_suggestions_match_settlements_by_amount(session, legacy) -> None:
    """The signal is arithmetic: a day's trades netting to the day's cash movement."""
    suggestions = suggest_reclassifications(session, legacy)
    by_reference = {item.source_reference: item for item in suggestions}

    settlement = next(
        item for key, item in by_reference.items() if key and "cash-adjust" in key
    )
    assert settlement.suggested is FlowClassification.INTERNAL
    assert settlement.confidence == "high"
    assert any("the amounts match" in line for line in settlement.evidence)


def test_a_deposit_on_a_day_without_trades_is_suggested_external(session, legacy) -> None:
    suggestions = suggest_reclassifications(session, legacy)
    opening = next(item for item in suggestions if "opening" in (item.source_reference or ""))
    assert opening.suggested is FlowClassification.EXTERNAL


def test_suggestions_are_advisory_and_change_nothing(session, legacy) -> None:
    """Plan principle 3: a confident guess must not write itself into the record."""
    before = replay_state(session, legacy, FUTURE).flows.net_external
    suggest_reclassifications(session, legacy)
    suggest_reclassifications(session, legacy)

    assert replay_state(session, legacy, FUTURE).flows.net_external == before


def test_ruled_events_drop_out_of_the_review_queue(session, legacy) -> None:
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    set_flow_override(
        session, settlement.id, classification="internal", reason="Same-day settlement."
    )
    session.commit()

    remaining = {item.event_id for item in suggest_reclassifications(session, legacy)}
    assert settlement.id not in remaining


def test_a_ruling_can_be_revised(session, legacy) -> None:
    settlement = event_by_request(session, legacy, "cash-adjust-to-8600")
    set_flow_override(session, settlement.id, classification="internal", reason="First reading.")
    session.commit()
    set_flow_override(
        session,
        settlement.id,
        classification="external",
        reason="Broker statement shows this really did leave the account.",
    )
    session.commit()

    assert resolve_flow(session, settlement) is FlowClassification.EXTERNAL
    assert replay_state(session, legacy, FUTURE).flows.net_external == Decimal("8600")
