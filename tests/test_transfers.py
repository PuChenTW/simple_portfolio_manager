"""Transfers: two linked events, one transaction, no half-recorded money.

The invariant under test throughout is conservation. Money that leaves one portfolio arrives in
the other, at the rate the user supplied, or nothing happens at all -- including when the second
half is the one that fails.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventStatus, EventType, LegType
from portfolio_manager.models import CashBalance, JournalEvent, JournalLeg
from portfolio_manager.postings import (
    TransactionRequest,
    event_detail,
    record_transaction,
    reverse_transaction,
)
from portfolio_manager.replay import replay_state
from portfolio_manager.transfers import events_of, reverse_transfer, transfer_cash

FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def cash_of(session, portfolio_id: str) -> Decimal:
    session.expire_all()
    balance = session.get(CashBalance, portfolio_id)
    return balance.amount if balance else Decimal("0")


def fund(session, portfolio_id: str, amount: str) -> None:
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id=f"seed-{portfolio_id}",
            event_type=EventType.DEPOSIT,
            amount=Decimal(amount),
        ),
    )


def event_count(session, portfolio_id: str) -> int:
    session.expire_all()
    return len(
        session.scalars(
            select(JournalEvent).where(JournalEvent.portfolio_id == portfolio_id)
        ).all()
    )


@pytest.fixture
def accounts(harness, session) -> tuple[str, str]:
    """A funded USD bank account and an empty USD brokerage account."""
    bank = harness.cash_account("USD bank", "USD")
    broker = harness.portfolio("USD broker", "USD")
    fund(session, bank, "10000")
    return bank, broker


def test_a_transfer_conserves_total_cash(accounts, session) -> None:
    bank, broker = accounts
    before = cash_of(session, bank) + cash_of(session, broker)

    transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    assert cash_of(session, bank) == Decimal("7500")
    assert cash_of(session, broker) == Decimal("2500")
    assert cash_of(session, bank) + cash_of(session, broker) == before


def test_a_transfer_writes_one_linked_event_in_each_portfolio(accounts, session) -> None:
    bank, broker = accounts

    out_event, in_event = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    assert out_event.portfolio_id == bank
    assert in_event.portfolio_id == broker
    assert out_event.event_type == EventType.TRANSFER_OUT.value
    assert in_event.event_type == EventType.TRANSFER_IN.value
    assert out_event.transfer_id == in_event.transfer_id
    assert (out_event.transfer_role, in_event.transfer_role) == ("out", "in")


def test_each_half_of_a_transfer_balances_on_its_own(accounts, session) -> None:
    """Each event nets to zero in its own currency, so no half needs the other to be valid."""
    bank, broker = accounts
    out_event, in_event = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    for event in (out_event, in_event):
        report = event_detail(session, event.portfolio_id, event.id)["balance"]
        assert report.balanced is True
        assert report.residual == Decimal("0")
        assert report.warnings == [], "a single-currency event needs no parity assumption"


def test_a_cross_currency_transfer_uses_the_supplied_rate(harness, session) -> None:
    bank = harness.cash_account("TWD bank", "TWD")
    broker = harness.portfolio("USD broker", "USD")
    fund(session, bank, "320000")

    transfer_cash(
        session, bank, broker, "t-1", Decimal("32000"), fx_rate=Decimal("0.03125")
    )

    assert cash_of(session, bank) == Decimal("288000")
    assert cash_of(session, broker) == Decimal("1000")


def test_a_cross_currency_transfer_records_its_rate_for_audit(harness, session) -> None:
    bank = harness.cash_account("TWD bank", "TWD")
    broker = harness.portfolio("USD broker", "USD")
    fund(session, bank, "320000")

    out_event, _ = transfer_cash(
        session, bank, broker, "t-1", Decimal("32000"), fx_rate=Decimal("0.03125")
    )

    legs = session.scalars(
        select(JournalLeg).where(JournalLeg.event_id == out_event.id)
    ).all()
    counter = next(leg for leg in legs if leg.leg_type == LegType.OTHER.value)
    recorded = json.loads(counter.leg_metadata)
    assert recorded["fx_rate"] == "0.03125"
    assert recorded["counterparty_amount"] == "1000.00000"
    assert recorded["counterparty_currency"] == "USD"


def test_a_cross_currency_leg_carries_no_fx_rate_column(harness, session) -> None:
    """The rate must stay out of Leg.fx_rate, which would unbalance the event.

    `functional_amount` multiplies by that field unconditionally, and both legs are already in
    the event's own currency, so a rate there would scale one side of a balanced pair.
    """
    bank = harness.cash_account("TWD bank", "TWD")
    broker = harness.portfolio("USD broker", "USD")
    fund(session, bank, "320000")

    out_event, in_event = transfer_cash(
        session, bank, broker, "t-1", Decimal("32000"), fx_rate=Decimal("0.03125")
    )

    for event in (out_event, in_event):
        legs = session.scalars(
            select(JournalLeg).where(JournalLeg.event_id == event.id)
        ).all()
        assert all(leg.fx_rate is None for leg in legs)
        assert event_detail(session, event.portfolio_id, event.id)["balance"].balanced


def test_a_cross_currency_transfer_without_a_rate_is_refused(harness, session) -> None:
    bank = harness.cash_account("TWD bank", "TWD")
    broker = harness.portfolio("USD broker", "USD")
    fund(session, bank, "320000")

    with pytest.raises(DomainError) as excinfo:
        transfer_cash(session, bank, broker, "t-1", Decimal("32000"))

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "fx_rate_required"
    assert cash_of(session, bank) == Decimal("320000"), "nothing moved"
    assert event_count(session, broker) == 0


def test_a_same_currency_transfer_refuses_an_fx_rate(accounts, session) -> None:
    bank, broker = accounts

    with pytest.raises(DomainError) as excinfo:
        transfer_cash(session, bank, broker, "t-1", Decimal("100"), fx_rate=Decimal("1.02"))

    assert excinfo.value.code == "unexpected_fx_rate"


def test_a_transfer_to_the_same_portfolio_is_refused(accounts, session) -> None:
    bank, _ = accounts

    with pytest.raises(DomainError) as excinfo:
        transfer_cash(session, bank, bank, "t-1", Decimal("100"))

    assert excinfo.value.code == "self_transfer"


def test_an_underfunded_transfer_leaves_both_portfolios_untouched(accounts, session) -> None:
    """The overdraft is caught on the source, before the destination is written."""
    bank, broker = accounts

    with pytest.raises(DomainError) as excinfo:
        transfer_cash(session, bank, broker, "t-1", Decimal("999999"))

    assert excinfo.value.code == "insufficient_cash"
    assert cash_of(session, bank) == Decimal("10000")
    assert cash_of(session, broker) == Decimal("0")
    assert event_count(session, broker) == 0


def test_repeating_a_transfer_request_id_returns_the_same_pair(accounts, session) -> None:
    bank, broker = accounts
    first_out, first_in = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    second_out, second_in = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    assert (second_out.id, second_in.id) == (first_out.id, first_in.id)
    assert cash_of(session, bank) == Decimal("7500"), "the repeat moved no further money"
    assert event_count(session, broker) == 1


def test_reusing_a_transfer_request_id_with_different_data_conflicts(accounts, session) -> None:
    bank, broker = accounts
    transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    with pytest.raises(DomainError) as excinfo:
        transfer_cash(session, bank, broker, "t-1", Decimal("999"))

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "idempotency_conflict"


def test_reversing_one_half_of_a_transfer_is_refused(accounts, session) -> None:
    bank, broker = accounts
    out_event, in_event = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    for event in (out_event, in_event):
        with pytest.raises(DomainError) as excinfo:
            reverse_transaction(session, event.portfolio_id, event.id, f"rev-{event.id}")
        assert excinfo.value.status_code == 409
        assert excinfo.value.code == "reverse_the_transfer_instead"


def test_reversing_a_transfer_unwinds_both_sides(accounts, session) -> None:
    bank, broker = accounts
    out_event, in_event = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    reverse_transfer(session, out_event.transfer_id, "rev-1")

    assert cash_of(session, bank) == Decimal("10000")
    assert cash_of(session, broker) == Decimal("0")
    session.expire_all()
    assert session.get(JournalEvent, out_event.id).status == EventStatus.REVERSED.value
    assert session.get(JournalEvent, in_event.id).status == EventStatus.REVERSED.value
    assert len(events_of(session, out_event.transfer_id)) == 4


def test_reversing_a_cross_currency_transfer_restores_both_balances(harness, session) -> None:
    """The undo uses the executed rate, so it cannot manufacture an FX gain."""
    bank = harness.cash_account("TWD bank", "TWD")
    broker = harness.portfolio("USD broker", "USD")
    fund(session, bank, "320000")
    out_event, _ = transfer_cash(
        session, bank, broker, "t-1", Decimal("32000"), fx_rate=Decimal("0.03125")
    )

    reverse_transfer(session, out_event.transfer_id, "rev-1")

    assert cash_of(session, bank) == Decimal("320000")
    assert cash_of(session, broker) == Decimal("0")


def test_reversing_a_transfer_twice_is_refused(accounts, session) -> None:
    bank, broker = accounts
    out_event, _ = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))
    reverse_transfer(session, out_event.transfer_id, "rev-1")

    with pytest.raises(DomainError) as excinfo:
        reverse_transfer(session, out_event.transfer_id, "rev-2")

    assert excinfo.value.code == "already_reversed"


def test_reversing_a_transfer_the_destination_spent_is_refused(accounts, session) -> None:
    """Better to refuse than to leave a negative balance the ledger forbids elsewhere."""
    bank, broker = accounts
    out_event, _ = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))
    record_transaction(
        session,
        broker,
        TransactionRequest(
            request_id="spend-1",
            event_type=EventType.WITHDRAWAL,
            amount=Decimal("2000"),
        ),
    )

    with pytest.raises(DomainError) as excinfo:
        reverse_transfer(session, out_event.transfer_id, "rev-1")

    assert excinfo.value.code == "insufficient_cash"
    assert cash_of(session, bank) == Decimal("7500"), "the refusal rolled back the source too"
    assert cash_of(session, broker) == Decimal("500")


def test_reversing_an_unknown_transfer_is_not_found(session) -> None:
    with pytest.raises(DomainError) as excinfo:
        reverse_transfer(session, "no-such-transfer", "rev-1")

    assert excinfo.value.status_code == 404


def test_a_transfer_replays_as_external_on_both_sides(accounts, session) -> None:
    """External is right per portfolio: the money genuinely crossed each book's boundary.

    TWR must neutralize it on both sides, or moving cash into a broker would read as a return.
    Netting the pair is a group-level question, and there is no group-level return to distort.
    """
    bank, broker = accounts
    transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    source = replay_state(session, bank, FUTURE)
    destination = replay_state(session, broker, FUTURE)

    assert source.flows.net_external == Decimal("7500"), "10000 deposited, 2500 sent out"
    assert destination.flows.net_external == Decimal("2500")
    assert source.coverage.unknown_flow_events == 0
    assert destination.coverage.unknown_flow_events == 0


def test_a_reversed_transfer_nets_to_no_external_flow(accounts, session) -> None:
    bank, broker = accounts
    out_event, _ = transfer_cash(session, bank, broker, "t-1", Decimal("2500"))

    reverse_transfer(session, out_event.transfer_id, "rev-1")

    destination = replay_state(session, broker, FUTURE)
    assert destination.flows.net_external == Decimal("0")
    assert destination.coverage.unknown_flow_events == 0, "a reversal is not an unknown flow"


def test_a_transfer_into_a_cash_account_is_allowed(harness, session) -> None:
    """The securities guardrail must not block cash movement."""
    broker = harness.portfolio("USD broker", "USD")
    bank = harness.cash_account("USD bank", "USD")
    fund(session, broker, "5000")

    transfer_cash(session, broker, bank, "t-1", Decimal("1200"))

    assert cash_of(session, bank) == Decimal("1200")
    assert cash_of(session, broker) == Decimal("3800")
