"""Redis cache for market data, wrapping any `MarketProvider`.

The cache is an accelerator, never a source of truth. Every Redis failure degrades to the
wrapped provider rather than surfacing an error, because a cache that can break a request is
worse than no cache. Nothing here decides what a price *means*; it only avoids asking Yahoo the
same question twice.

Daily bars are stored one calendar month per key. A range request is answered by reading the
months it spans and fetching only the ones that are missing, so the overlapping ranges a snapshot
rebuild produces reuse each other instead of each becoming its own entry. The month containing
today is held briefly: its last bar is still moving. Older months are held far longer because a
closed session's OHLC is a fact.

That "fact" has one exception. With `auto_adjust`, Yahoo restates past bars after a split or
dividend, so a cached month can disagree with the provider's current view of the same month.
Rather than guess when that happened, recording a corporate action warns the operator and
`clear_ticker` lets them drop the affected symbol.
"""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from .market import (
    HistoryAdjustment,
    HistoryBar,
    HistoryInterval,
    HistoryResult,
    MarketProvider,
)

logger = logging.getLogger(__name__)

# Serialized payloads carry this so a format change cannot be read back as valid data.
SCHEMA_VERSION = 1

KEY_PREFIX = "pm"


class CacheUnavailable(Exception):
    """Raised internally when Redis cannot answer; always caught and degraded."""


def month_key(ticker: str, interval: HistoryInterval, adjustment: HistoryAdjustment,
              month: date) -> str:
    return f"{KEY_PREFIX}:hist:{ticker}:{interval.value}:{adjustment.value}:{month:%Y-%m}"


def ticker_pattern(ticker: str) -> str:
    return f"{KEY_PREFIX}:*:{ticker}:*"


def months_between(start: date, end: date) -> Iterator[date]:
    """Every month-start from `start`'s month through `end`'s month, inclusive."""
    current = start.replace(day=1)
    last = end.replace(day=1)
    while current <= last:
        yield current
        current = (current + timedelta(days=32)).replace(day=1)


def _next_month(month: date) -> date:
    return (month + timedelta(days=32)).replace(day=1)


def _encode_bar(bar: HistoryBar) -> dict[str, str]:
    # Decimals are stored as strings: routing a price through float would defeat the exact
    # arithmetic the rest of the service is built on.
    return {
        "t": bar.timestamp.isoformat(),
        "o": str(bar.open),
        "h": str(bar.high),
        "l": str(bar.low),
        "c": str(bar.close),
        "v": str(bar.volume),
    }


def _decode_bar(raw: dict[str, str]) -> HistoryBar:
    return HistoryBar(
        timestamp=datetime.fromisoformat(raw["t"]),
        open=Decimal(raw["o"]),
        high=Decimal(raw["h"]),
        low=Decimal(raw["l"]),
        close=Decimal(raw["c"]),
        volume=Decimal(raw["v"]),
    )


