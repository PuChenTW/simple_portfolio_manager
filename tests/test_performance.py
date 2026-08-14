"""TWR and XIRR: what the holdings did, versus what the investor earned.

The governing test is that a large mid-period deposit leaves TWR untouched while moving XIRR.
If a deposit changes TWR, the implementation is measuring the size of the portfolio rather than
its performance -- which is exactly the error that makes `total_pnl / cost_basis` unusable.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventType
from portfolio_manager.performance import (
    TWR_METHOD,
    _daily_returns,
    calculate_performance,
)
from portfolio_manager.postings import TransactionRequest, record_transaction
from portfolio_manager.valuation import create_snapshot

DAY0 = date(2026, 3, 2)


def at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC)


class StepProvider:
    """A price series the test controls exactly, so returns are arithmetic, not luck."""

    def __init__(self) -> None:
        self.prices: dict[date, Decimal] = {}
        self.default = Decimal("100")

    def price_on(self, day: date) -> Decimal:
        return self.prices.get(day, self.default)

    def fetch(self, ticker: str):  # pragma: no cover - valuation must not read live quotes
        raise AssertionError("performance must not read the current quote")

    def history(self, ticker: str, days=None, start_date=None, end_date=None, **kwargs):
        from portfolio_manager.market import (
            HistoryAdjustment,
            HistoryBar,
            HistoryInterval,
            HistoryResult,
        )

        first = start_date or DAY0
        last = end_date or DAY0
        bars, day = [], first
        while day <= last:
            price = self.price_on(day)
            bars.append(
                HistoryBar(
                    timestamp=at(day),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=Decimal("1000"),
                )
            )
            day += timedelta(days=1)
        return HistoryResult(
            ticker=ticker,
            provider="Step Fake",
            interval=kwargs.get("interval", HistoryInterval.DAILY),
            adjustment=kwargs.get("adjustment", HistoryAdjustment.AUTO),
            requested_start_date=start_date,
            requested_end_date=end_date,
            fetched_at=datetime.now(UTC),
            warnings=[],
            bars=bars,
        )


@pytest.fixture
def provider() -> StepProvider:
    return StepProvider()


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def build(session, harness, provider, *, events, days: int) -> str:
    """Post `events`, then snapshot every day from DAY0 for `days`."""
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    for request in events:
        record_transaction(session, portfolio_id, request)
    for offset in range(days):
        create_snapshot(session, portfolio_id, DAY0 + timedelta(days=offset), provider)
    return portfolio_id


def test_twr_matches_nav_growth_when_no_money_moves(session, harness, provider) -> None:
    """With no external flow, TWR is simply how much the portfolio grew."""
    provider.prices = {DAY0: Decimal("100"), DAY0 + timedelta(days=1): Decimal("110")}
    portfolio_id = build(
        session,
        harness,
        provider,
        days=2,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="b", event_type=EventType.BUY, ticker="AAPL",
                quantity=Decimal("100"), unit_price=Decimal("100"), occurred_at=at(DAY0),
            ),
        ],
    )

    result = calculate_performance(session, portfolio_id, DAY0, DAY0 + timedelta(days=1))

    # 10,000 -> 11,000 on a portfolio holding 100 shares that rose from 100 to 110.
    assert result.beginning_value == Decimal("10000")
    assert result.ending_value == Decimal("11000")
    assert result.twr_percent == Decimal("10")
    assert result.twr_method == TWR_METHOD


def test_a_mid_period_deposit_does_not_distort_twr(session, harness, provider) -> None:
    """The property that separates TWR from a naive value change.

    The portfolio rises 10% on day one, then a 100,000 deposit lands with no further price move.
    A measure that divided by contributed capital would report a huge gain or loss; TWR reports
    the 10% the holdings actually earned.
    """
    day1, day2 = DAY0 + timedelta(days=1), DAY0 + timedelta(days=2)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("110"), day2: Decimal("110")}
    portfolio_id = build(
        session,
        harness,
        provider,
        days=3,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="b", event_type=EventType.BUY, ticker="AAPL",
                quantity=Decimal("100"), unit_price=Decimal("100"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="big", event_type=EventType.DEPOSIT,
                amount=Decimal("100000"), occurred_at=at(day2),
            ),
        ],
    )

    result = calculate_performance(session, portfolio_id, DAY0, day2)

    assert result.twr_percent == Decimal("10"), "the deposit must not register as performance"
    assert result.external_inflows == Decimal("100000")
    assert result.ending_value == Decimal("111000")


def test_xirr_reflects_when_the_money_arrived(session, harness, provider) -> None:
    """XIRR answers the other question: what the investor earned on capital at risk."""
    day1 = DAY0 + timedelta(days=1)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("110")}
    portfolio_id = build(
        session,
        harness,
        provider,
        days=2,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="b", event_type=EventType.BUY, ticker="AAPL",
                quantity=Decimal("100"), unit_price=Decimal("100"), occurred_at=at(DAY0),
            ),
        ],
    )

    result = calculate_performance(session, portfolio_id, DAY0, day1)

    assert result.xirr_percent is not None
    assert result.xirr_unavailable_reason is None
    # 10% in a single day annualizes to an enormous rate; the sign and scale are what matter.
    assert result.xirr_percent > Decimal("1000")


def test_a_dividend_is_return_not_contribution(session, harness, provider) -> None:
    """Plan 3.8: income must not be mistaken for investor capital."""
    day1 = DAY0 + timedelta(days=1)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("100")}
    portfolio_id = build(
        session,
        harness,
        provider,
        days=2,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="b", event_type=EventType.BUY, ticker="AAPL",
                quantity=Decimal("100"), unit_price=Decimal("100"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="div", event_type=EventType.DIVIDEND, ticker="AAPL",
                amount=Decimal("500"), occurred_at=at(day1),
            ),
        ],
    )

    result = calculate_performance(session, portfolio_id, DAY0, day1)

    assert result.external_inflows == Decimal("0"), "a dividend is not a contribution"
    assert result.income == Decimal("500")
    assert result.twr_percent == Decimal("5"), "the dividend is return the portfolio earned"


def test_a_withdrawal_does_not_read_as_a_loss(session, harness, provider) -> None:
    day1 = DAY0 + timedelta(days=1)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("100")}
    portfolio_id = build(
        session,
        harness,
        provider,
        days=2,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="w", event_type=EventType.WITHDRAWAL,
                amount=Decimal("4000"), occurred_at=at(day1),
            ),
        ],
    )

    result = calculate_performance(session, portfolio_id, DAY0, day1)

    assert result.external_outflows == Decimal("-4000")
    assert result.twr_percent == Decimal("0"), "taking money out is not a loss"


def test_returns_are_reported_with_their_method_and_version(session, harness, provider) -> None:
    """Plan 3.5: a return is meaningless without the convention that produced it."""
    day1 = DAY0 + timedelta(days=1)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("105")}
    portfolio_id = build(
        session, harness, provider, days=2,
        events=[TransactionRequest(
            request_id="d", event_type=EventType.DEPOSIT,
            amount=Decimal("1000"), occurred_at=at(DAY0),
        )],
    )
    result = calculate_performance(session, portfolio_id, DAY0, day1)

    assert result.twr_method == "modified-dietz-daily-v1"
    assert result.xirr_method.startswith("newton-bisection")
    assert result.calculation_version


def test_a_short_period_is_not_annualized(session, harness, provider) -> None:
    """Annualizing a two-day return would turn noise into an authoritative-looking number."""
    day1 = DAY0 + timedelta(days=1)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("101")}
    portfolio_id = build(
        session, harness, provider, days=2,
        events=[TransactionRequest(
            request_id="d", event_type=EventType.DEPOSIT,
            amount=Decimal("1000"), occurred_at=at(DAY0),
        )],
    )
    result = calculate_performance(session, portfolio_id, DAY0, day1)
    assert result.annualized_twr_percent is None


def test_a_period_past_a_month_is_annualized(session, harness, provider) -> None:
    """Every other test here spans two days and stops at the under-a-month guard, so the
    annualization arithmetic itself only runs once a period is long enough to reach it."""
    days = 31
    last = DAY0 + timedelta(days=days - 1)
    provider.prices = {
        DAY0 + timedelta(days=offset): Decimal("100") + offset for offset in range(days)
    }
    portfolio_id = build(
        session, harness, provider, days=days,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="b", event_type=EventType.BUY, ticker="AAPL",
                quantity=Decimal("100"), unit_price=Decimal("100"), occurred_at=at(DAY0),
            ),
        ],
    )

    result = calculate_performance(session, portfolio_id, DAY0, last)

    assert result.twr_percent is not None and result.twr_percent > Decimal("0")
    # A month of gains scales to a much larger annual figure; the exact rate is the method's
    # business, but it must be a real number rather than an exception.
    assert result.annualized_twr_percent is not None
    assert result.annualized_twr_percent > result.twr_percent


def test_a_gap_in_the_series_is_reported(session, harness, provider) -> None:
    """Plan principle 3: an incomplete series is named, not quietly interpolated."""
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id="d", event_type=EventType.DEPOSIT,
            amount=Decimal("1000"), occurred_at=at(DAY0),
        ),
    )
    create_snapshot(session, portfolio_id, DAY0, provider)
    create_snapshot(session, portfolio_id, DAY0 + timedelta(days=3), provider)

    result = calculate_performance(session, portfolio_id, DAY0, DAY0 + timedelta(days=3))

    assert len(result.coverage.missing_dates) == 2
    assert result.coverage.is_reliable is False
    assert any("no snapshot" in warning for warning in result.coverage.warnings)


def test_an_unclassifiable_flow_makes_the_result_unreliable(session, harness, provider) -> None:
    """Cash that lands in neither the capital base nor the return biases both measures.

    A reversal is classified by the event it undoes. When that original is not in the replay --
    here, an id that resolves to nothing -- the type gives no answer, and the honest report is
    that the period could not be measured cleanly rather than a number that looks settled.
    """
    from portfolio_manager.models import JournalEvent

    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "c-1",
            "transaction_type": "deposit",
            "amount": "1000",
            "occurred_at": at(DAY0).isoformat(),
        },
    )
    # Make the event unclassifiable the way an orphaned reversal is: a type nothing resolves.
    event = session.scalars(select(JournalEvent)).one()
    event.event_type = "not_a_known_type"
    session.commit()

    for offset in range(2):
        create_snapshot(session, portfolio_id, DAY0 + timedelta(days=offset), provider)

    result = calculate_performance(session, portfolio_id, DAY0, DAY0 + timedelta(days=1))

    assert result.coverage.unclassified_flow_events >= 1
    assert result.coverage.is_reliable is False
    assert any("could not be classified" in warning for warning in result.coverage.warnings)


def test_a_fully_journaled_portfolio_reports_no_flow_gaps(session, harness, provider) -> None:
    """A warning nobody can ever clear teaches people to ignore the ones that matter.

    Every event posted through the journal carries a type that classifies itself, so an ordinary
    portfolio must come back clean rather than carrying a permanent caveat.
    """
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "c-1",
            "transaction_type": "deposit",
            "amount": "5000",
            "occurred_at": at(DAY0).isoformat(),
        },
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        json={
            "request_id": "t-1",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "100",
            "occurred_at": at(DAY0).isoformat(),
        },
    )
    for offset in range(2):
        create_snapshot(session, portfolio_id, DAY0 + timedelta(days=offset), provider)

    result = calculate_performance(session, portfolio_id, DAY0, DAY0 + timedelta(days=1))

    assert result.coverage.unclassified_flow_events == 0
    assert result.coverage.is_reliable, "nothing here biases the return"


def test_a_period_without_two_snapshots_returns_null_with_a_reason(
    session, harness, provider
) -> None:
    portfolio_id = harness.portfolio()
    create_snapshot(session, portfolio_id, DAY0, provider)

    result = calculate_performance(session, portfolio_id, DAY0, DAY0)

    assert result.twr_percent is None
    assert result.xirr_percent is None
    assert result.xirr_unavailable_reason
    assert any("both ends" in warning for warning in result.coverage.warnings)


def test_xirr_explains_itself_when_all_flows_share_a_sign(session, harness, provider) -> None:
    """Plan 3.6: no real solution returns null plus a reason, never an opaque failure."""
    portfolio_id = harness.portfolio()
    for offset in range(2):
        create_snapshot(session, portfolio_id, DAY0 + timedelta(days=offset), provider)

    result = calculate_performance(session, portfolio_id, DAY0, DAY0 + timedelta(days=1))

    assert result.xirr_percent is None
    assert result.xirr_unavailable_reason is not None


def test_an_inverted_range_is_rejected(session, harness, provider) -> None:
    portfolio_id = harness.portfolio()
    with pytest.raises(DomainError) as excinfo:
        calculate_performance(session, portfolio_id, DAY0 + timedelta(days=1), DAY0)
    assert excinfo.value.code == "invalid_date_range"


def test_daily_returns_are_available_on_request(session, harness, provider) -> None:
    day1 = DAY0 + timedelta(days=1)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("110")}
    portfolio_id = build(
        session, harness, provider, days=2,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="b", event_type=EventType.BUY, ticker="AAPL",
                quantity=Decimal("100"), unit_price=Decimal("100"), occurred_at=at(DAY0),
            ),
        ],
    )

    result = calculate_performance(session, portfolio_id, DAY0, day1, include_daily=True)
    assert len(result.daily_returns) == 1
    assert result.daily_returns[0].return_percent == Decimal("10")

    without = calculate_performance(session, portfolio_id, DAY0, day1)
    assert without.daily_returns == []


def test_the_same_data_returns_the_same_numbers(session, harness, provider) -> None:
    """Plan principle 10: a rerun over unchanged data must reproduce the result exactly."""
    day1 = DAY0 + timedelta(days=1)
    provider.prices = {DAY0: Decimal("100"), day1: Decimal("107")}
    portfolio_id = build(
        session, harness, provider, days=2,
        events=[
            TransactionRequest(
                request_id="d", event_type=EventType.DEPOSIT,
                amount=Decimal("10000"), occurred_at=at(DAY0),
            ),
            TransactionRequest(
                request_id="b", event_type=EventType.BUY, ticker="AAPL",
                quantity=Decimal("100"), unit_price=Decimal("100"), occurred_at=at(DAY0),
            ),
        ],
    )

    first = calculate_performance(session, portfolio_id, DAY0, day1)
    second = calculate_performance(session, portfolio_id, DAY0, day1)
    assert (first.twr_percent, first.xirr_percent) == (second.twr_percent, second.xirr_percent)


def test_a_negative_base_yields_no_return_rather_than_an_inverted_one() -> None:
    """A negative denominator divides cleanly and reports the opposite of the truth.

    Modified Dietz divides the gain by the capital that produced it. When that base is negative
    -- an overdrawn book, or a liability if one were ever measured -- the division still succeeds
    and silently flips the sign: a balance recovering from -1,000,000 to -950,000 is a gain of
    50,000, yet `gain / denominator` reports -5%. Only `== ZERO` was guarded before, so the wrong
    number was returned in place of no number.
    """
    snapshots = [
        SimpleNamespace(
            total_value=Decimal("-1000000"),
            valuation_date=datetime.combine(DAY0, datetime.min.time(), tzinfo=UTC),
        ),
        SimpleNamespace(
            total_value=Decimal("-950000"),
            valuation_date=datetime.combine(
                DAY0 + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            ),
        ),
    ]

    daily = _daily_returns(snapshots, [Decimal("0")])

    assert [row.return_percent for row in daily] == [None]
