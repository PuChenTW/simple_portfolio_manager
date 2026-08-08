"""Time-weighted and money-weighted return.

The two answer different questions and neither replaces the other. TWR measures how the holdings
performed, neutralizing the effect of money arriving or leaving: it is what you compare against a
benchmark. XIRR measures what the investor actually earned on the capital they had at risk, so
the timing of contributions moves it. A portfolio whose big deposit landed just before a rally
has an XIRR well above its TWR, and neither number is wrong.

Everything here reads stored snapshots and the journal; nothing is recomputed from today's
prices. A period with a gap in its snapshot series, an unpriced holding, or an unruled legacy
cash event is reported with that gap named, because a return computed over an incomplete series
is not a smaller truth -- it is a different number wearing the same label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, DivisionByZero, InvalidOperation

from sqlalchemy.orm import Session

from .errors import DomainError
from .models import PortfolioValuationSnapshot
from .replay import replay_state
from .services import ZERO, get_portfolio
from .valuation import (
    CALCULATION_VERSION,
    SnapshotStatus,
    list_snapshots,
    missing_dates,
)

ONE = Decimal("1")
HUNDRED = Decimal("100")
DAYS_PER_YEAR = Decimal("365")

# Named so a stored result can be compared against one computed by a later revision.
TWR_METHOD = "modified-dietz-daily-v1"
XIRR_METHOD = "newton-bisection-actual-365-v1"

TWR_METHOD_DESCRIPTION = (
    "將每日子期間報酬以幾何方式串連。每日報酬為 (期末淨值 - 期初淨值 - 現金流) / "
    "(期初淨值 + 加權現金流)，其中當日發生的現金流以一半權重計入，"
    "因為 journal 之前的資料只記錄日期、沒有時間點。若分母為零，"
    "該日視為無報酬貢獻，而非全額虧損。"
)
XIRR_METHOD_DESCRIPTION = (
    "使外部現金流與期末淨值折現後總和為零的利率，以牛頓法求解，"
    "並在牛頓法失敗時改用二分法。投入資金記為負值，提領與期末淨值記為正值；"
    "天數採實際天數並以 365 天為基準年化。"
)

# A flow weight of one half assumes a flow arrives mid-day. The legacy rows carry a date only,
# so a more precise convention would be false precision rather than better accuracy.
MIDDAY_WEIGHT = Decimal("0.5")


@dataclass
class DailyReturn:
    """One sub-period, kept so a caller can see which day drove a result."""

    valuation_date: date
    beginning_value: Decimal
    ending_value: Decimal
    external_flow: Decimal
    return_percent: Decimal | None


@dataclass
class PerformanceCoverage:
    """Whether the series behind a return is complete enough to trust it."""

    snapshots_used: int = 0
    missing_dates: list[date] = field(default_factory=list)
    partial_snapshots: int = 0
    unclassified_flow_events: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def is_reliable(self) -> bool:
        return (
            not self.missing_dates
            and self.partial_snapshots == 0
            and self.unclassified_flow_events == 0
        )


@dataclass
class PerformanceResult:
    portfolio_id: str
    base_currency: str
    start_date: date
    end_date: date
    beginning_value: Decimal
    ending_value: Decimal
    external_inflows: Decimal
    external_outflows: Decimal
    income: Decimal
    fees: Decimal
    taxes: Decimal
    twr_percent: Decimal | None
    annualized_twr_percent: Decimal | None
    xirr_percent: Decimal | None
    xirr_unavailable_reason: str | None
    twr_method: str
    xirr_method: str
    calculation_version: str
    coverage: PerformanceCoverage
    daily_returns: list[DailyReturn]


def calculate_performance(
    session: Session,
    portfolio_id: str,
    start_date: date,
    end_date: date,
    *,
    include_daily: bool = False,
) -> PerformanceResult:
    """Measure return over a period from stored snapshots and journal flows."""
    portfolio = get_portfolio(session, portfolio_id)
    if start_date > end_date:
        raise DomainError(
            422,
            "invalid_date_range",
            "start_date must not be after end_date",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )

    snapshots = list_snapshots(session, portfolio_id, start_date, end_date)
    coverage = _assess_coverage(session, portfolio_id, snapshots, start_date, end_date)

    if len(snapshots) < 2:
        return _insufficient(portfolio, start_date, end_date, snapshots, coverage)

    daily = _daily_returns(session, portfolio_id, snapshots)
    twr = _link(daily)
    opening, closing = snapshots[0], snapshots[-1]
    window = _window_flows(session, portfolio_id, snapshots)

    xirr, xirr_reason = _xirr(
        session, portfolio_id, snapshots, opening.total_value, closing.total_value
    )
    if twr is None:
        coverage.warnings.append(
            "No day in this period had a value to measure against, so there is no return to "
            "report. This is normal for a portfolio that was empty throughout."
        )

    return PerformanceResult(
        portfolio_id=portfolio_id,
        base_currency=portfolio.base_currency,
        start_date=_date_of(opening),
        end_date=_date_of(closing),
        beginning_value=opening.total_value,
        ending_value=closing.total_value,
        external_inflows=window["inflows"],
        external_outflows=window["outflows"],
        income=window["income"],
        fees=window["fees"],
        taxes=window["taxes"],
        # Null, not zero: a period where no day had a measurable base has no return, and
        # reporting 0% would claim the portfolio was flat when nothing was actually measured.
        twr_percent=None if twr is None else _as_percent(twr),
        annualized_twr_percent=_annualize(twr, _date_of(opening), _date_of(closing)),
        xirr_percent=xirr,
        xirr_unavailable_reason=xirr_reason,
        twr_method=TWR_METHOD,
        xirr_method=XIRR_METHOD,
        calculation_version=CALCULATION_VERSION,
        coverage=coverage,
        daily_returns=daily if include_daily else [],
    )


def _daily_returns(
    session: Session, portfolio_id: str, snapshots: list[PortfolioValuationSnapshot]
) -> list[DailyReturn]:
    """Modified Dietz return for each consecutive pair of snapshots."""
    results: list[DailyReturn] = []
    for previous, current in zip(snapshots, snapshots[1:], strict=False):
        start_value = previous.total_value
        end_value = current.total_value
        flow = _external_flow_between(session, portfolio_id, previous, current)

        denominator = start_value + flow * MIDDAY_WEIGHT
        if denominator == ZERO:
            # An empty portfolio that received money has no return to report for that day;
            # calling it zero or infinite would both be inventions.
            results.append(
                DailyReturn(_date_of(current), start_value, end_value, flow, None)
            )
            continue

        gain = end_value - start_value - flow
        results.append(
            DailyReturn(
                _date_of(current), start_value, end_value, flow, _as_percent(gain / denominator)
            )
        )
    return results


def _link(daily: list[DailyReturn]) -> Decimal | None:
    """Chain sub-period returns geometrically; days without a return are skipped, not zeroed."""
    usable = [item for item in daily if item.return_percent is not None]
    if not usable:
        return None
    compounded = ONE
    for item in usable:
        compounded *= ONE + item.return_percent / HUNDRED  # type: ignore[operator]
    return compounded - ONE


def _annualize(twr: Decimal | None, start: date, end: date) -> Decimal | None:
    """Scale a period return to a year, but only when the period justifies it.

    Annualizing a few days multiplies noise into a headline figure -- a 1% week becomes 68% a
    year -- so a period under a month returns null rather than a number that looks authoritative.
    """
    if twr is None:
        return None
    days = (end - start).days
    if days < 30:
        return None
    growth = ONE + twr
    if growth <= ZERO:
        return None  # A total loss has no meaningful annual rate.
    try:
        years = Decimal(days) / DAYS_PER_YEAR
        if years == ZERO:
            return None
        # A fractional root is transcendental, so it leaves Decimal for the exponent alone and
        # returns immediately. The accounting values it is derived from stay exact.
        annual = float(growth) ** float(ONE / years)
        return _as_percent(Decimal(str(annual)) - ONE)
    except (InvalidOperation, DivisionByZero, OverflowError, ValueError):
        return None


def _xirr(
    session: Session,
    portfolio_id: str,
    snapshots: list[PortfolioValuationSnapshot],
    beginning_value: Decimal,
    ending_value: Decimal,
) -> tuple[Decimal | None, str | None]:
    """Money-weighted return, or null with the reason it could not be computed."""
    flows: list[tuple[date, Decimal]] = []
    opening = _date_of(snapshots[0])
    closing = _date_of(snapshots[-1])

    # The opening value is capital already at risk, so it enters as an outflow from the investor.
    if beginning_value != ZERO:
        flows.append((opening, -beginning_value))
    for previous, current in zip(snapshots, snapshots[1:], strict=False):
        flow = _external_flow_between(session, portfolio_id, previous, current)
        if flow != ZERO:
            flows.append((_date_of(current), -flow))
    if ending_value != ZERO:
        flows.append((closing, ending_value))

    if len(flows) < 2:
        return None, "Fewer than two cash flows: there is nothing to solve for."
    if all(amount <= ZERO for _, amount in flows) or all(amount >= ZERO for _, amount in flows):
        return None, (
            "All cash flows share one sign, so no rate discounts them to zero. This usually "
            "means the period has contributions but no ending value, or vice versa."
        )
    if (closing - opening).days == 0:
        return None, "The period covers a single day, which is too short to imply an annual rate."

    rate = _solve_xirr(flows, opening)
    if rate is None:
        return None, (
            "No rate converged for these cash flows. This can happen when flows are large "
            "relative to the values they surround."
        )
    return _as_percent(rate), None


def _solve_xirr(
    flows: list[tuple[date, Decimal]], origin: date, *, tolerance: float = 1e-9
) -> Decimal | None:
    """Newton's method, falling back to bisection when it wanders outside a sane range."""
    amounts = [(float((day - origin).days) / 365.0, float(amount)) for day, amount in flows]

    def net_present_value(rate: float) -> float:
        return sum(amount / (1.0 + rate) ** years for years, amount in amounts)

    rate = 0.1
    for _ in range(100):
        try:
            value = net_present_value(rate)
            derivative = sum(
                -years * amount / (1.0 + rate) ** (years + 1.0) for years, amount in amounts
            )
        except (OverflowError, ZeroDivisionError, ValueError):
            break
        if abs(value) < tolerance:
            return Decimal(str(rate))
        if derivative == 0.0:
            break
        step = value / derivative
        rate -= step
        if rate <= -0.9999:  # A rate at -100% makes the discount factor undefined.
            break

    return _bisect_xirr(net_present_value, tolerance)


