"""Quote freshness must track when a price can actually move, not a fixed clock.

The cases that matter are the boundaries: the bell in both directions, the weekend, the overnight
hours where "next open" means today rather than tomorrow, and crypto -- which has no close and
must never receive the long TTL. Every datetime here is built in exchange-local time and then
converted, so a test says what a trader would say ("Tuesday 10:00 in New York") rather than a UTC
offset that changes with daylight saving.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from portfolio_manager import services
from portfolio_manager.models import QuoteCache
from portfolio_manager.services import MarketService
from portfolio_manager.sessions import (
    is_market_open,
    quote_ttl_for,
    seconds_until_next_open,
)

NY = ZoneInfo("America/New_York")
TAIPEI = ZoneInfo("Asia/Taipei")
OPEN_TTL = 300

HOUR = 3600


def ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


def taipei(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TAIPEI)


# 2026-08-11 is a Tuesday; 2026-08-15 a Saturday; 2026-08-17 the following Monday.
class TestIsMarketOpen:
    @pytest.mark.parametrize(
        "moment,expected",
        [
            (ny(2026, 8, 11, 9, 29), False),  # one minute before the bell
            (ny(2026, 8, 11, 9, 30), True),  # the bell itself is open
            (ny(2026, 8, 11, 12, 0), True),
            (ny(2026, 8, 11, 15, 59), True),
            (ny(2026, 8, 11, 16, 0), False),  # the close is not open
            (ny(2026, 8, 11, 20, 0), False),  # post-market is not the regular session
            (ny(2026, 8, 15, 12, 0), False),  # Saturday midday
            (ny(2026, 8, 16, 12, 0), False),  # Sunday midday
        ],
    )
    def test_us_session_boundaries(self, moment: datetime, expected: bool) -> None:
        assert is_market_open("US", moment) is expected

    @pytest.mark.parametrize(
        "moment,expected",
        [
            (taipei(2026, 8, 11, 8, 59), False),
            (taipei(2026, 8, 11, 9, 0), True),
            (taipei(2026, 8, 11, 13, 29), True),
            (taipei(2026, 8, 11, 13, 30), False),  # Taiwan closes at 13:30, not 16:00
            (taipei(2026, 8, 15, 10, 0), False),
        ],
    )
    def test_taiwan_session_boundaries(self, moment: datetime, expected: bool) -> None:
        assert is_market_open("TW", moment) is expected

    def test_two_shares_taiwan_hours(self) -> None:
        assert is_market_open("TWO", taipei(2026, 8, 11, 10, 0)) is True
        assert is_market_open("TWO", taipei(2026, 8, 11, 14, 0)) is False

    def test_crypto_is_always_open(self) -> None:
        # Including the hours and days every other market is shut.
        assert is_market_open("CRYPTO", ny(2026, 8, 15, 3, 0)) is True
        assert is_market_open("CRYPTO", ny(2026, 8, 11, 23, 0)) is True

    def test_unknown_market_is_treated_as_open(self) -> None:
        """Guessing a close for unknown hours would freeze a price that may be moving."""
        assert is_market_open("XETRA", ny(2026, 8, 15, 3, 0)) is True

    def test_us_session_is_local_not_utc(self) -> None:
        """13:00 UTC is inside the session in winter and outside it in summer."""
        winter = datetime(2026, 1, 13, 15, 0, tzinfo=ZoneInfo("UTC"))  # 10:00 EST
        summer = datetime(2026, 8, 11, 13, 0, tzinfo=ZoneInfo("UTC"))  # 09:00 EDT
        assert is_market_open("US", winter) is True
        assert is_market_open("US", summer) is False


class TestSecondsUntilNextOpen:
    def test_overnight_points_at_this_mornings_open(self) -> None:
        """A quote held at 03:00 must expire at 09:30 today, not tomorrow."""
        assert seconds_until_next_open("US", ny(2026, 8, 11, 3, 0)) == int(6.5 * HOUR)

    def test_after_the_close_points_at_tomorrow(self) -> None:
        # 16:00 Tuesday -> 09:30 Wednesday is 17.5 hours.
        assert seconds_until_next_open("US", ny(2026, 8, 11, 16, 0)) == int(17.5 * HOUR)

    def test_friday_evening_skips_the_weekend(self) -> None:
        # Friday 2026-08-14 17:00 -> Monday 2026-08-17 09:30.
        expected = (16.5 + 24 + 24) * HOUR
        assert seconds_until_next_open("US", ny(2026, 8, 14, 17, 0)) == int(expected)

    def test_saturday_points_at_monday(self) -> None:
        expected = (24 + 24 + 9.5) * HOUR  # Sat 00:00 -> Sun -> Mon 00:00 -> 09:30
        assert seconds_until_next_open("US", ny(2026, 8, 15, 0, 0)) == int(expected)

    def test_during_session_still_reports_the_next_one(self) -> None:
        """The value is only consumed when closed, but must stay coherent regardless."""
        assert seconds_until_next_open("US", ny(2026, 8, 11, 12, 0)) == int(21.5 * HOUR)

    def test_crypto_has_no_next_open(self) -> None:
        assert seconds_until_next_open("CRYPTO", ny(2026, 8, 15, 3, 0)) is None

    def test_unknown_market_has_no_next_open(self) -> None:
        assert seconds_until_next_open("XETRA", ny(2026, 8, 15, 3, 0)) is None


class TestQuoteTtl:
    def test_open_market_keeps_the_short_ttl(self) -> None:
        assert quote_ttl_for("US", ny(2026, 8, 11, 12, 0), OPEN_TTL) == OPEN_TTL

    def test_closed_market_holds_until_the_open(self) -> None:
        assert quote_ttl_for("US", ny(2026, 8, 11, 16, 0), OPEN_TTL) == int(17.5 * HOUR)

    def test_weekend_holds_across_the_whole_weekend(self) -> None:
        ttl = quote_ttl_for("US", ny(2026, 8, 15, 12, 0), OPEN_TTL)
        assert ttl == int((21.5 + 24) * HOUR)

    def test_crypto_never_gets_the_long_ttl(self) -> None:
        """A 24/7 price cached until a notional open would be frozen indefinitely."""
        assert quote_ttl_for("CRYPTO", ny(2026, 8, 15, 3, 0), OPEN_TTL) == OPEN_TTL

    def test_unknown_market_never_gets_the_long_ttl(self) -> None:
        assert quote_ttl_for("XETRA", ny(2026, 8, 15, 3, 0), OPEN_TTL) == OPEN_TTL

    def test_ttl_is_floored_at_the_open_ttl(self) -> None:
        """A quote fetched just before the bell must not be cached for less than 5 minutes."""
        just_before_open = ny(2026, 8, 11, 9, 29)
        assert seconds_until_next_open("US", just_before_open) == 60
        # 60s until the bell, floored up to the 300s a mid-session quote would have received.
        assert quote_ttl_for("US", just_before_open, OPEN_TTL) == OPEN_TTL

    def test_taiwan_closes_earlier_so_holds_longer(self) -> None:
        """The same wall-clock afternoon moment is closed in Taipei and open in New York."""
        afternoon = taipei(2026, 8, 11, 14, 0)
        assert quote_ttl_for("TW", afternoon, OPEN_TTL) == int(19 * HOUR)

    def test_zero_open_ttl_is_respected_when_open(self) -> None:
        """PORTFOLIO_QUOTE_TTL_SECONDS=0 means always refetch; closing must not override that."""
        assert quote_ttl_for("US", ny(2026, 8, 11, 12, 0), 0) == 0


def test_session_hours_cover_every_market_metadata_can_assign() -> None:
    """`_metadata` assigns US, TW, TWO, or CRYPTO. Each needs a defined freshness rule.

    A market added there without an entry here silently falls into the unknown-market branch and
    keeps the short TTL forever -- correct, but never benefiting from the closed-market hold.
    """
    from portfolio_manager.sessions import ALWAYS_OPEN, MARKET_SESSIONS

    assigned = {"US", "TW", "TWO", "CRYPTO"}
    assert assigned <= set(MARKET_SESSIONS) | ALWAYS_OPEN


def test_sessions_are_ordered_open_before_close() -> None:
    from portfolio_manager.sessions import MARKET_SESSIONS

    for market, (_, opens, closes) in MARKET_SESSIONS.items():
        assert isinstance(opens, time) and isinstance(closes, time)
        assert opens < closes, f"{market} closes before it opens"


class TestMarketServiceUsesSessionTtl:
    """The rule only matters if it changes how often the provider is actually called.

    `MarketService.get` reads the wall clock through `services.utc_now`, so each test pins that
    to a chosen moment -- a Tuesday midday session or the Saturday after it -- and backdates the
    stored quote by an hour. An hour is far past the 300s open TTL and far short of any weekend.
    """

    def _service(self, harness):
        session = harness.session_factory()
        return MarketService(session, harness.provider, OPEN_TTL), session

    # Pinned instants are expressed in UTC rather than with an exchange offset: SQLite stores the
    # wall clock and drops the offset, so an offset-carrying `fetched_at` reads back shifted by
    # that offset and the age under test would not be the age intended.
    SATURDAY = datetime(2026, 8, 15, 16, 0, tzinfo=UTC)  # 12:00 Sat in New York, closed
    MIDDAY = datetime(2026, 8, 11, 16, 0, tzinfo=UTC)  # 12:00 Tue in New York, open
    OVERNIGHT = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)  # 03:00 Sat in New York

    def _age_quote(self, session, symbol: str, at: datetime) -> None:
        """Backdate the cached quote an hour before `at`, the pinned 'now'."""
        cached = session.get(QuoteCache, symbol)
        cached.fetched_at = at - timedelta(hours=1)
        session.commit()

    def _get_at(self, monkeypatch, service, symbol: str, moment: datetime):
        monkeypatch.setattr(services, "utc_now", lambda: moment)
        return service.get(symbol)

    def test_closed_market_serves_cache_past_the_open_ttl(self, harness, monkeypatch) -> None:
        saturday = self.SATURDAY
        service, session = self._service(harness)
        self._get_at(monkeypatch, service, "AAPL", saturday)
        session.commit()
        assert harness.provider.calls == ["AAPL"]

        self._age_quote(session, "AAPL", saturday)
        self._get_at(monkeypatch, service, "AAPL", saturday)
        assert harness.provider.calls == ["AAPL"], "closed market must not refetch"

    def test_open_market_refetches_past_the_open_ttl(self, harness, monkeypatch) -> None:
        midday = self.MIDDAY
        service, session = self._service(harness)
        self._get_at(monkeypatch, service, "AAPL", midday)
        session.commit()

        self._age_quote(session, "AAPL", midday)
        self._get_at(monkeypatch, service, "AAPL", midday)
        assert harness.provider.calls == ["AAPL", "AAPL"], "open market must refetch"

    def test_cache_hit_while_closed_is_not_marked_stale(self, harness, monkeypatch) -> None:
        """A closed market's last price is current, not stale.

        Marking it otherwise would put an unclearable warning on every overnight read, which
        AGENTS.md forbids: a warning nobody can act on teaches readers to ignore the real ones.
        """
        saturday = self.SATURDAY
        service, session = self._service(harness)
        self._get_at(monkeypatch, service, "AAPL", saturday)
        session.commit()
        self._age_quote(session, "AAPL", saturday)

        state = self._get_at(monkeypatch, service, "AAPL", saturday)
        assert state.stale is False
        assert state.warnings == []

    def test_crypto_refetches_overnight(self, harness, monkeypatch) -> None:
        """BTC has no close, so an hour-old quote must still be refreshed at 3am Saturday."""
        overnight = self.OVERNIGHT
        service, session = self._service(harness)
        self._get_at(monkeypatch, service, "BTC-USD", overnight)
        session.commit()
        self._age_quote(session, "BTC-USD", overnight)

        self._get_at(monkeypatch, service, "BTC-USD", overnight)
        assert harness.provider.calls == ["BTC-USD", "BTC-USD"]

    def test_taiwan_and_us_diverge_at_the_same_moment(self, harness, monkeypatch) -> None:
        """One instant is closed for Taipei and open for New York; each must act accordingly."""
        moment = self.MIDDAY  # 00:00 next day in Taipei, well after its 13:30 close
        service, session = self._service(harness)
        for symbol in ("AAPL", "2330.TW"):
            self._get_at(monkeypatch, service, symbol, moment)
        session.commit()
        for symbol in ("AAPL", "2330.TW"):
            self._age_quote(session, symbol, moment)

        for symbol in ("AAPL", "2330.TW"):
            self._get_at(monkeypatch, service, symbol, moment)
        assert harness.provider.calls.count("AAPL") == 2, "US open: refetch"
        assert harness.provider.calls.count("2330.TW") == 1, "Taipei closed: serve cache"
