"""A group's value over time, assembled from stored per-portfolio snapshots.

`consolidation.py` answers "what is this group worth on one date" by replaying the journal and
pricing it. Doing that once per day across a multi-year range would replay every portfolio
thousands of times, so this reads the snapshots that `valuation.py` already stored and converts
them. The two modules therefore differ in where their numbers come from, and this one can only
report dates that have a snapshot -- it never builds one.

They differ in a second, more surprising way. `consolidation.py` uses effective-dated membership:
a report for a past date contains the portfolios that were in the group then. A series built that
way shows nothing at all before the group was assembled, which for a group created after years of
trading means a flat zero over the entire history the reader wants to see. This module instead
takes the group's *current* members and charts each from its own first snapshot. The cost is that
the two endpoints give different answers for the same past date, deliberately. See
docs/adr/0001-nav-series-uses-current-membership.md.

That choice makes the series a sum over a changing set of accounts, which is the one way it can
mislead: the total climbs when an account joins, and that climb is indistinguishable from a gain
unless the reader is told. Every point therefore carries how many accounts it covers, and the
series reports the date each account entered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .consolidation import UnconvertedAmount, coverage_percent, get_group, rate_for
from .errors import DomainError
from .fx import Conversion, FxService, FxUnavailable
from .journal import PortfolioKind
from .market import MarketProvider
from .models import Portfolio, PortfolioGroupMember, PortfolioValuationSnapshot
from .services import ZERO, first_event_dates
from .valuation import CALCULATION_VERSION, SnapshotStatus

CALCULATION_METHOD = (
    "Each date sums the stored valuation snapshots of the group's current members, converted to "
    "the reporting currency at the rate in force on that date. Rates never come from after the "
    "date being valued. A member with no snapshot on or before a date contributes nothing and is "
    "not counted as missing, because it did not exist yet; a member that has started but lacks a "
    "snapshot for the date makes that point partial. An amount whose currency pair cannot be "
    "resolved is excluded from that point's totals and reported under `unconverted`."
)


@dataclass
class SeriesPoint:
    """One date's consolidated value, with what it took to reach it."""

    valuation_date: date
    securities_value: Decimal
    cash_value: Decimal
    assets_value: Decimal
    liabilities_value: Decimal
    net_value: Decimal
    # Not "how many accounts exist" but "how many are in this number". A reader comparing two
    # dates needs this to know whether the comparison is between like and like.
    contributing_account_count: int
    converted_value_coverage_percent: Decimal
    status: str
    unconverted: list[UnconvertedAmount] = field(default_factory=list)
    missing_portfolio_ids: list[str] = field(default_factory=list)


@dataclass
class AccountEntry:
    """The date an account's data begins, which is where the series changes what it measures."""

    portfolio_id: str
    portfolio_name: str
    entered_on: date


@dataclass
class NavSeries:
    group_id: str
    group_name: str
    reporting_currency: str
    start_date: date
    end_date: date
    portfolio_ids: list[str]
    points: list[SeriesPoint]
    account_entries: list[AccountEntry]
    calculation_version: str
    calculation_method: str
    total_account_count: int
    partial_points: int
    missing_dates: list[date]
    fx_rates_used: list[Conversion]
    warnings: list[str] = field(default_factory=list)


def build_nav_series(
    session: Session,
    group_id: str,
    provider: MarketProvider,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    reporting_currency: str | None = None,
) -> NavSeries:
    """Sum the group's members day by day across a range.

    `start_date` and `end_date` are optional: omitted, the range spans every date that has a
    snapshot, so a caller can ask for the whole history without first discovering when it begins.
    """
    group = get_group(session, group_id)
    currency = (reporting_currency or group.reporting_currency).upper()
    members = _current_members(session, group_id)

    if not members:
        return _empty_series(group, currency, start_date, end_date)

    portfolio_ids = [item.id for item in members]
    by_date = _snapshots_by_date(session, portfolio_ids)

    if not by_date:
        series = _empty_series(group, currency, start_date, end_date)
        series.portfolio_ids = portfolio_ids
        series.total_account_count = len(members)
        series.warnings.append(
            "No valuation snapshots exist for any member of this group. Build them with "
            "rebuild_valuation_snapshots before reading a series."
        )
        return series

    first, last = _resolve_range(by_date, start_date, end_date)
    fx = FxService(session, provider)
    _prefetch_rates(fx, members, currency, first, last)

    entries = _account_entries(session, members, by_date)
    started_by_date = _started_on_or_before(entries)

    kinds = {item.id: item.kind for item in members}
    points: list[SeriesPoint] = []
    absent: list[date] = []
    rates_used: dict[str, Conversion] = {}

    for day in sorted(by_date):
        if day < first or day > last:
            continue
        point = _point_for(fx, day, by_date[day], kinds, currency, rates_used)
        expected = started_by_date(day)
        point.missing_portfolio_ids = sorted(expected - set(by_date[day]))
        if point.missing_portfolio_ids:
            point.status = SnapshotStatus.PARTIAL
        points.append(point)

    absent = _missing_dates(set(by_date), first, last, started_by_date)
    partial = [item for item in points if item.status == SnapshotStatus.PARTIAL]

    return NavSeries(
        group_id=group.id,
        group_name=group.name,
        reporting_currency=currency,
        start_date=first,
        end_date=last,
        portfolio_ids=portfolio_ids,
        points=points,
        account_entries=entries,
        calculation_version=CALCULATION_VERSION,
        calculation_method=CALCULATION_METHOD,
        total_account_count=len(members),
        partial_points=len(partial),
        missing_dates=absent,
        fx_rates_used=sorted(rates_used.values(), key=lambda item: item.base_currency),
        warnings=_describe(entries, points, absent, partial, currency),
    )