def _bisect_xirr(net_present_value, tolerance: float) -> Decimal | None:
    """Bracket a sign change across plausible rates, then halve toward the root."""
    low, high = -0.9999, 10.0
    try:
        low_value, high_value = net_present_value(low), net_present_value(high)
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    if low_value * high_value > 0.0:
        return None  # No sign change in range: there is no root to find here.

    for _ in range(200):
        middle = (low + high) / 2.0
        try:
            value = net_present_value(middle)
        except (OverflowError, ZeroDivisionError, ValueError):
            return None
        if abs(value) < tolerance or (high - low) / 2.0 < tolerance:
            return Decimal(str(middle))
        if value * low_value < 0.0:
            high = middle
        else:
            low, low_value = middle, value
    return None


def _external_flow_between(
    session: Session,
    portfolio_id: str,
    previous: PortfolioValuationSnapshot,
    current: PortfolioValuationSnapshot,
) -> Decimal:
    """Net investor capital that moved after one snapshot's cutoff and up to the next.

    Read from the journal rather than differenced from the stored cumulative totals, so a gap in
    the snapshot series cannot silently absorb a flow into the wrong day.
    """
    window_start = _aware_utc(previous.valuation_as_of) + timedelta(microseconds=1)
    return replay_state(
        session, portfolio_id, _aware_utc(current.valuation_as_of), since=window_start
    ).flows.net_external


