"""Point-in-time snapshots: no look-ahead, no invented prices, no silent restatement.

The fake provider here returns a different close on every date, which is what makes look-ahead
detectable: if a snapshot ever used a later bar, the value it reports would be visibly wrong
rather than coincidentally right.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventType
from portfolio_manager.market import (
    HistoryAdjustment,
    HistoryBar,
    HistoryInterval,
    HistoryResult,
    MarketDataError,
)
from portfolio_manager.models import PositionValuationSnapshot
from portfolio_manager.postings import TransactionRequest, record_transaction
from portfolio_manager.valuation import (
    CALCULATION_VERSION,
    SnapshotStatus,
    create_snapshot,
    rebuild_snapshots,
    snapshot_warnings,
)

BUY_DATE = date(2026, 3, 2)
LATER = date(2026, 3, 6)


class DatedProvider:
    """Prices rise by 1 per day from a known base, so any date maps to a distinct price."""

    def __init__(self) -> None:
        self.base = {"AAPL": Decimal("100"), "MSFT": Decimal("400")}
        self.unpriceable: set[str] = set()
        self.requested_end_dates: list[date] = []

    def price_on(self, ticker: str, day: date) -> Decimal:
        return self.base[ticker] + Decimal((day - BUY_DATE).days)

    def fetch(self, ticker: str):  # pragma: no cover - snapshots must never call this
        raise AssertionError("valuation must not read the current quote")

    def history(
        self,
        ticker: str,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        interval: HistoryInterval = HistoryInterval.DAILY,
        adjustment: HistoryAdjustment = HistoryAdjustment.AUTO,
    ) -> HistoryResult:
        if ticker in self.unpriceable or ticker not in self.base:
            raise MarketDataError(f"No data for {ticker}")
        self.requested_end_dates.append(end_date)

        first = start_date or BUY_DATE
        last = end_date or LATER
        bars = []
        day = first
        while day <= last:
            price = self.price_on(ticker, day)
            bars.append(
                HistoryBar(
                    timestamp=datetime.combine(day, datetime.min.time(), UTC),
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
            provider="Dated Fake",
            interval=interval,
            adjustment=adjustment,
            requested_start_date=start_date,
            requested_end_date=end_date,
            fetched_at=datetime.now(UTC),
            warnings=[],
            bars=bars,
        )


@pytest.fixture
def provider() -> DatedProvider:
    return DatedProvider()


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


@pytest.fixture
def held(harness, session) -> str:
    """20,000 deposited, then 100 AAPL bought at 100 on BUY_DATE."""
    portfolio_id = harness.portfolio()
    harness.client.get("/api/v1/market/instruments/AAPL")
    record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id="dep",
            event_type=EventType.DEPOSIT,
            amount=Decimal("20000"),
            occurred_at=datetime.combine(BUY_DATE, datetime.min.time(), UTC),
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
            unit_price=Decimal("100"),
            occurred_at=datetime.combine(BUY_DATE, datetime.min.time(), UTC),
        ),
    )
    return portfolio_id


def positions_of(session, snapshot) -> list[PositionValuationSnapshot]:
    return list(
        session.scalars(
            __import__("sqlalchemy")
            .select(PositionValuationSnapshot)
            .where(PositionValuationSnapshot.portfolio_snapshot_id == snapshot.id)
        ).all()
    )


def test_snapshot_values_the_portfolio_on_that_date(session, held, provider) -> None:
    snapshot = create_snapshot(session, held, BUY_DATE, provider)

    assert snapshot.securities_value == Decimal("10000"), "100 shares at that day's close of 100"
    assert snapshot.cash_value == Decimal("10000")
    assert snapshot.total_value == Decimal("20000")
    assert snapshot.status == SnapshotStatus.COMPLETE
    assert snapshot.pricing_coverage_percent == Decimal("100")
    assert snapshot.calculation_version == CALCULATION_VERSION


def test_snapshot_never_uses_a_later_price(session, held, provider) -> None:
    """The property that makes a backfilled series honest rather than clairvoyant."""
    early = create_snapshot(session, held, BUY_DATE, provider)
    late = create_snapshot(session, held, LATER, provider)

    assert early.securities_value == Decimal("10000"), "the close on BUY_DATE, not four days on"
    assert late.securities_value == Decimal("10400")
    assert all(end <= LATER for end in provider.requested_end_dates)


def test_a_price_request_is_bounded_by_the_valuation_date(session, held, provider) -> None:
    create_snapshot(session, held, BUY_DATE, provider)
    assert provider.requested_end_dates == [BUY_DATE], "history must be cut at the valuation date"


def test_a_provider_returning_future_bars_is_still_not_trusted(session, held, provider) -> None:
    """Defense in depth: the pricer filters bars itself rather than trusting `end_date`."""

    class LeakyProvider(DatedProvider):
        def history(self, ticker, **kwargs):
            kwargs["end_date"] = LATER  # ignore the caller's cutoff
            return super().history(ticker, **kwargs)

    leaky = LeakyProvider()
    snapshot = create_snapshot(session, held, BUY_DATE, leaky)
    assert snapshot.securities_value == Decimal("10000"), "future bars must be discarded"


def test_an_unpriceable_holding_makes_the_snapshot_partial(session, held, provider) -> None:
    """A missing price is recorded as missing; zero would look like a real valuation."""
    provider.unpriceable.add("AAPL")
    snapshot = create_snapshot(session, held, BUY_DATE, provider)

    assert snapshot.status == SnapshotStatus.PARTIAL
    assert snapshot.securities_value == Decimal("0")
    assert snapshot.unpriced_market_value == Decimal("10000"), "carried at cost so it is visible"
    assert snapshot.positions_priced == 0
    assert snapshot.positions_total == 1
    assert snapshot.pricing_coverage_percent == Decimal("0")
    assert any("could not be priced" in w for w in snapshot_warnings(snapshot))

    row = positions_of(session, snapshot)[0]
    assert row.price is None and row.market_value is None, "unknown stays null, never zero"


def test_partial_coverage_is_proportional(session, harness, provider) -> None:
    portfolio_id = harness.portfolio()
    for ticker in ("AAPL", "MSFT"):
        harness.client.get(f"/api/v1/market/instruments/{ticker}")
    with harness.session_factory() as session:
        record_transaction(
            session,
            portfolio_id,
            TransactionRequest(
                request_id="dep",
                event_type=EventType.DEPOSIT,
                amount=Decimal("100000"),
                occurred_at=datetime.combine(BUY_DATE, datetime.min.time(), UTC),
            ),
        )
        for index, (ticker, price) in enumerate((("AAPL", "100"), ("MSFT", "400"))):
            record_transaction(
                session,
                portfolio_id,
                TransactionRequest(
                    request_id=f"buy-{index}",
                    event_type=EventType.BUY,
                    ticker=ticker,
                    quantity=Decimal("10"),
                    unit_price=Decimal(price),
                    occurred_at=datetime.combine(BUY_DATE, datetime.min.time(), UTC),
                ),
            )
        provider.unpriceable.add("MSFT")
        snapshot = create_snapshot(session, portfolio_id, BUY_DATE, provider)

    assert snapshot.positions_priced == 1
    assert snapshot.positions_total == 2
    assert snapshot.pricing_coverage_percent == Decimal("50")
    assert snapshot.securities_value == Decimal("1000"), "only the priced holding"
    assert snapshot.unpriced_market_value == Decimal("4000")


def test_a_stale_carried_forward_price_is_flagged(session, held) -> None:
    """A close carried forward across a long gap is usable but must not look current."""

    class GapProvider(DatedProvider):
        def history(self, ticker, **kwargs):
            result = super().history(ticker, **kwargs)
            keep = [bar for bar in result.bars if bar.timestamp.date() <= BUY_DATE]
            return HistoryResult(**{**result.__dict__, "bars": keep})

    far = BUY_DATE + timedelta(days=20)
    snapshot = create_snapshot(session, held, far, GapProvider())

    row = positions_of(session, snapshot)[0]
    assert row.price == Decimal("100"), "the last close before the gap"
    assert row.price_stale is True
    assert any("carried forward" in w for w in snapshot_warnings(snapshot))


@pytest.mark.parametrize(
    ("provider_value", "expected"),
    [
        # Real float32 round-trips observed in this portfolio's own Yahoo history.
        ("117.58000183105469", "117.58"),
        ("371.8999938964844", "371.9"),
        ("32.84000015258789", "32.84"),
        ("319.0899963378906", "319.09"),
        ("65432.109375", "65432.11"),
        # Genuine sub-cent precision must survive: FX and crypto trade far below a cent.
        ("0.0001234499941347167", "0.00012345"),
    ],
)
def test_float32_provider_noise_is_not_stored_as_price(
    session, held, provider, provider_value, expected
) -> None:
    """yfinance returns float32, so 117.58 arrives as 117.58000183105469.

    Those digits are the float32 round-trip, not price data. Storing them would make a snapshot
    look more precise than its source and pollute every value derived from it.
    """
    provider.base["AAPL"] = Decimal(provider_value)
    snapshot = create_snapshot(session, held, BUY_DATE, provider)

    row = positions_of(session, snapshot)[0]
    assert row.price == Decimal(expected), "provider noise must not survive into the record"


def test_a_cleaned_price_multiplies_out_exactly(session, held, provider) -> None:
    provider.base["AAPL"] = Decimal("117.58000183105469")
    snapshot = create_snapshot(session, held, BUY_DATE, provider)
    assert snapshot.securities_value == Decimal("11758"), "100 shares at a clean 117.58"


def test_repeating_a_snapshot_returns_the_stored_one(session, held, provider) -> None:
    """Idempotency: a retried job must not create a second version of the same day."""
    first = create_snapshot(session, held, BUY_DATE, provider)
    second = create_snapshot(session, held, BUY_DATE, provider)
    assert first.id == second.id


def test_forcing_a_revision_replaces_the_snapshot(session, held, provider) -> None:
    first = create_snapshot(session, held, BUY_DATE, provider)
    provider.base["AAPL"] = Decimal("111")
    second = create_snapshot(session, held, BUY_DATE, provider, force_revision=True)

    assert second.id != first.id
    assert second.securities_value == Decimal("11100")
    assert len(positions_of(session, second)) == 1, "the old position rows went with it"


def test_a_future_valuation_date_is_rejected(session, held, provider) -> None:
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    with pytest.raises(DomainError) as excinfo:
        create_snapshot(session, held, tomorrow, provider)
    assert excinfo.value.code == "valuation_date_in_future"


def test_todays_snapshot_is_capped_at_the_current_instant(session, held, provider) -> None:
    """A day still in progress is valued as far as it has got, not to a midnight that is ahead."""
    today = datetime.now(UTC).date()
    snapshot = create_snapshot(session, held, today, provider)

    now = datetime.now(UTC)
    as_of = snapshot.valuation_as_of
    as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    assert as_of <= now, "valuation_as_of must never sit in the future"


def test_a_snapshot_before_any_activity_is_empty_not_wrong(session, held, provider) -> None:
    snapshot = create_snapshot(session, held, BUY_DATE - timedelta(days=1), provider)
    assert snapshot.total_value == Decimal("0")
    assert snapshot.positions_total == 0
    assert snapshot.status == SnapshotStatus.COMPLETE, "nothing held is fully priced"


def test_an_exited_position_is_not_carried_into_later_snapshots(session, held, provider) -> None:
    record_transaction(
        session,
        held,
        TransactionRequest(
            request_id="sell-all",
            event_type=EventType.SELL,
            ticker="AAPL",
            quantity=Decimal("100"),
            unit_price=Decimal("120"),
            occurred_at=datetime.combine(BUY_DATE + timedelta(days=1), datetime.min.time(), UTC),
        ),
    )
    snapshot = create_snapshot(session, held, LATER, provider)
    assert snapshot.positions_total == 0
    assert snapshot.securities_value == Decimal("0")
    assert snapshot.cash_value == Decimal("22000")


def test_snapshot_carries_the_legacy_gap_forward(session, harness, provider) -> None:
    """A portfolio built from migrated rows must not present as fully reconciled."""
    from portfolio_manager.backfill import backfill_portfolio
    from portfolio_manager.models import Portfolio

    portfolio_id = harness.portfolio()
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/cash-transactions",
        json={"request_id": "c-1", "action": "deposit", "amount": "10000"},
    )
    harness.client.post(
        f"/api/v1/portfolios/{portfolio_id}/trades",
        json={
            "request_id": "t-1",
            "ticker": "AAPL",
            "side": "buy",
            "quantity": "10",
            "unit_price": "100",
        },
    )
    backfill_portfolio(session, session.get(Portfolio, portfolio_id))
    session.commit()

    snapshot = create_snapshot(session, portfolio_id, datetime.now(UTC).date(), provider)
    assert snapshot.has_unlinked_legacy_events is True
    assert any("has not been inferred" in w for w in snapshot_warnings(snapshot))


def test_rebuild_covers_every_day_in_the_range(session, held, provider) -> None:
    report = rebuild_snapshots(session, held, BUY_DATE, LATER, provider)

    assert report.created == 5, "March 2 through 6 inclusive"
    assert report.failed == []
    assert report.skipped_existing == 0


def test_rebuild_is_resumable(session, held, provider) -> None:
    """Re-running after an interruption fills only the gaps rather than rewriting history."""
    create_snapshot(session, held, BUY_DATE, provider)
    report = rebuild_snapshots(session, held, BUY_DATE, LATER, provider)

    assert report.skipped_existing == 1
    assert report.created == 4


def test_rebuild_prices_each_day_independently(session, held, provider) -> None:
    rebuild_snapshots(session, held, BUY_DATE, LATER, provider)

    import sqlalchemy as sa

    from portfolio_manager.models import PortfolioValuationSnapshot

    values = list(
        session.scalars(
            sa.select(PortfolioValuationSnapshot.securities_value)
            .where(PortfolioValuationSnapshot.portfolio_id == held)
            .order_by(PortfolioValuationSnapshot.valuation_date)
        ).all()
    )
    assert values == [Decimal(str(10000 + 100 * day)) for day in range(5)]


def test_rebuild_stops_at_today(session, held, provider) -> None:
    future = datetime.now(UTC).date() + timedelta(days=5)
    report = rebuild_snapshots(session, held, BUY_DATE, future, provider)
    assert any("in the future" in warning for warning in report.warnings)


def test_rebuild_rejects_an_inverted_range(session, held, provider) -> None:
    with pytest.raises(DomainError) as excinfo:
        rebuild_snapshots(session, held, LATER, BUY_DATE, provider)
    assert excinfo.value.code == "invalid_date_range"


def test_rebuild_reports_partial_days(session, held, provider) -> None:
    provider.unpriceable.add("AAPL")
    report = rebuild_snapshots(session, held, BUY_DATE, LATER, provider)

    assert report.partial == 5
    assert any("are partial" in warning for warning in report.warnings)


def test_rebuild_is_deterministic(session, held, provider) -> None:
    """Same journal and same version must rebuild to the same numbers (plan principle 10)."""
    first = rebuild_snapshots(session, held, BUY_DATE, LATER, provider)
    second = rebuild_snapshots(session, held, BUY_DATE, LATER, provider, force_revision=True)

    assert first.created == second.created
    import sqlalchemy as sa

    from portfolio_manager.models import PortfolioValuationSnapshot

    values = list(
        session.scalars(
            sa.select(PortfolioValuationSnapshot.total_value)
            .where(PortfolioValuationSnapshot.portfolio_id == held)
            .order_by(PortfolioValuationSnapshot.valuation_date)
        ).all()
    )
    assert values == [Decimal(str(20000 + 100 * day)) for day in range(5)]
