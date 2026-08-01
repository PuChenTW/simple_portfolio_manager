"""Consolidating portfolios across currencies.

The load-bearing property is that value which cannot be converted is visible. It is excluded
from the totals, listed in `unconverted`, and reflected in the coverage percentage -- never
converted at a guessed rate, and never quietly dropped so the total looks tidy.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_manager.consolidation import (
    build_consolidated_summary,
    create_group,
    delete_group,
    member_portfolio_ids,
    replace_members,
)
from portfolio_manager.errors import DomainError
from portfolio_manager.journal import EventType
from portfolio_manager.market import (
    HistoryAdjustment,
    HistoryBar,
    HistoryInterval,
    HistoryResult,
    MarketDataError,
)
from portfolio_manager.postings import TransactionRequest, record_transaction

# Portfolios are created by the harness "now", so the report date must not precede
# their inception; membership cannot start before the portfolio exists.
DAY = datetime.now(UTC).date()


class MixedProvider:
    """Prices two listings and one FX pair, so cross-currency behaviour is exact."""

    def __init__(self) -> None:
        self.values = {
            "AAPL": Decimal("200"),      # USD
            "2330.TW": Decimal("1000"),  # TWD
            "USDTWD=X": Decimal("32"),
        }
        self.unavailable: set[str] = set()

    def fetch(self, ticker: str):  # pragma: no cover - consolidation is point-in-time
        raise AssertionError("consolidation must not read the current quote")

    def history(self, ticker, days=None, start_date=None, end_date=None, **kwargs):
        if ticker in self.unavailable or ticker not in self.values:
            raise MarketDataError(f"No data for {ticker}")
        value = self.values[ticker]
        last = end_date or DAY
        bars = [
            HistoryBar(
                timestamp=datetime.combine(last, datetime.min.time(), UTC),
                open=value, high=value, low=value, close=value, volume=Decimal("1"),
            )
        ]
        return HistoryResult(
            ticker=ticker, provider="Mixed Fake",
            interval=kwargs.get("interval", HistoryInterval.DAILY),
            adjustment=kwargs.get("adjustment", HistoryAdjustment.AUTO),
            requested_start_date=start_date, requested_end_date=end_date,
            fetched_at=datetime.now(UTC), warnings=[], bars=bars,
        )


@pytest.fixture
def provider() -> MixedProvider:
    return MixedProvider()


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC)


@pytest.fixture
def two_portfolios(harness, session) -> tuple[str, str]:
    """A USD portfolio holding 10 AAPL and a TWD portfolio holding 5 of 2330."""
    usd = harness.portfolio("US", "USD")
    twd = harness.portfolio("TW", "TWD")
    for ticker in ("AAPL", "2330.TW"):
        harness.client.get(f"/api/v1/market/instruments/{ticker}")

    record_transaction(session, usd, TransactionRequest(
        request_id="d1", event_type=EventType.DEPOSIT,
        amount=Decimal("5000"), occurred_at=at(DAY - timedelta(days=1))))
    record_transaction(session, usd, TransactionRequest(
        request_id="b1", event_type=EventType.BUY, ticker="AAPL",
        quantity=Decimal("10"), unit_price=Decimal("200"),
        occurred_at=at(DAY - timedelta(days=1))))
    record_transaction(session, twd, TransactionRequest(
        request_id="d2", event_type=EventType.DEPOSIT,
        amount=Decimal("100000"), occurred_at=at(DAY - timedelta(days=1))))
    record_transaction(session, twd, TransactionRequest(
        request_id="b2", event_type=EventType.BUY, ticker="2330.TW",
        quantity=Decimal("5"), unit_price=Decimal("1000"),
        occurred_at=at(DAY - timedelta(days=1))))
    return usd, twd


def test_a_group_reports_both_currencies_in_one_total(session, two_portfolios, provider) -> None:
    """The headline capability: USD and TWD holdings become one comparable number."""
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)

    # USD side: 10 AAPL at 200 = 2,000 securities, 3,000 cash.
    # TWD side: 5 x 1,000 = 5,000 securities and 95,000 cash, at 32 TWD per USD.
    assert summary.securities_value == Decimal("2156.25")   # 2000 + 5000/32
    assert summary.cash_value == Decimal("5968.75")         # 3000 + 95000/32
    assert summary.total_value == Decimal("8125")
    assert summary.converted_value_coverage_percent == Decimal("100")


def test_every_position_keeps_its_local_and_converted_value(
    session, two_portfolios, provider
) -> None:
    """Plan 2.4: a converted figure without its original is not auditable."""
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    taiwanese = next(row for row in summary.positions if row.ticker == "2330.TW")

    assert taiwanese.local_currency == "TWD"
    assert taiwanese.local_market_value == Decimal("5000")
    assert taiwanese.reporting_market_value == Decimal("156.25")
    assert taiwanese.fx_rate is not None
    assert taiwanese.fx_path == ["TWD", "USD"]
    assert taiwanese.fx_as_of is not None


def test_the_rates_used_are_reported(session, two_portfolios, provider) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    currencies = {item.base_currency for item in summary.fx_rates_used}

    assert "TWD" in currencies, "the rate behind the conversion must be visible"
    assert summary.calculation_method


def test_weights_sum_to_the_converted_total(session, two_portfolios, provider) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    weighted = sum(
        (row.weight_percent for row in summary.positions if row.weight_percent), start=Decimal("0")
    )
    securities_share = summary.securities_value / summary.total_value * Decimal("100")
    assert abs(weighted - securities_share) < Decimal("0.01")


def test_an_unconvertible_currency_is_excluded_and_reported(
    session, two_portfolios, provider
) -> None:
    """The property that keeps a consolidated total honest when FX is missing."""
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])
    provider.unavailable.add("USDTWD=X")

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)

    # Only the USD side converts: 2,000 securities + 3,000 cash.
    assert summary.securities_value == Decimal("2000")
    assert summary.cash_value == Decimal("3000")
    assert summary.unconverted, "the TWD value must be listed, not dropped"
    assert {item.currency for item in summary.unconverted} == {"TWD"}
    assert summary.converted_value_coverage_percent < Decimal("100")
    assert any("could not be converted" in warning for warning in summary.warnings)


def test_an_unconvertible_holding_keeps_its_local_value(
    session, two_portfolios, provider
) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])
    provider.unavailable.add("USDTWD=X")

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    taiwanese = next(row for row in summary.positions if row.ticker == "2330.TW")

    assert taiwanese.local_market_value == Decimal("5000"), "the local figure survives"
    assert taiwanese.reporting_market_value is None, "no guessed conversion"
    assert taiwanese.weight_percent is None, "an unconverted holding has no weight, not zero"


def test_an_unpriceable_holding_does_not_break_the_group(
    session, two_portfolios, provider
) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])
    provider.unavailable.add("AAPL")

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    apple = next(row for row in summary.positions if row.ticker == "AAPL")

    assert apple.local_market_value is None
    assert summary.securities_value == Decimal("156.25"), "the TWD holding still counts"


def test_currency_exposure_covers_holdings_and_cash(session, two_portfolios, provider) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])

    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    exposure = {item.currency: item for item in summary.currency_exposure}

    assert exposure["USD"].local_amount == Decimal("5000")     # 2,000 stock + 3,000 cash
    assert exposure["TWD"].local_amount == Decimal("100000")   # 5,000 stock + 95,000 cash
    assert exposure["TWD"].reporting_amount == Decimal("3125")


def test_issuer_exposure_aggregates_across_listings(session, harness, provider) -> None:
    """TSM and 2330.TW are one company; issuer view must combine them without merging listings."""
    usd = harness.portfolio("US", "USD")
    twd = harness.portfolio("TW", "TWD")
    for ticker in ("TSM", "2330.TW"):
        harness.client.get(f"/api/v1/market/instruments/{ticker}")
    for reference in ("TSM", "2330.TW"):
        harness.client.put(
            f"/api/v1/instruments/{reference}/issuer",
            json={"request_id": f"iss-{reference}", "legal_name": "TSMC"},
        )

    provider.values["TSM"] = Decimal("300")
    record_transaction(session, usd, TransactionRequest(
        request_id="d", event_type=EventType.DEPOSIT, amount=Decimal("10000"),
        occurred_at=at(DAY - timedelta(days=1))))
    record_transaction(session, usd, TransactionRequest(
        request_id="b", event_type=EventType.BUY, ticker="TSM", quantity=Decimal("10"),
        unit_price=Decimal("300"), occurred_at=at(DAY - timedelta(days=1))))
    record_transaction(session, twd, TransactionRequest(
        request_id="d2", event_type=EventType.DEPOSIT, amount=Decimal("100000"),
        occurred_at=at(DAY - timedelta(days=1))))
    record_transaction(session, twd, TransactionRequest(
        request_id="b2", event_type=EventType.BUY, ticker="2330.TW", quantity=Decimal("5"),
        unit_price=Decimal("1000"), occurred_at=at(DAY - timedelta(days=1))))

    group = create_group(session, "All", "USD", [usd, twd])
    summary = build_consolidated_summary(session, group.id, provider, as_of=DAY)

    assert len(summary.issuer_exposure) == 1, "one company, one issuer row"
    exposure = summary.issuer_exposure[0]
    assert exposure.tickers == ["2330.TW", "TSM"]
    assert exposure.reporting_value == Decimal("3156.25")  # 3,000 + 5,000/32
    assert len(summary.positions) == 2, "the individual listings are still separate"


def test_membership_is_effective_dated(session, two_portfolios, provider) -> None:
    """Plan 8.7: changing members must not rewrite a past report.

    Dropping a portfolio closes its interval at the moment of the change, so it disappears from
    reports after that instant while remaining in every report before it. The dropped row is
    retained rather than deleted, which is what makes the earlier report reproducible.
    """
    from portfolio_manager.models import PortfolioGroupMember

    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])
    assert len(member_portfolio_ids(session, group.id, DAY)) == 2

    replace_members(session, group.id, [usd])

    # After the change, only the remaining portfolio is a member.
    assert member_portfolio_ids(session, group.id, DAY + timedelta(days=1)) == [usd]

    # The dropped membership is closed, not erased, so the earlier interval is still on record.
    import sqlalchemy as sa

    dropped = session.scalar(
        sa.select(PortfolioGroupMember).where(
            PortfolioGroupMember.group_id == group.id,
            PortfolioGroupMember.portfolio_id == twd,
        )
    )
    assert dropped is not None, "membership rows are never deleted"
    assert dropped.effective_to is not None, "the interval was closed"
    assert dropped.effective_from < dropped.effective_to


def test_a_group_needs_at_least_one_portfolio(session) -> None:
    with pytest.raises(DomainError) as excinfo:
        create_group(session, "Empty", "USD", [])
    assert excinfo.value.code == "empty_group"


def test_a_group_rejects_an_unknown_portfolio(session) -> None:
    with pytest.raises(DomainError) as excinfo:
        create_group(session, "Bad", "USD", ["does-not-exist"])
    assert excinfo.value.status_code == 404


def test_an_unknown_group_is_not_found(session, provider) -> None:
    with pytest.raises(DomainError) as excinfo:
        build_consolidated_summary(session, "missing", provider, as_of=DAY)
    assert excinfo.value.status_code == 404


def test_a_future_as_of_is_rejected(session, two_portfolios, provider) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)

    with pytest.raises(DomainError) as excinfo:
        build_consolidated_summary(session, group.id, provider, as_of=tomorrow)
    assert excinfo.value.code == "valuation_date_in_future"


def test_the_reporting_currency_can_be_overridden(session, two_portfolios, provider) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])

    in_twd = build_consolidated_summary(
        session, group.id, provider, as_of=DAY, reporting_currency="TWD"
    )
    assert in_twd.reporting_currency == "TWD"
    # The same holdings, expressed the other way: 8,125 USD at 32 TWD/USD.
    assert in_twd.total_value == Decimal("260000")


def test_consolidation_is_deterministic(session, two_portfolios, provider) -> None:
    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])

    first = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    second = build_consolidated_summary(session, group.id, provider, as_of=DAY)
    assert first.total_value == second.total_value


def test_deleting_a_group_leaves_the_portfolios_intact(session, two_portfolios, provider) -> None:
    """A group is a reporting lens, so removing it must destroy no recorded data."""
    import sqlalchemy as sa

    from portfolio_manager.models import Portfolio, PortfolioGroupMember
    from portfolio_manager.replay import replay_state

    usd, twd = two_portfolios
    group = create_group(session, "All", "USD", [usd, twd])
    before = replay_state(session, usd, datetime(2030, 1, 1, tzinfo=UTC))

    delete_group(session, group.id)

    assert session.get(Portfolio, usd) is not None
    assert session.get(Portfolio, twd) is not None
    after = replay_state(session, usd, datetime(2030, 1, 1, tzinfo=UTC))
    assert after.cash == before.cash
    assert len(after.positions) == len(before.positions)

    orphans = session.scalars(
        sa.select(PortfolioGroupMember).where(PortfolioGroupMember.group_id == group.id)
    ).all()
    assert orphans == [], "membership rows describe only this grouping and go with it"


def test_deleting_an_unknown_group_is_not_found(session) -> None:
    with pytest.raises(DomainError) as excinfo:
        delete_group(session, "missing")
    assert excinfo.value.status_code == 404


def test_a_deleted_group_can_be_recreated(session, two_portfolios, provider) -> None:
    """Nothing about the deletion prevents reporting the same portfolios together again."""
    usd, twd = two_portfolios
    first = create_group(session, "All", "USD", [usd, twd])
    total = build_consolidated_summary(session, first.id, provider, as_of=DAY).total_value

    delete_group(session, first.id)
    second = create_group(session, "All", "USD", [usd, twd])

    assert second.id != first.id
    assert build_consolidated_summary(session, second.id, provider, as_of=DAY).total_value == total