def _window_flows(
    session: Session, portfolio_id: str, snapshots: list[PortfolioValuationSnapshot]
) -> dict[str, Decimal]:
    """Flow components across the whole period, for the informational breakdown."""
    window_start = _aware_utc(snapshots[0].valuation_as_of) + timedelta(microseconds=1)
    flows = replay_state(
        session, portfolio_id, _aware_utc(snapshots[-1].valuation_as_of), since=window_start
    ).flows
    return {
        "inflows": flows.external_in,
        "outflows": flows.external_out,
        "income": flows.income,
        "fees": flows.fees,
        "taxes": flows.taxes,
    }


def _assess_coverage(
    session: Session,
    portfolio_id: str,
    snapshots: list[PortfolioValuationSnapshot],
    start_date: date,
    end_date: date,
) -> PerformanceCoverage:
    absent = missing_dates(snapshots, start_date, end_date)
    partial = [item for item in snapshots if item.status == SnapshotStatus.PARTIAL]
    # An event whose cash movement cannot be classified sits in neither the capital base nor the
    # return, so both TWR and XIRR are computed over an incomplete picture.
    unclassified = replay_state(
        session, portfolio_id, _end_of_day(end_date)
    ).coverage.unknown_flow_events

    coverage = PerformanceCoverage(
        snapshots_used=len(snapshots),
        missing_dates=absent,
        partial_snapshots=len(partial),
        unclassified_flow_events=unclassified,
    )
    if absent:
        coverage.warnings.append(
            f"{len(absent)} dates in this period have no snapshot. Sub-periods spanning a gap "
            "are linked across it, which understates volatility and can bias the result; build "
            "the missing dates for a return that covers every day."
        )
    if partial:
        coverage.warnings.append(
            f"{len(partial)} snapshots are partial because a holding could not be priced that "
            "day. Their totals exclude that holding, so returns around those dates move for a "
            "reason that is not market performance."
        )
    if unclassified:
        coverage.warnings.append(
            f"{unclassified} events could not be classified as investor capital or portfolio "
            "activity. Their cash sits outside both totals, so TWR and XIRR are measured over an "
            "incomplete picture."
        )
    return coverage