def _current_members(session: Session, group_id: str) -> list[Portfolio]:
    """The group's members as of now, regardless of when each one joined.

    Deliberately not `_members_at`: this series charts the accounts the reader has today across
    the whole history of their data, not the accounts a past report would have contained.
    """
    ids = session.scalars(
        select(PortfolioGroupMember.portfolio_id)
        .where(
            PortfolioGroupMember.group_id == group_id,
            PortfolioGroupMember.effective_to.is_(None),
        )
        .distinct()
    ).all()
    if not ids:
        return []
    rows = session.scalars(
        select(Portfolio).where(Portfolio.id.in_(list(ids))).order_by(Portfolio.created_at)
    ).all()
    return list(rows)


def _snapshots_by_date(
    session: Session, portfolio_ids: list[str]
) -> dict[date, dict[str, PortfolioValuationSnapshot]]:
    """Every member's snapshots, grouped by date.

    One query for the whole range rather than one per portfolio per day: the series is a join
    across members, so it needs them together anyway.
    """
    rows = session.scalars(
        select(PortfolioValuationSnapshot)
        .where(
            PortfolioValuationSnapshot.portfolio_id.in_(portfolio_ids),
            PortfolioValuationSnapshot.calculation_version == CALCULATION_VERSION,
        )
        .order_by(PortfolioValuationSnapshot.valuation_date)
    ).all()

    grouped: dict[date, dict[str, PortfolioValuationSnapshot]] = {}
    for row in rows:
        day = row.valuation_date.date()
        grouped.setdefault(day, {})[row.portfolio_id] = row
    return grouped


