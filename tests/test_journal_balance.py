"""Unit tests for the journal balance invariant and reversal inversion."""

from decimal import Decimal

import pytest

from portfolio_manager.errors import DomainError
from portfolio_manager.journal import (
    EventType,
    FlowClassification,
    Leg,
    LegType,
    classify_flow,
    invert,
    require_balanced,
    validate_balance,
)


def buy_legs() -> list[Leg]:
    """10 shares at 140, plus a 1.50 fee and 0.25 tax, settled in cash."""
    return [
        Leg(
            leg_type=LegType.SECURITY,
            currency="USD",
            account_role="position",
            amount_delta=Decimal("1400"),
            quantity_delta=Decimal("10"),
            unit_price=Decimal("140"),
            instrument_id="instr-1",
        ),
        Leg(
            leg_type=LegType.FEE,
            currency="USD",
            account_role="expense",
            amount_delta=Decimal("1.50"),
        ),
        Leg(
            leg_type=LegType.TAX,
            currency="USD",
            account_role="expense",
            amount_delta=Decimal("0.25"),
        ),
        Leg(
            leg_type=LegType.CASH,
            currency="USD",
            account_role="settlement",
            amount_delta=Decimal("-1401.75"),
        ),
    ]


def test_balanced_buy_passes() -> None:
    report = require_balanced(buy_legs(), "USD")
    assert report.balanced
    assert report.residual == Decimal("0")
    assert report.leg_count == 4


def test_unbalanced_event_is_rejected_with_the_residual() -> None:
    """A settlement that ignores the fee must not post; the residual identifies the omission."""
    legs = buy_legs()
    legs[-1].amount_delta = Decimal("-1400")  # forgot the 1.75 of fee and tax

    with pytest.raises(DomainError) as excinfo:
        require_balanced(legs, "USD")
    error = excinfo.value
    assert error.code == "journal_out_of_balance"
    assert error.details["residual"] == "1.75"
    assert len(error.details["legs"]) == 4


def test_empty_event_is_rejected() -> None:
    with pytest.raises(DomainError) as excinfo:
        validate_balance([], "USD")
    assert excinfo.value.code == "empty_journal_event"


def test_quantity_only_legs_do_not_break_the_monetary_balance() -> None:
    """A split changes share count without moving money."""
    legs = [
        Leg(
            leg_type=LegType.SECURITY,
            currency="USD",
            account_role="position",
            quantity_delta=Decimal("10"),
            instrument_id="instr-1",
        )
    ]
    assert validate_balance(legs, "USD").balanced


def test_foreign_currency_leg_balances_through_its_explicit_fx_rate() -> None:
    legs = [
        Leg(
            leg_type=LegType.SECURITY,
            currency="TWD",
            account_role="position",
            amount_delta=Decimal("3200"),
            quantity_delta=Decimal("1"),
            fx_rate=Decimal("0.03125"),  # TWD -> USD
            instrument_id="instr-tw",
        ),
        Leg(
            leg_type=LegType.CASH,
            currency="USD",
            account_role="settlement",
            amount_delta=Decimal("-100"),
        ),
    ]
    assert require_balanced(legs, "USD").balanced


def test_decimal_arithmetic_is_exact_for_values_binary_floats_cannot_represent() -> None:
    """0.1 + 0.2 - 0.3 must be exactly zero; this is why money is never a binary float."""
    legs = [
        Leg(
            leg_type=LegType.CASH, currency="USD", account_role="a", amount_delta=Decimal("0.1")
        ),
        Leg(
            leg_type=LegType.CASH, currency="USD", account_role="b", amount_delta=Decimal("0.2")
        ),
        Leg(
            leg_type=LegType.CASH, currency="USD", account_role="c", amount_delta=Decimal("-0.3")
        ),
    ]
    report = validate_balance(legs, "USD")
    assert report.residual == Decimal("0")
    assert report.balanced


def test_inverted_legs_cancel_the_original_exactly() -> None:
    original = buy_legs()
    combined = original + invert(original)
    report = validate_balance(combined, "USD")
    assert report.residual == Decimal("0")

    quantities = sum(
        (leg.quantity_delta for leg in combined if leg.quantity_delta is not None),
        start=Decimal("0"),
    )
    assert quantities == Decimal("0"), "a reversal must restore the original share count"


def test_inversion_preserves_instrument_and_currency_identity() -> None:
    inverted = invert(buy_legs())
    security = next(leg for leg in inverted if leg.leg_type == LegType.SECURITY)
    assert security.instrument_id == "instr-1"
    assert security.currency == "USD"
    assert security.quantity_delta == Decimal("-10")
    assert security.unit_price == Decimal("140"), "price is a fact about the original execution"


def test_deposits_are_external_and_dividends_are_internal() -> None:
    """TWR neutralizes external flows, so misclassifying a dividend would distort performance."""
    assert classify_flow(EventType.DEPOSIT) is FlowClassification.EXTERNAL
    assert classify_flow(EventType.WITHDRAWAL) is FlowClassification.EXTERNAL
    assert classify_flow(EventType.TRANSFER_IN) is FlowClassification.EXTERNAL

    assert classify_flow(EventType.DIVIDEND) is FlowClassification.INTERNAL
    assert classify_flow(EventType.INTEREST) is FlowClassification.INTERNAL
    assert classify_flow(EventType.BUY) is FlowClassification.INTERNAL
    assert classify_flow(EventType.FEE) is FlowClassification.INTERNAL
