from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import DomainError
from .sessions import MARKET_SESSIONS


def report_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(422, "invalid_report_timezone", "Use an IANA timezone name",
                          {"report_timezone": name}) from exc


def completed_price_date(cutoff: datetime, market: str = "") -> date:
    session = MARKET_SESSIONS.get(market)
    if session is None:
        return cutoff.astimezone(UTC).date() - timedelta(days=1)
    zone, _, close = session
    local = cutoff.astimezone(zone)
    day = local.date()
    if local.time() < close:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


@dataclass(frozen=True)
class ReportTime:
    timezone: str = "UTC"

    def today(self, now: datetime) -> date:
        return now.astimezone(report_zone(self.timezone)).date()

    def end_of_day(self, day: date) -> datetime:
        next_day = datetime.combine(day + timedelta(days=1), time(), report_zone(self.timezone))
        return next_day.astimezone(UTC) - timedelta(microseconds=1)

    def cutoff(self, day: date, now: datetime) -> datetime:
        if day > self.today(now):
            raise DomainError(422, "valuation_date_in_future", "Report date is in the future",
                              {"valuation_date": day.isoformat()})
        return min(self.end_of_day(day), now)