def _resolve_range(
    by_date: dict[date, dict[str, PortfolioValuationSnapshot]],
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date]:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise DomainError(
            422,
            "invalid_date_range",
            "start_date must not be after end_date",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
    days = sorted(by_date)
    return (start_date or days[0], end_date or days[-1])


def _prefetch_rates(
    fx: FxService, members: list[Portfolio], currency: str, first: date, last: date
) -> None:
    """Fetch each pair's whole range once, instead of a 30-day window per date."""
    for base in sorted({item.base_currency.upper() for item in members}):
        if base != currency:
            fx.prefetch_range(base, currency, first, last)


def _account_entries(
    session: Session,
    members: list[Portfolio],
    by_date: dict[date, dict[str, PortfolioValuationSnapshot]],
) -> list[AccountEntry]:
    """When each account's data begins.

    `first_event_date` and not the earliest snapshot: a rebuild can store zero-valued snapshots
    for dates before an account did anything, and marking those as the entry would put a label on
    a date where the line does not move. The entry that matters is the first real activity.
    Accounts with no journal events fall back to their first snapshot so they are still declared.
    """
    started = first_event_dates(session, [item.id for item in members])
    earliest: dict[str, date] = {}
    for day, snapshots in by_date.items():
        for portfolio_id in snapshots:
            if portfolio_id not in earliest or day < earliest[portfolio_id]:
                earliest[portfolio_id] = day

    entries: list[AccountEntry] = []
    for member in members:
        entered = started.get(member.id) or earliest.get(member.id)
        if entered is None:
            continue
        entries.append(AccountEntry(member.id, member.name, entered))
    return sorted(entries, key=lambda item: (item.entered_on, item.portfolio_name))


def _started_on_or_before(entries: list[AccountEntry]):
    """A test for which accounts should have a snapshot on a date.

    An account before its entry date is not missing data -- it has none to have. Counting it as a
    gap would mark every point in the years before the newest account as partial, producing a
    warning on 1200 dates that nobody can ever act on.
    """
    ordered = sorted(entries, key=lambda item: item.entered_on)

    def started(day: date) -> set[str]:
        return {item.portfolio_id for item in ordered if item.entered_on <= day}

    return started


def _point_for(
    fx: FxService,
    day: date,
    snapshots: dict[str, PortfolioValuationSnapshot],
    kinds: dict[str, str],
    currency: str,
    rates_used: dict[str, Conversion],
) -> SeriesPoint:
    """One date's totals, converted and split into what is owned and what is owed."""
    rates: dict[str, Conversion] = {}
    unconverted: list[UnconvertedAmount] = []
    securities = cash = assets = liabilities = ZERO
    excluded = ZERO
    counted = 0

    for portfolio_id, snapshot in sorted(snapshots.items()):
        base = snapshot.base_currency.upper()
        conversion = rate_for(fx, base, currency, day, rates)
        if isinstance(conversion, FxUnavailable):
            if snapshot.total_value != ZERO:
                unconverted.append(
                    UnconvertedAmount(base, snapshot.total_value, conversion.reason)
                )
                excluded += abs(snapshot.total_value)
            continue

        rates_used.setdefault(base, conversion)
        counted += 1
        securities += conversion.apply(snapshot.securities_value)
        cash += conversion.apply(snapshot.cash_value)
        total = conversion.apply(snapshot.total_value)
        # A snapshot stores one signed total, so what is owned and what is owed are separated by
        # the account's kind rather than read off the row. A liability's total is already
        # negative, which is what keeps assets + liabilities == net.
        if kinds.get(portfolio_id) == PortfolioKind.LIABILITY:
            liabilities += total
        else:
            assets += total

    net = assets + liabilities
    return SeriesPoint(
        valuation_date=day,
        securities_value=securities,
        cash_value=cash,
        assets_value=assets,
        liabilities_value=liabilities,
        net_value=net,
        contributing_account_count=counted,
        converted_value_coverage_percent=coverage_percent(abs(net), excluded),
        status=SnapshotStatus.PARTIAL if unconverted else SnapshotStatus.COMPLETE,
        unconverted=unconverted,
    )


def _missing_dates(
    present: set[date], first: date, last: date, started_by_date
) -> list[date]:
    """Dates in range where an account that had started has no snapshot at all.

    Only dates with no snapshot for any member appear here; a date where some members are
    present is reported on the point itself. Dates before the first account started are not
    gaps, so they are never listed.
    """
    gaps: list[date] = []
    span = (last - first).days
    for offset in range(span + 1):
        day = first + timedelta(days=offset)
        if day in present:
            continue
        if started_by_date(day):
            gaps.append(day)
    return gaps


def _describe(
    entries: list[AccountEntry],
    points: list[SeriesPoint],
    absent: list[date],
    partial: list[SeriesPoint],
    currency: str,
) -> list[str]:
    """Warnings a reader can act on, and the one fact that changes how the line is read."""
    warnings: list[str] = []

    # Not a gap and not fixable -- a permanent property of the data. It is reported because it
    # changes what the line means, which is exactly what a reader comparing two dates needs.
    if len(entries) > 1 and points:
        first_count = points[0].contributing_account_count
        last_count = points[-1].contributing_account_count
        if first_count != last_count:
            warnings.append(
                f"This series sums a changing set of accounts: {first_count} at the start and "
                f"{last_count} at the end. A rise where an account enters is not a gain. "
                f"See `account_entries` for the {len(entries)} entry dates."
            )

    if absent:
        warnings.append(
            f"{len(absent)} dates in this range have no snapshot for any member and are "
            "reported as missing rather than interpolated"
        )

    incomplete = [item for item in partial if item.missing_portfolio_ids]
    if incomplete:
        warnings.append(
            f"{len(incomplete)} points are partial because an account that had started has no "
            "snapshot on that date, so those totals understate the group"
        )

    unconverted = [item for item in points if item.unconverted]
    if unconverted:
        worst = min(unconverted, key=lambda item: item.converted_value_coverage_percent)
        warnings.append(
            f"{len(unconverted)} points hold value that could not be converted to {currency} and "
            f"is excluded from their totals; coverage is lowest on "
            f"{worst.valuation_date.isoformat()} at "
            f"{worst.converted_value_coverage_percent.quantize(Decimal('0.01'))}%"
        )

    return warnings


def _empty_series(
    group, currency: str, start_date: date | None, end_date: date | None
) -> NavSeries:
    today = date.today()
    return NavSeries(
        group_id=group.id,
        group_name=group.name,
        reporting_currency=currency,
        start_date=start_date or today,
        end_date=end_date or today,
        portfolio_ids=[],
        points=[],
        account_entries=[],
        calculation_version=CALCULATION_VERSION,
        calculation_method=CALCULATION_METHOD,
        total_account_count=0,
        partial_points=0,
        missing_dates=[],
        fx_rates_used=[],
        warnings=["This group has no members, so there is no series to build"],
    )


__all__ = [
    "CALCULATION_METHOD",
    "AccountEntry",
    "NavSeries",
    "SeriesPoint",
    "build_nav_series",
]
