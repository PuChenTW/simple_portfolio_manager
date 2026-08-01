"""Currency conversion: auditable, bounded by the cutoff, and never invented.

The rules under test come from plan 2.3. A conversion discloses how it was derived; a historical
conversion never reaches forward for a rate; and a pair that cannot be resolved returns no number
rather than a plausible one.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_manager.fx import (
    ConversionMethod,
    FxService,
    FxUnavailable,
)
from portfolio_manager.market import (
    HistoryAdjustment,
    HistoryBar,
    HistoryInterval,
    HistoryResult,
    MarketDataError,
)
from portfolio_manager.models import FxRate


def select_all_rates():
    import sqlalchemy as sa

    return sa.select(FxRate)

ZERO = Decimal("0")
DAY = date(2026, 7, 30)
EPOCH = date(2026, 7, 1)


class FxProvider:
    """Serves the pairs it is given and nothing else, so gaps are exercised explicitly."""

    def __init__(
        self, pairs: dict[str, Decimal] | None = None, *, drift: bool = False
    ) -> None:
        self.pairs = pairs if pairs is not None else {"USDTWD=X": Decimal("32.00")}
        # Off by default so a test asserting on a rate sees exactly what it supplied. Turn on
        # to give each date a distinguishable value when testing point-in-time behaviour.
        self.drift = drift
        self.requested: list[tuple[str, date | None]] = []

    def fetch(self, ticker: str):  # pragma: no cover - FX must not use the quote path
        raise AssertionError("FX conversion must not read the current quote")

    def history(self, ticker, days=None, start_date=None, end_date=None, **kwargs):
        self.requested.append((ticker, end_date))
        if ticker not in self.pairs:
            raise MarketDataError(f"No FX data for {ticker}")
        base = self.pairs[ticker]
        first = start_date or DAY
        last = end_date or DAY
        bars, day = [], first
        while day <= last:
            # Drift is anchored to a fixed epoch, not to the request window, so the rate for a
            # given date is the same no matter which range asked for it.
            rate = base + (Decimal((day - EPOCH).days) / Decimal("100") if self.drift else ZERO)
            bars.append(
                HistoryBar(
                    timestamp=datetime.combine(day, datetime.min.time(), UTC),
                    open=rate, high=rate, low=rate, close=rate, volume=Decimal("0"),
                )
            )
            day += timedelta(days=1)
        return HistoryResult(
            ticker=ticker, provider="Fake FX",
            interval=kwargs.get("interval", HistoryInterval.DAILY),
            adjustment=kwargs.get("adjustment", HistoryAdjustment.AUTO),
            requested_start_date=start_date, requested_end_date=end_date,
            fetched_at=datetime.now(UTC), warnings=[], bars=bars,
        )


@pytest.fixture
def session(harness):
    with harness.session_factory() as active:
        yield active


def test_same_currency_converts_at_one(session) -> None:
    service = FxService(session, FxProvider())
    result = service.convert("USD", "USD", DAY)

    assert result.rate == Decimal("1")
    assert result.method == ConversionMethod.IDENTITY


def test_a_direct_pair_is_used_as_published(session) -> None:
    service = FxService(session, FxProvider({"USDTWD=X": Decimal("32.00")}))
    result = service.convert("USD", "TWD", DAY)

    assert result.method == ConversionMethod.DIRECT
    assert result.conversion_path == ["USD", "TWD"]
    assert result.apply(Decimal("100")) == result.rate * Decimal("100")
    assert result.provider == "Fake FX"


def test_a_missing_pair_is_inverted_and_says_so(session) -> None:
    """TWD/USD is rarely published; inverting USD/TWD is correct but must be disclosed."""
    service = FxService(session, FxProvider({"USDTWD=X": Decimal("32.00")}))
    result = service.convert("TWD", "USD", DAY)

    assert result.method == ConversionMethod.INVERSE
    assert any("inverted" in warning for warning in result.warnings)
    # Inverting a rate near 32 gives roughly 0.031.
    assert Decimal("0.03") < result.rate < Decimal("0.032")


def test_an_inverse_round_trip_returns_the_original_amount(session) -> None:
    service = FxService(session, FxProvider({"USDTWD=X": Decimal("32.00")}))
    forward = service.convert("USD", "TWD", DAY)
    back = service.convert("TWD", "USD", DAY)

    amount = Decimal("1000")
    round_tripped = back.apply(forward.apply(amount))
    assert abs(round_tripped - amount) < Decimal("0.01"), "inversion must not lose value"


def test_a_cross_reports_its_full_path(session) -> None:
    """With no JPY/TWD pair, the conversion routes through USD and says so."""
    service = FxService(
        session,
        FxProvider({"USDTWD=X": Decimal("32.00"), "USDJPY=X": Decimal("150.00")}),
    )
    result = service.convert("JPY", "TWD", DAY)

    assert result.method == ConversionMethod.CROSS
    assert result.conversion_path == ["JPY", "USD", "TWD"]
    assert any("crosses through USD" in warning for warning in result.warnings)
    # 1 JPY = 1/150 USD = 32/150 TWD, about 0.213.
    assert Decimal("0.21") < result.rate < Decimal("0.22")


def test_an_unresolvable_pair_returns_no_rate(session) -> None:
    """Plan principle 3: no rate is better than a plausible one."""
    service = FxService(session, FxProvider({}))
    result = service.convert("USD", "TWD", DAY)

    assert isinstance(result, FxUnavailable)
    assert "No USD/TWD rate" in result.reason


def test_a_historical_conversion_never_uses_a_later_rate(session) -> None:
    """The property that keeps a backfilled series from looking clairvoyant."""
    provider = FxProvider({"USDTWD=X": Decimal("32.00")}, drift=True)
    service = FxService(session, provider)

    earlier = service.convert("USD", "TWD", DAY - timedelta(days=5))
    later = service.convert("USD", "TWD", DAY)

    assert earlier.rate < later.rate, "the rate rises daily in this fixture"
    assert all(end is None or end <= DAY for _, end in provider.requested)
    assert earlier.price_as_of.date() <= DAY - timedelta(days=5)


def test_a_provider_ignoring_the_cutoff_is_still_bounded(session) -> None:
    """Defense in depth: future bars are discarded rather than trusted."""

    class LeakyProvider(FxProvider):
        def history(self, ticker, **kwargs):
            kwargs["end_date"] = DAY + timedelta(days=10)
            return super().history(ticker, **kwargs)

    service = FxService(session, LeakyProvider({"USDTWD=X": Decimal("32.00")}))
    service.convert("USD", "TWD", DAY)

    stored = session.scalars(select_all_rates()).all()
    assert stored, "rates were stored"
    assert all(rate.price_as_of.date() <= DAY for rate in stored), "no future rate persisted"


def test_a_stale_rate_is_flagged_but_still_returned(session) -> None:
    """A carried-forward rate stays usable; it just must not look current."""
    provider = FxProvider({"USDTWD=X": Decimal("32.00")})
    service = FxService(session, provider)
    service.convert("USD", "TWD", DAY - timedelta(days=20))

    # Ask well after the last stored observation, with the provider now unable to serve.
    provider.pairs = {}
    later = FxService(session, provider).convert("USD", "TWD", DAY)

    assert later.rate > 0
    assert later.is_stale is True
    assert any("days earlier" in warning for warning in later.warnings)


def test_rates_are_stored_for_audit(session) -> None:
    """Plan 2.2: storing only converted totals would make a report impossible to check."""
    service = FxService(session, FxProvider({"USDTWD=X": Decimal("32.00")}))
    service.convert("USD", "TWD", DAY)

    stored = session.scalars(select_all_rates()).all()
    assert stored
    row = stored[0]
    assert row.provider == "Fake FX"
    assert row.provider_symbol == "USDTWD=X"
    assert row.rate > 0


def test_fetching_twice_does_not_duplicate_stored_rates(session) -> None:
    provider = FxProvider({"USDTWD=X": Decimal("32.00")})
    FxService(session, provider).convert("USD", "TWD", DAY)
    before = len(session.scalars(select_all_rates()).all())

    FxService(session, provider).convert("USD", "TWD", DAY)
    assert len(session.scalars(select_all_rates()).all()) == before


def test_float32_noise_is_not_stored_as_a_rate(session) -> None:
    """Yahoo returns 32.395 as 32.39500045776367; the residue must not reach the record."""
    service = FxService(session, FxProvider({"USDTWD=X": Decimal("32.39500045776367")}))
    result = service.convert("USD", "TWD", DAY)

    assert result.rate == Decimal("32.395"), "the rate is quantized, not carried raw"


def test_an_unavailable_pair_is_not_refetched_repeatedly(session) -> None:
    provider = FxProvider({})
    service = FxService(session, provider)
    service.convert("USD", "TWD", DAY)
    calls_after_first = len(provider.requested)

    service.convert("USD", "TWD", DAY)
    assert len(provider.requested) == calls_after_first, "a known failure is remembered"
