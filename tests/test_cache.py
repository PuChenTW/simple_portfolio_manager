"""The market cache must be invisible except in the requests it prevents.

Every test here asserts one of two things: that a repeated question stops reaching the provider,
or that the answer is identical whether it came from Redis or from the provider. A cache that
changes a number is worse than no cache, so the equality checks matter more than the hit counts.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portfolio_manager.cache import (
    CachingMarketProvider,
    month_key,
    months_between,
)
from portfolio_manager.market import (
    HistoryAdjustment,
    HistoryBar,
    HistoryInterval,
    HistoryResult,
    MarketDataError,
)


class FakeRedis:
    """The handful of Redis commands the cache uses, backed by a dict."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("redis is down")

    def mget(self, keys: list[str]) -> list[str | None]:
        self._check()
        return [self.store.get(key) for key in keys]

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._check()
        self.store[key] = value

    def scan_iter(self, match: str, count: int = 100):
        self._check()
        import fnmatch

        yield from [key for key in list(self.store) if fnmatch.fnmatch(key, match)]

    def delete(self, key: str) -> int:
        self._check()
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def provider(market_provider):
    return market_provider


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def caching(provider, redis: FakeRedis) -> CachingMarketProvider:
    return CachingMarketProvider(
        provider, redis, month_ttl_seconds=2592000, recent_ttl_seconds=600
    )


# A range wholly in the past, so no bucket lands in the current month and every month is
# complete. Dates are before the fake provider's fixed `last_date` of 2026-07-24.
START = date(2026, 3, 1)
END = date(2026, 4, 30)


def test_a_repeated_range_does_not_reach_the_provider(caching, provider) -> None:
    first = caching.history("AAPL", start_date=START, end_date=END)
    assert provider.history_calls == ["AAPL"]

    second = caching.history("AAPL", start_date=START, end_date=END)
    assert provider.history_calls == ["AAPL"], "the second range was served from cache"
    assert [bar.close for bar in second.bars] == [bar.close for bar in first.bars]


def test_cached_bars_are_identical_to_provider_bars(provider, caching) -> None:
    """Serialization must not round a price or lose a bar."""
    uncached = provider.history("AAPL", start_date=START, end_date=END)
    caching.history("AAPL", start_date=START, end_date=END)
    from_cache = caching.history("AAPL", start_date=START, end_date=END)

    assert [bar.timestamp for bar in from_cache.bars] == [bar.timestamp for bar in uncached.bars]
    assert [bar.close for bar in from_cache.bars] == [bar.close for bar in uncached.bars]
    assert [bar.volume for bar in from_cache.bars] == [bar.volume for bar in uncached.bars]


def test_decimal_values_survive_the_round_trip_exactly(caching) -> None:
    """A price must never pass through a float; string storage is what guarantees that."""
    caching.history("AAPL", start_date=START, end_date=END)
    cached = caching.history("AAPL", start_date=START, end_date=END)
    for bar in cached.bars:
        assert isinstance(bar.close, Decimal)
    # 140 - index/10 produces values like 139.7 that a float would not represent exactly.
    assert any(bar.close.as_tuple().exponent < 0 for bar in cached.bars)


def test_the_provider_string_is_preserved(caching) -> None:
    """It is persisted into `fx_rates.provider` and is part of that table's unique constraint."""
    caching.history("AAPL", start_date=START, end_date=END)
    assert caching.history("AAPL", start_date=START, end_date=END).provider == "Fake Market"


def test_warnings_are_cached_with_their_bars(provider, redis) -> None:
    """Losing a warning would make a cached read look cleaner than the original."""

    class WarningProvider(type(provider)):
        def history(self, ticker: str, **kwargs) -> HistoryResult:
            result = super().history(ticker, **kwargs)
            return HistoryResult(
                ticker=result.ticker,
                provider=result.provider,
                interval=result.interval,
                adjustment=result.adjustment,
                requested_start_date=result.requested_start_date,
                requested_end_date=result.requested_end_date,
                fetched_at=result.fetched_at,
                warnings=["Volume was unavailable for 2 observations"],
                bars=result.bars,
            )

    caching = CachingMarketProvider(
        WarningProvider(), redis, month_ttl_seconds=100, recent_ttl_seconds=10
    )
    caching.history("AAPL", start_date=START, end_date=END)
    assert caching.history("AAPL", start_date=START, end_date=END).warnings == [
        "Volume was unavailable for 2 observations"
    ]


def test_a_cached_range_never_returns_a_bar_after_the_end_date(caching) -> None:
    """Invariant 5: a date is never valued with a later price, cache or not."""
    caching.history("AAPL", start_date=START, end_date=END)
    narrower = caching.history("AAPL", start_date=START, end_date=date(2026, 3, 15))
    assert narrower.bars, "the narrower range still returns data"
    assert all(bar.timestamp.date() <= date(2026, 3, 15) for bar in narrower.bars)


def test_a_narrower_range_is_served_from_the_cached_months(caching, provider) -> None:
    caching.history("AAPL", start_date=START, end_date=END)
    caching.history("AAPL", start_date=date(2026, 3, 10), end_date=date(2026, 3, 20))
    assert provider.history_calls == ["AAPL"], "a subrange reuses the cached months"


def test_redis_failure_degrades_to_the_provider(caching, provider, redis) -> None:
    """A broken cache must not become a broken API."""
    redis.fail = True
    result = caching.history("AAPL", start_date=START, end_date=END)
    assert result.bars, "the provider answered despite Redis being down"
    assert provider.history_calls == ["AAPL"]


