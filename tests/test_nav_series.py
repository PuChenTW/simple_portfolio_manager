"""A group's value over time.

The load-bearing property is that the line says what it is a sum of. The series charts the
group's current members across the whole history of their data, so it rises when an account's
records begin -- a rise indistinguishable from a gain unless every point declares how many
accounts it covers and the response declares when each one entered. Most of these tests exist to
pin that down, because the failure mode is a plausible-looking chart rather than an exception.

The second property is the one shared with every other reported number here: value that could
not be converted is excluded and visible, never converted at a guessed rate.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_manager.consolidation import create_group
from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventType, PortfolioKind
from portfolio_manager.market import (
    HistoryAdjustment,
    HistoryBar,
    HistoryInterval,
    HistoryResult,
    MarketDataError,
)
from portfolio_manager.models import Portfolio
from portfolio_manager.nav_series import build_nav_series
from portfolio_manager.postings import TransactionRequest, record_transaction
from portfolio_manager.valuation import SnapshotStatus, create_snapshot

DAY = datetime.now(UTC).date()


class SeriesProvider:
    """Prices two listings and one FX pair, flat across history so sums are exact.

    Flat prices are the point: a moving price would make every assertion about a total depend on
    which date the bar came from, and these tests are about composition, not pricing.
    """

    def __init__(self) -> None:
        self.values = {
            "AAPL": Decimal("200"),  # USD
            "2330.TW": Decimal("1000"),  # TWD
            "USDTWD=X": Decimal("32"),
        }
        self.unavailable: set[str] = set()
        self.history_calls: list[tuple[str, date | None, date | None]] = []

    def fetch(self, ticker: str):  # pragma: no cover - a series is point-in-time throughout
        raise AssertionError("a nav series must not read the current quote")

    def history(self, ticker, days=None, start_date=None, end_date=None, **kwargs):
        self.history_calls.append((ticker, start_date, end_date))
        if ticker in self.unavailable or ticker not in self.values:
            raise MarketDataError(f"No data for {ticker}")
        value = self.values[ticker]
        last = end_date or DAY
        first = start_date or (last - timedelta(days=30))
        span = max((last - first).days, 0)
        bars = [
            HistoryBar(
                timestamp=datetime.combine(
                    first + timedelta(days=offset), datetime.min.time(), UTC
                ),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=Decimal("1"),
            )
            for offset in range(span + 1)
        ]
        return HistoryResult(
            ticker=ticker,
            provider="Series Fake",
            interval=kwargs.get("interval", HistoryInterval.DAILY),
            adjustment=kwargs.get("adjustment", HistoryAdjustment.AUTO),
            requested_start_date=start_date,
            requested_end_date=end_date,
            fetched_at=datetime.now(UTC),
            warnings=[],
            bars=bars,
        )


@pytest.fixture
def provider() -> SeriesProvider:
    return SeriesProvider()


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC)


def snapshot_range(session, portfolio_id: str, provider, first: date, last: date) -> None:
    """Build a snapshot for every date in a range, the way the cron eventually would."""
    for offset in range((last - first).days + 1):
        create_snapshot(session, portfolio_id, first + timedelta(days=offset), provider)


@pytest.fixture
def staggered(harness, session, provider):
    """Two portfolios whose data starts five days apart, in different currencies.

    This is the shape that makes the series hard to read honestly: `old` has history the group
    did not, and `young` appears partway through, lifting the total without anyone earning
    anything.
    """
    old = harness.portfolio("US brokerage", "USD")
    young = harness.portfolio("TW brokerage", "TWD")
    for ticker in ("AAPL", "2330.TW"):
        harness.client.get(f"/api/v1/market/instruments/{ticker}")

    old_start = DAY - timedelta(days=9)
    young_start = DAY - timedelta(days=4)

    record_transaction(session, old, TransactionRequest(
        request_id="d1", event_type=EventType.DEPOSIT,
        amount=Decimal("5000"), occurred_at=at(old_start)))
    record_transaction(session, old, TransactionRequest(
        request_id="b1", event_type=EventType.BUY, ticker="AAPL",
        quantity=Decimal("10"), unit_price=Decimal("200"), occurred_at=at(old_start)))

    record_transaction(session, young, TransactionRequest(
        request_id="d2", event_type=EventType.DEPOSIT,
        amount=Decimal("100000"), occurred_at=at(young_start)))
    record_transaction(session, young, TransactionRequest(
        request_id="b2", event_type=EventType.BUY, ticker="2330.TW",
        quantity=Decimal("5"), unit_price=Decimal("1000"), occurred_at=at(young_start)))

    snapshot_range(session, old, provider, old_start, DAY)
    snapshot_range(session, young, provider, young_start, DAY)

    group = create_group(session, "Everything", "TWD", [old, young])
    session.commit()
    return group.id, old, young, old_start, young_start


def test_the_series_spans_every_date_that_has_a_snapshot(session, staggered, provider) -> None:
    """Omitting both dates asks for the whole history without first discovering where it begins."""
    group_id, _, _, old_start, _ = staggered

    series = build_nav_series(session, group_id, provider)

    assert series.start_date == old_start
    assert series.end_date == DAY
    assert len(series.points) == (DAY - old_start).days + 1
    assert series.reporting_currency == "TWD"


def test_a_point_counts_only_the_accounts_inside_it(session, staggered, provider) -> None:
    """The count is what stops a reader comparing two dates that sum different accounts."""
    group_id, _, _, old_start, young_start = staggered

    series = build_nav_series(session, group_id, provider)
    by_date = {point.valuation_date: point for point in series.points}

    assert by_date[old_start].contributing_account_count == 1
    assert by_date[young_start].contributing_account_count == 2
    assert series.total_account_count == 2


def test_an_account_before_its_first_event_is_not_a_gap(session, staggered, provider) -> None:
    """It has no data to be missing, so flagging it would warn on dates nobody can act on."""
    group_id, _, young, old_start, young_start = staggered

    series = build_nav_series(session, group_id, provider)
    by_date = {point.valuation_date: point for point in series.points}

    early = by_date[old_start]
    assert early.status == SnapshotStatus.COMPLETE
    assert early.missing_portfolio_ids == []
    assert young not in early.missing_portfolio_ids
    assert series.missing_dates == []
    assert series.partial_points == 0


def test_a_started_account_with_no_snapshot_makes_the_point_partial(
    harness, session, staggered, provider
) -> None:
    """That is a real gap: the account had begun, so the total understates the group."""
    group_id, _, young, _, young_start = staggered

    from portfolio_manager.models import PortfolioValuationSnapshot

    victim = young_start + timedelta(days=1)
    row = session.query(PortfolioValuationSnapshot).filter(
        PortfolioValuationSnapshot.portfolio_id == young,
        PortfolioValuationSnapshot.valuation_date == at(victim),
    ).one()
    session.delete(row)
    session.commit()

    series = build_nav_series(session, group_id, provider)
    by_date = {point.valuation_date: point for point in series.points}

    assert by_date[victim].status == SnapshotStatus.PARTIAL
    assert by_date[victim].missing_portfolio_ids == [young]
    assert series.partial_points == 1
    assert any("understate" in warning for warning in series.warnings)


def test_the_series_reports_when_each_account_entered(session, staggered, provider) -> None:
    """These are the dates where the line changes what it measures, so they are the chart's key."""
    group_id, old, young, old_start, young_start = staggered

    series = build_nav_series(session, group_id, provider)

    assert [(item.portfolio_id, item.entered_on) for item in series.account_entries] == [
        (old, old_start),
        (young, young_start),
    ]


