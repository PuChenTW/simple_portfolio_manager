"""Corporate actions: split arithmetic, income handling, and unresolved cost basis.

The governing rule under test is that a treatment this service cannot determine is reported as
unresolved rather than approximated. A wrong-but-plausible allocation would be indistinguishable
from a fact and would corrupt every gain calculation downstream.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from portfolio_manager.corporate_actions import (
    ActionType,
    apply_corporate_action,
    preview_application,
    record_corporate_action,
)
from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventType
from portfolio_manager.models import CashBalance, Position
from portfolio_manager.postings import TransactionRequest, record_transaction

EX_DATE = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


@pytest.fixture
def holding(harness, session) -> str:
    """A portfolio holding 100 AAPL at an average cost of 140."""
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id="seed", event_type=EventType.DEPOSIT, amount=Decimal("20000")
        ),
    )
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id="buy",
            event_type=EventType.BUY,
            ticker="AAPL",
            quantity=Decimal("100"),
            unit_price=Decimal("140"),
        ),
    )
    return portfolio_id


def position_of(session, portfolio_id: str) -> Position:
    session.expire_all()
    return session.get(Position, (portfolio_id, "AAPL"))


def cash_of(session, portfolio_id: str) -> Decimal:
    session.expire_all()
    return session.get(CashBalance, portfolio_id).amount


def test_two_for_one_split_doubles_quantity_and_halves_unit_cost(session, holding) -> None:
    """Total cost basis is invariant across a split: it redenominates the same stake."""
    action = record_corporate_action(
        session,
        request_id="split-1",
        instrument_reference="AAPL",
        action_type=ActionType.SPLIT,
        ex_date=EX_DATE,
        ratio=Decimal("2"),
        source="issuer announcement",
    )
    apply_corporate_action(session, holding, action.id, request_id="apply-split-1")

    position = position_of(session, holding)
    assert position.quantity == Decimal("200")
    assert position.average_cost == Decimal("70")
    total_basis = position.quantity * position.average_cost
    assert total_basis == Decimal("14000"), "a split must not change total cost basis"


def test_reverse_split_reduces_quantity_and_raises_unit_cost(session, holding) -> None:
    action = record_corporate_action(
        session,
        request_id="rsplit-1",
        instrument_reference="AAPL",
        action_type=ActionType.REVERSE_SPLIT,
        ex_date=EX_DATE,
        ratio=Decimal("0.5"),
        source="issuer announcement",
    )
    apply_corporate_action(session, holding, action.id, request_id="apply-rsplit-1")

    position = position_of(session, holding)
    assert position.quantity == Decimal("50")
    assert position.average_cost == Decimal("280")
    assert position.quantity * position.average_cost == Decimal("14000")


def test_split_does_not_change_cash(session, holding) -> None:
    before = cash_of(session, holding)
    action = record_corporate_action(
        session,
        request_id="split-2",
        instrument_reference="AAPL",
        action_type=ActionType.SPLIT,
        ex_date=EX_DATE,
        ratio=Decimal("2"),
        source="issuer announcement",
    )
    apply_corporate_action(session, holding, action.id, request_id="apply-split-2")
    assert cash_of(session, holding) == before, "a split creates no cash"


def test_cash_dividend_pays_net_of_withholding_without_touching_the_position(
    session, holding
) -> None:
    before_cash = cash_of(session, holding)
    action = record_corporate_action(
        session,
        request_id="div-1",
        instrument_reference="AAPL",
        action_type=ActionType.CASH_DIVIDEND,
        ex_date=EX_DATE,
        cash_amount=Decimal("0.25"),  # per share
        withholding_tax=Decimal("3.75"),
        source="issuer announcement",
    )
    preview = preview_application(session, holding, action.id)
    assert preview.cash_amount == Decimal("25")  # 100 shares * 0.25
    assert preview.withholding_tax == Decimal("3.75")

    apply_corporate_action(session, holding, action.id, request_id="apply-div-1")

    assert cash_of(session, holding) == before_cash + Decimal("21.25")
    position = position_of(session, holding)
    assert position.quantity == Decimal("100"), "a cash dividend does not change share count"
    assert position.average_cost == Decimal("140")


def test_return_of_capital_reduces_basis_rather_than_booking_income(session, holding) -> None:
    action = record_corporate_action(
        session,
        request_id="roc-1",
        instrument_reference="AAPL",
        action_type=ActionType.RETURN_OF_CAPITAL,
        ex_date=EX_DATE,
        cash_amount=Decimal("10"),  # per share, well under the 140 basis
        source="issuer announcement",
    )
    apply_corporate_action(session, holding, action.id, request_id="apply-roc-1")

    position = position_of(session, holding)
    assert position.quantity == Decimal("100")
    assert position.average_cost == Decimal("130"), "basis absorbs the distribution"


def test_return_of_capital_above_basis_is_left_unresolved(session, holding) -> None:
    """Basis floors at zero; the excess is taxable under rules that vary by jurisdiction."""
    action = record_corporate_action(
        session,
        request_id="roc-2",
        instrument_reference="AAPL",
        action_type=ActionType.RETURN_OF_CAPITAL,
        ex_date=EX_DATE,
        cash_amount=Decimal("200"),  # exceeds the 140 per-share basis
        source="issuer announcement",
    )
    preview = preview_application(session, holding, action.id)

    assert preview.cost_basis_unresolved is True
    assert preview.resulting_average_cost == Decimal("0")
    assert any("exceeds remaining cost basis" in warning for warning in preview.warnings)


def test_spinoff_allocation_is_never_guessed(session, holding) -> None:
    """Without a disclosed allocation, the action is recorded but not applied."""
    action = record_corporate_action(
        session,
        request_id="spin-1",
        instrument_reference="AAPL",
        action_type=ActionType.SPINOFF,
        ex_date=EX_DATE,
        source="issuer announcement",
    )
    assert action.cost_basis_unresolved is True
    assert action.cost_allocation_percent is None

    preview = preview_application(session, holding, action.id)
    assert preview.applicable is False
    assert any("cannot determine" in warning for warning in preview.warnings)

    with pytest.raises(DomainError) as excinfo:
        apply_corporate_action(session, holding, action.id, request_id="apply-spin-1")
    assert excinfo.value.code == "action_not_applicable"

    # The unapplied action must leave the position untouched.
    position = position_of(session, holding)
    assert position.quantity == Decimal("100")
    assert position.average_cost == Decimal("140")


def test_an_action_cannot_be_applied_twice(session, holding) -> None:
    action = record_corporate_action(
        session,
        request_id="split-3",
        instrument_reference="AAPL",
        action_type=ActionType.SPLIT,
        ex_date=EX_DATE,
        ratio=Decimal("2"),
        source="issuer announcement",
    )
    apply_corporate_action(session, holding, action.id, request_id="apply-once")

    with pytest.raises(DomainError) as excinfo:
        apply_corporate_action(session, holding, action.id, request_id="apply-twice")
    assert excinfo.value.code == "action_not_applicable"

    assert position_of(session, holding).quantity == Decimal("200"), "no double application"


def test_recording_an_action_is_idempotent(session, harness) -> None:
    harness.client.get("/api/v1/market/instruments/AAPL")
    kwargs = dict(
        request_id="dup-action",
        instrument_reference="AAPL",
        action_type=ActionType.SPLIT,
        ex_date=EX_DATE,
        ratio=Decimal("2"),
        source="issuer announcement",
    )
    first = record_corporate_action(session, **kwargs)
    second = record_corporate_action(session, **kwargs)
    assert first.id == second.id


def test_split_without_a_ratio_is_rejected(session) -> None:
    with pytest.raises(DomainError) as excinfo:
        record_corporate_action(
            session,
            request_id="bad-split",
            instrument_reference="AAPL",
            action_type=ActionType.SPLIT,
            ex_date=EX_DATE,
            source="issuer announcement",
        )
    assert excinfo.value.code == "missing_field"


def test_action_on_an_unheld_instrument_is_not_applicable(session, harness) -> None:
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    action = record_corporate_action(
        session,
        request_id="split-4",
        instrument_reference="AAPL",
        action_type=ActionType.SPLIT,
        ex_date=EX_DATE,
        ratio=Decimal("2"),
        source="issuer announcement",
    )
    preview = preview_application(session, portfolio_id, action.id)
    assert preview.applicable is False
    assert any("holds no AAPL position" in warning for warning in preview.warnings)


def test_split_round_trip_over_http(harness) -> None:
    """The full record -> preview -> apply flow through the API, as an agent would drive it."""
    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={"request_id": "d", "transaction_type": "deposit", "amount": "20000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "b",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "100",
            "unit_price": "140",
        },
    )

    recorded = harness.client.post(
        "/api/v1/corporate-actions",
        json={
            "request_id": "ca-http-1",
            "ticker": "AAPL",
            "action_type": "split",
            "ex_date": "2026-06-01T00:00:00Z",
            "ratio": "2",
            "source": "issuer announcement",
        },
    )
    assert recorded.status_code == 201, recorded.text
    action_id = recorded.json()["id"]
    assert recorded.json()["cost_basis_unresolved"] is False

    preview = harness.client.get(
        f"/api/v1/portfolios/{portfolio_id}/corporate-actions/{action_id}/preview"
    ).json()
    assert preview["applicable"] is True
    assert Decimal(preview["resulting_quantity"]) == Decimal("200")
    assert Decimal(preview["resulting_average_cost"]) == Decimal("70")

    # The preview must not have changed anything.
    before = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    assert Decimal(before["positions"][0]["quantity"]) == Decimal("100")

    applied = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/corporate-actions/{action_id}/apply",
        json={"request_id": "apply-http-1"},
    )
    assert applied.status_code == 201, applied.text

    summary = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    position = summary["positions"][0]
    assert Decimal(position["quantity"]) == Decimal("200")
    assert Decimal(position["average_cost"]) == Decimal("70")

    repeat = harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/corporate-actions/{action_id}/apply",
        json={"request_id": "apply-http-2"},
    )
    assert repeat.status_code == 422
    assert repeat.json()["code"] == "action_not_applicable"


def test_preview_does_not_write_anything(session, holding) -> None:
    action = record_corporate_action(
        session,
        request_id="split-5",
        instrument_reference="AAPL",
        action_type=ActionType.SPLIT,
        ex_date=EX_DATE,
        ratio=Decimal("2"),
        source="issuer announcement",
    )
    preview_application(session, holding, action.id)
    preview_application(session, holding, action.id)

    position = position_of(session, holding)
    assert position.quantity == Decimal("100"), "preview must not apply the action"
    assert position.average_cost == Decimal("140")