def _insufficient(
    portfolio,
    start_date: date,
    end_date: date,
    snapshots: list[PortfolioValuationSnapshot],
    coverage: PerformanceCoverage,
) -> PerformanceResult:
    """A period needs two snapshots to bound it; say so rather than returning a zero."""
    coverage.warnings.append(
        f"A return needs a snapshot at both ends of the period; {len(snapshots)} were found. "
        "Build them with rebuild_valuation_snapshots and try again."
    )
    only = snapshots[0] if snapshots else None
    return PerformanceResult(
        portfolio_id=portfolio.id,
        base_currency=portfolio.base_currency,
        start_date=start_date,
        end_date=end_date,
        beginning_value=only.total_value if only else ZERO,
        ending_value=only.total_value if only else ZERO,
        external_inflows=ZERO,
        external_outflows=ZERO,
        income=ZERO,
        fees=ZERO,
        taxes=ZERO,
        twr_percent=None,
        annualized_twr_percent=None,
        xirr_percent=None,
        xirr_unavailable_reason="The period does not contain two snapshots to measure between.",
        twr_method=TWR_METHOD,
        xirr_method=XIRR_METHOD,
        calculation_version=CALCULATION_VERSION,
        coverage=coverage,
        daily_returns=[],
    )


def _as_percent(ratio: Decimal) -> Decimal:
    return ratio * HUNDRED


def _date_of(snapshot: PortfolioValuationSnapshot) -> date:
    return snapshot.valuation_date.date()


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59, 999999), UTC)