def test_a_provider_error_is_not_cached(caching, provider) -> None:
    """A delisted ticker must stay an error, not become a permanent empty result."""
    with pytest.raises(MarketDataError):
        caching.history("NOPE", start_date=START, end_date=END)
    with pytest.raises(MarketDataError):
        caching.history("NOPE", start_date=START, end_date=END)


def test_a_mid_month_request_still_caches_the_whole_month(provider, redis, caching) -> None:
    """A stored bucket is always the complete month, never the slice that was asked for.

    Callers request ranges that start and end mid-month -- a snapshot rebuild asks for "60 days
    back from this date". If such a request cached only its own slice, a later full-month read
    would inherit a gap the cache itself invented; if it cached nothing, the hottest path in the
    service would never be cached at all. So the fetch widens to the month boundary.
    """
    caching.history("AAPL", start_date=date(2026, 3, 10), end_date=date(2026, 3, 20))
    assert list(redis.store) == [
        month_key("AAPL", HistoryInterval.DAILY, HistoryAdjustment.AUTO, date(2026, 3, 1))
    ], "the whole month was cached, not the requested slice"

    provider.history_calls.clear()
    full = caching.history("AAPL", start_date=date(2026, 3, 1), end_date=date(2026, 3, 31))
    assert provider.history_calls == [], "the full month was served from that bucket"
    assert len(full.bars) == 31, "and it came back complete, with no gap"


def test_a_mid_month_request_returns_only_what_was_asked_for(caching) -> None:
    """Widening the fetch must not widen the answer."""
    result = caching.history("AAPL", start_date=date(2026, 3, 10), end_date=date(2026, 3, 20))
    assert all(
        date(2026, 3, 10) <= bar.timestamp.date() <= date(2026, 3, 20) for bar in result.bars
    )
    assert len(result.bars) == 11


def test_days_relative_requests_are_not_cached(caching, provider) -> None:
    """`days` means "the last N bars", whose answer moves with the calendar."""
    caching.history("AAPL", days=30)
    caching.history("AAPL", days=30)
    assert provider.history_calls == ["AAPL", "AAPL"]


def test_clear_ticker_removes_only_that_symbol(caching, redis, provider) -> None:
    caching.history("AAPL", start_date=START, end_date=END)
    caching.history("MSFT", start_date=START, end_date=END)
    assert caching.clear_ticker("AAPL") == 2, "both AAPL months were dropped"

    provider.history_calls.clear()
    caching.history("MSFT", start_date=START, end_date=END)
    assert provider.history_calls == [], "MSFT was untouched"
    caching.history("AAPL", start_date=START, end_date=END)
    assert provider.history_calls == ["AAPL"], "AAPL was refetched"


def test_disabled_cache_passes_everything_through(provider) -> None:
    caching = CachingMarketProvider(
        provider, None, month_ttl_seconds=100, recent_ttl_seconds=10
    )
    caching.history("AAPL", start_date=START, end_date=END)
    caching.history("AAPL", start_date=START, end_date=END)
    assert provider.history_calls == ["AAPL", "AAPL"]
    assert caching.clear_ticker("AAPL") == 0


def test_the_current_month_gets_the_short_ttl(provider, redis) -> None:
    """Today's bar is still moving, so its bucket must expire quickly."""
    ttls: dict[str, int] = {}

    class RecordingRedis(FakeRedis):
        def setex(self, key: str, ttl: int, value: str) -> None:
            ttls[key] = ttl
            super().setex(key, ttl, value)

    today = datetime.now(UTC).date()

    class TodayProvider(type(provider)):
        def history(self, ticker: str, **kwargs) -> HistoryResult:
            result = super().history(ticker, **kwargs)
            bar = HistoryBar(
                timestamp=datetime.combine(today, datetime.min.time(), UTC),
                open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
                close=Decimal("1"), volume=Decimal("1"),
            )
            return HistoryResult(
                ticker=result.ticker, provider=result.provider, interval=result.interval,
                adjustment=result.adjustment, requested_start_date=today.replace(day=1),
                requested_end_date=_end_of_month(today), fetched_at=result.fetched_at,
                warnings=[], bars=[bar],
            )

    recording = RecordingRedis()
    caching = CachingMarketProvider(
        TodayProvider(), recording, month_ttl_seconds=2592000, recent_ttl_seconds=600
    )
    caching.history("AAPL", start_date=today.replace(day=1), end_date=_end_of_month(today))
    key = month_key("AAPL", HistoryInterval.DAILY, HistoryAdjustment.AUTO, today.replace(day=1))
    assert ttls[key] == 600, "the live month expires on the short TTL"


def _end_of_month(day: date) -> date:
    from datetime import timedelta

    return (day.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def test_months_between_covers_a_year_boundary() -> None:
    months = list(months_between(date(2025, 11, 15), date(2026, 2, 3)))
    assert months == [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)]


def test_adjustment_and_interval_are_part_of_the_key(caching, provider) -> None:
    """Adjusted and unadjusted bars are different data and must never share an entry."""
    caching.history("AAPL", start_date=START, end_date=END)
    caching.history(
        "AAPL", start_date=START, end_date=END, adjustment=HistoryAdjustment.UNADJUSTED
    )
    assert provider.history_calls == ["AAPL", "AAPL"]