def test_a_changing_account_count_is_warned_about(session, staggered, provider) -> None:
    """The one way this chart misleads, so it must be stated rather than left to be noticed."""
    group_id, _, _, _, _ = staggered

    series = build_nav_series(session, group_id, provider)

    assert any(
        "changing set of accounts" in warning and "not a gain" in warning
        for warning in series.warnings
    )


def test_values_convert_at_each_date_own_rate(session, staggered, provider) -> None:
    """10 AAPL at 200 USD is 2000 USD, which at 32 is 64000 TWD, plus 5000 TWD of TWD cash."""
    group_id, _, _, _, young_start = staggered

    series = build_nav_series(session, group_id, provider)
    by_date = {point.valuation_date: point for point in series.points}

    # Before the TW account starts: 2000 USD of AAPL plus 3000 USD of cash, all at 32.
    early = by_date[young_start - timedelta(days=1)]
    assert early.net_value == Decimal("5000") * Decimal("32")

    # After: the TWD account adds 5000 TWD of 2330 plus 95000 TWD cash, unconverted.
    later = by_date[young_start]
    assert later.net_value == Decimal("5000") * Decimal("32") + Decimal("100000")
    assert later.assets_value == later.net_value
    assert later.liabilities_value == Decimal("0")


def test_assets_and_liabilities_reconcile_to_net(harness, session, provider) -> None:
    """A liability's stored total is already negative, so the split must still sum to net."""
    cash = harness.portfolio("Savings", "TWD")
    with harness.session_factory() as other:
        row = other.get(Portfolio, cash)
        row.kind = PortfolioKind.CASH.value
        other.commit()
    loan = harness.liability_account("Bank loan", "TWD")

    start = DAY - timedelta(days=2)
    record_transaction(session, cash, TransactionRequest(
        request_id="c1", event_type=EventType.DEPOSIT,
        amount=Decimal("500000"), occurred_at=at(start)))
    record_transaction(session, loan, TransactionRequest(
        request_id="l1", event_type=EventType.WITHDRAWAL,
        amount=Decimal("300000"), occurred_at=at(start)))

    snapshot_range(session, cash, provider, start, DAY)
    snapshot_range(session, loan, provider, start, DAY)
    group = create_group(session, "Net", "TWD", [cash, loan])
    session.commit()

    series = build_nav_series(session, group.id, provider)
    point = series.points[-1]

    assert point.assets_value == Decimal("500000")
    assert point.liabilities_value == Decimal("-300000")
    assert point.net_value == Decimal("200000")
    assert point.assets_value + point.liabilities_value == point.net_value