class CachingMarketProvider:
    """A `MarketProvider` that serves daily bars from Redis and falls through on a miss.

    Structural typing means this substitutes for the real provider everywhere without any
    consumer knowing it exists.
    """

    def __init__(
        self,
        inner: MarketProvider,
        client: object | None,
        *,
        month_ttl_seconds: int,
        recent_ttl_seconds: int,
    ) -> None:
        self.inner = inner
        self.client = client
        self.month_ttl = month_ttl_seconds
        self.recent_ttl = recent_ttl_seconds

    # `fetch` returns a live quote plus a year of derived indicators. Caching it here would
    # duplicate `MarketService`, which already has a TTL and the stale-quote fallback, so this
    # simply delegates.
    def fetch(self, ticker: str):
        return self.inner.fetch(ticker)

    def history(
        self,
        ticker: str,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        interval: HistoryInterval = HistoryInterval.DAILY,
        adjustment: HistoryAdjustment = HistoryAdjustment.AUTO,
    ) -> HistoryResult:
        symbol = ticker.strip().upper()
        # Only explicit bounded ranges are cacheable. A `days`-relative request means "the last N
        # bars", whose answer moves with the calendar and cannot be keyed by month.
        cacheable = (
            self.client is not None
            and start_date is not None
            and end_date is not None
            and interval is HistoryInterval.DAILY
        )
        if not cacheable:
            return self.inner.history(
                symbol,
                days=days,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                adjustment=adjustment,
            )

        assert start_date is not None and end_date is not None  # narrowed by `cacheable`
        try:
            return self._cached_history(symbol, start_date, end_date, interval, adjustment)
        except Exception:
            logger.warning("market cache unavailable for %s; using provider", symbol,
                           exc_info=True)
            return self.inner.history(
                symbol,
                days=days,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                adjustment=adjustment,
            )

    def _cached_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        interval: HistoryInterval,
        adjustment: HistoryAdjustment,
    ) -> HistoryResult:
        months = list(months_between(start_date, end_date))
        keys = [month_key(symbol, interval, adjustment, month) for month in months]
        stored = self.client.mget(keys)  # type: ignore[union-attr]

        cached: dict[date, dict] = {}
        missing: list[date] = []
        for month, raw in zip(months, stored, strict=True):
            payload = _load(raw)
            if payload is None:
                missing.append(month)
            else:
                cached[month] = payload

        if missing:
            # One provider call spanning every gap, widened to whole months. Callers ask for
            # ranges that start and end mid-month -- a snapshot rebuild asks for "60 days back
            # from this date" -- and a partial month cannot be cached without inventing a gap
            # later. Fetching to the month boundary costs a few extra days once and makes the
            # bucket reusable by every later request that touches the same month.
            today = datetime.now(UTC).date()
            fetch_start = min(missing)
            # Never past today: the provider has nothing beyond it, and asking implies a
            # look-ahead this service does not permit.
            fetch_end = min(_end_of_month(max(missing)), today)
            fetched = self.inner.history(
                symbol,
                start_date=fetch_start,
                end_date=fetch_end,
                interval=interval,
                adjustment=adjustment,
            )
            cached.update(self._store(symbol, fetched, missing, interval, adjustment))

        bars: list[HistoryBar] = []
        warnings: list[str] = []
        provider = ""
        fetched_at = datetime.now(UTC)
        for month in months:
            payload = cached.get(month)
            if payload is None:
                continue
            bars.extend(_decode_bar(item) for item in payload["bars"])
            provider = provider or payload["provider"]
            for warning in payload["warnings"]:
                if warning not in warnings:
                    warnings.append(warning)

        # Re-apply the requested bounds. Month buckets are wider than the request, and a bar
        # after `end_date` would be look-ahead in every caller that prices a past date.
        bars = sorted(
            (bar for bar in bars if start_date <= bar.timestamp.date() <= end_date),
            key=lambda bar: bar.timestamp,
        )
        return HistoryResult(
            ticker=symbol,
            provider=provider,
            interval=interval,
            adjustment=adjustment,
            requested_start_date=start_date,
            requested_end_date=end_date,
            fetched_at=fetched_at,
            warnings=warnings,
            bars=bars,
        )

    def _store(
        self,
        symbol: str,
        result: HistoryResult,
        months: list[date],
        interval: HistoryInterval,
        adjustment: HistoryAdjustment,
    ) -> dict[date, dict]:
        """Split a provider result into month buckets and write each one."""
        today = datetime.now(UTC).date()
        this_month = today.replace(day=1)
        by_month: dict[date, list[HistoryBar]] = {month: [] for month in months}
        for bar in result.bars:
            month = bar.timestamp.date().replace(day=1)
            if month in by_month:
                by_month[month].append(bar)

        # Only months the fetch covered end to end may be stored. A request starting or ending
        # mid-month produces a partial bucket that is indistinguishable from a complete one once
        # written, and a later full-month read would silently lose the bars outside the original
        # request. Partial months are still returned to this caller, just never persisted.
        covered_from = result.requested_start_date
        covered_to = result.requested_end_date

        written: dict[date, dict] = {}
        for month, month_bars in by_month.items():
            # An empty month is not evidence of a holiday-only month; the provider may simply
            # have returned nothing. Caching it would turn one bad response into a lasting gap.
            if not month_bars:
                continue
            # The month must be covered from its first day, and through its last -- except the
            # current month, which cannot be complete yet and is instead held under the short
            # TTL so its still-moving final bar is re-read soon.
            needed_through = min(_end_of_month(month), today)
            complete = (
                covered_from is not None
                and covered_to is not None
                and covered_from <= month
                and needed_through <= covered_to
            )
            payload = {
                "v": SCHEMA_VERSION,
                # Preserved verbatim: this string is persisted into `fx_rates.provider` and
                # participates in its unique constraint, so a substitute would create duplicates.
                "provider": result.provider,
                "warnings": result.warnings,
                "bars": [_encode_bar(bar) for bar in month_bars],
            }
            written[month] = payload
            if not complete:
                continue
            ttl = self.recent_ttl if month >= this_month else self.month_ttl
            try:
                self.client.setex(  # type: ignore[union-attr]
                    month_key(symbol, interval, adjustment, month), ttl, json.dumps(payload)
                )
            except Exception:
                logger.warning("could not cache %s %s", symbol, month, exc_info=True)
        return written

    def clear_ticker(self, ticker: str) -> int:
        """Drop every cached entry for a symbol. Returns how many keys were removed."""
        if self.client is None:
            return 0
        symbol = ticker.strip().upper()
        removed = 0
        # SCAN rather than KEYS: this runs against the same Redis serving live requests.
        for key in self.client.scan_iter(match=ticker_pattern(symbol), count=100):  # type: ignore[union-attr]
            removed += self.client.delete(key)  # type: ignore[union-attr]
        return removed


def _end_of_month(month: date) -> date:
    return _next_month(month) - timedelta(days=1)


def _load(raw: object) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != SCHEMA_VERSION:
        return None
    return payload


def build_provider(inner: MarketProvider) -> MarketProvider:
    """Wrap a provider in the configured cache, or return it unchanged when Redis is off.

    Both construction sites -- the API and the admin CLI -- go through this, so a rebuild run
    from the command line gets the same cache as one triggered over HTTP.
    """
    from .config import settings

    client = create_client(settings.redis_url)
    if client is None:
        return inner
    return CachingMarketProvider(
        inner,
        client,
        month_ttl_seconds=settings.history_cache_ttl_seconds,
        recent_ttl_seconds=settings.history_recent_ttl_seconds,
    )


def create_client(url: str | None) -> object | None:
    """Build a Redis client, or None when caching is not configured or unreachable."""
    if not url:
        return None
    try:
        import redis
    except ImportError:
        logger.warning("PORTFOLIO_REDIS_URL is set but the redis package is not installed")
        return None
    try:
        client = redis.from_url(
            url,
            decode_responses=True,
            # A cache must never be the slow path. If Redis does not answer quickly, the
            # provider call was going to be slower anyway.
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        client.ping()
    except Exception:
        logger.warning("Redis at %s is unreachable; market caching disabled", url, exc_info=True)
        return None
    return client
