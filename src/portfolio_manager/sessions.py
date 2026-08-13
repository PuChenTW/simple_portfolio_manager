"""Trading-session hours per market, used to decide how long a live quote stays fresh.

A quote's useful lifetime is not a fixed duration -- it is however long the price cannot move.
During a session that is a few minutes; after the close it is the rest of the night, because the
last trade of the day stays the last trade until the next open. Caching both cases under one TTL
means either re-fetching an unchanging price all night or serving a minutes-old price as current.

So `quote_ttl_for` answers "when could this price next change?" rather than "how old is too old?".
The closed-market answer expires exactly at the next open, which needs no tuning: it is derived
from the calendar rather than guessed at.

Weekends count as closed; market holidays deliberately do not. A holiday simply expires the quote
at the notional open, where the provider returns the same unchanged close and the quote is cached
again until the following open. That costs one request per holiday and cannot be wrong, whereas a
hand-maintained holiday table silently freezes prices for a full session once it drifts out of
date. See `docs/ARCHITECTURE.md`.

Sessions are continuous open-to-close spans. Taiwan has no lunch break and the US midday pause
does not exist for regular hours, so a single span per market is accurate; a market with a real
intraday break would need a list of spans here rather than one pair.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Regular cash-session hours in exchange-local time. Pre- and post-market moves exist but are not
# what `regularMarketPrice` reports, so extending these would claim a freshness the quote lacks.
MARKET_SESSIONS: dict[str, tuple[ZoneInfo, time, time]] = {
    "US": (ZoneInfo("America/New_York"), time(9, 30), time(16, 0)),
    "TW": (ZoneInfo("Asia/Taipei"), time(9, 0), time(13, 30)),
    "TWO": (ZoneInfo("Asia/Taipei"), time(9, 0), time(13, 30)),
}

# Crypto never closes, so it has no entry above and always uses the open-market TTL. Listing it
# with 00:00-23:59 hours would be a near-miss: the daily boundary would read as a close and hold a
# moving price until "the next open".
ALWAYS_OPEN = {"CRYPTO"}

SATURDAY = 5


def _is_weekend(day: date) -> bool:
    return day.weekday() >= SATURDAY


def is_market_open(market: str, moment: datetime) -> bool:
    """Whether `market` is inside its regular session at `moment` (an aware UTC datetime)."""
    if market in ALWAYS_OPEN:
        return True
    session = MARKET_SESSIONS.get(market)
    if session is None:
        # An unrecognized market is treated as always open, which keeps the short TTL and the
        # current behavior. Guessing a close for a market whose hours are unknown would freeze a
        # price that may well be moving.
        return True
    tz, opens, closes = session
    local = moment.astimezone(tz)
    if _is_weekend(local.date()):
        return False
    return opens <= local.time() < closes


def seconds_until_next_open(market: str, moment: datetime) -> int | None:
    """Seconds from `moment` until `market` next opens, or None when it has no close.

    Returns None for always-open and unrecognized markets, whose quotes never get the long TTL.
    """
    if market in ALWAYS_OPEN:
        return None
    session = MARKET_SESSIONS.get(market)
    if session is None:
        return None
    tz, opens, _ = session
    local = moment.astimezone(tz)

    # Today's open still counts when the moment precedes it -- an overnight quote held at 03:00
    # must expire at 09:30 today, not tomorrow.
    candidate = datetime.combine(local.date(), opens, tzinfo=tz)
    while candidate <= local or _is_weekend(candidate.date()):
        candidate = datetime.combine(candidate.date() + timedelta(days=1), opens, tzinfo=tz)
    return int((candidate - local).total_seconds())


def quote_ttl_for(market: str, moment: datetime, open_ttl_seconds: int) -> int:
    """How long a quote for `market` stays fresh, in seconds.

    Open markets keep the short configured TTL. Closed ones hold until the next open, floored at
    the open TTL so a quote fetched seconds before the bell is never cached for less than it
    would have been during the session.
    """
    if is_market_open(market, moment):
        return open_ttl_seconds
    until_open = seconds_until_next_open(market, moment)
    if until_open is None:
        return open_ttl_seconds
    return max(until_open, open_ttl_seconds)