def test_an_unresolvable_rate_excludes_value_rather_than_guessing(
    session, staggered, provider
) -> None:
    """Invariant 4: the amount stays in its own currency and the shortfall is reported."""
    group_id, _, _, _, _ = staggered
    provider.unavailable.add("USDTWD=X")

    series = build_nav_series(session, group_id, provider)
    point = series.points[-1]

    assert point.unconverted, "the USD account cannot reach TWD and must be listed"
    assert point.unconverted[0].currency == "USD"
    assert point.converted_value_coverage_percent < Decimal("100")
    assert point.contributing_account_count == 1, "only the TWD account reached the total"
    assert any("could not be converted" in warning for warning in series.warnings)


def test_current_membership_charts_history_the_group_predates(
    session, staggered, provider
) -> None:
    """The deliberate divergence from `get_consolidated_summary`.

    The group is created today, so effective-dated membership would report nothing for any of
    these dates. This endpoint charts them, which is the whole reason it exists.
    """
    group_id, _, _, old_start, _ = staggered

    series = build_nav_series(session, group_id, provider)

    assert series.points[0].valuation_date == old_start
    assert series.points[0].net_value > Decimal("0")


def test_rates_are_fetched_once_for_the_range_not_once_per_date(
    session, staggered, provider
) -> None:
    """Performance is a correctness property here: a 3-year series is 1000+ dates.

    `FxService` fetches a 30-day window around whatever date it is asked for, so without the
    range prefetch this would ask the provider for the same pair once per point.
    """
    group_id, _, _, _, _ = staggered
    provider.history_calls.clear()

    series = build_nav_series(session, group_id, provider)

    fx_calls = [call for call in provider.history_calls if call[0] == "USDTWD=X"]
    assert len(fx_calls) == 1, f"one fetch for the whole range, got {len(fx_calls)}"
    assert len(series.points) > 1, "and it covered more than one date"


def test_a_date_with_no_bar_inside_a_prefetched_range_does_not_refetch(
    session, staggered, provider
) -> None:
    """The refetch loop that made a three-year series take 36 seconds instead of 1.6.

    `_observed` refetches whenever the newest stored rate is older than the date asked for --
    right for a single report, and catastrophic for a series: every weekend and holiday looks
    like a cache that fell short, so the same 30-day window is fetched again once per date. The
    prefetch records what it covered so those dates resolve from what is already stored.
    """
    group_id, _, _, _, _ = staggered
    provider.history_calls.clear()

    build_nav_series(session, group_id, provider)
    first_pass = len([call for call in provider.history_calls if call[0] == "USDTWD=X"])

    # Every point after the first asks for a date whose own bar may not exist. If any of them
    # refetched, this count would grow with the number of points rather than stay at one.
    assert first_pass == 1


def test_an_explicit_range_narrows_the_series(session, staggered, provider) -> None:
    group_id, _, _, _, young_start = staggered

    series = build_nav_series(
        session, group_id, provider, start_date=young_start, end_date=DAY
    )

    assert series.start_date == young_start
    assert [point.valuation_date for point in series.points][0] == young_start
    assert all(point.contributing_account_count == 2 for point in series.points)


def test_a_reversed_range_is_refused(session, staggered, provider) -> None:
    group_id, _, _, _, _ = staggered

    with pytest.raises(DomainError) as raised:
        build_nav_series(
            session, group_id, provider, start_date=DAY, end_date=DAY - timedelta(days=5)
        )

    assert raised.value.code == "invalid_date_range"


def test_a_group_with_no_snapshots_says_so_rather_than_returning_an_empty_line(
    harness, session, provider
) -> None:
    """An empty chart and an unbuilt one look identical, so the response has to distinguish them."""
    portfolio = harness.portfolio("Fresh", "TWD")
    group = create_group(session, "New", "TWD", [portfolio])
    session.commit()

    series = build_nav_series(session, group.id, provider)

    assert series.points == []
    assert any("rebuild_valuation_snapshots" in warning for warning in series.warnings)


def test_the_endpoint_returns_the_series(harness, session, staggered, provider) -> None:
    """The HTTP surface, including that it reads and never builds."""
    from portfolio_manager.api import app, get_market_provider

    group_id, _, _, old_start, _ = staggered
    # The harness fake prices instruments but no FX pair, so the USD account would arrive
    # unconverted and the counts under test would all be one lower for the wrong reason.
    app.dependency_overrides[get_market_provider] = lambda: provider

    response = harness.client.get(f"/api/v1/portfolio-groups/{group_id}/nav-history")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reporting_currency"] == "TWD"
    assert body["start_date"] == old_start.isoformat()
    assert body["total_account_count"] == 2
    assert len(body["account_entries"]) == 2
    assert body["points"][0]["contributing_account_count"] == 1
    assert "net_value" in body["points"][0]
    assert "assets_value" in body["points"][0]
    assert "liabilities_value" in body["points"][0]
