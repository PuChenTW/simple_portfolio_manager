"""Point-in-time portfolio valuation.

A snapshot answers "what was this portfolio worth on that date", using only information that
existed on that date. Positions and cash come from replaying the journal to the cutoff; prices
come from the provider's history bounded by the same cutoff. Nothing reads the current quote:
using today's price to value a past date is look-ahead, and it makes a backfilled series look
like it predicted the market.

Missing prices are the normal case, not an error. A holding the provider cannot price on that
date is recorded with a null price and excluded from `securities_value`, its cost basis carried
in `unpriced_market_value` so a reader can see what is missing. The snapshot is then `partial`.
Substituting zero would report a smaller portfolio in a form indistinguishable from a real
valuation, which is exactly the failure this design exists to prevent.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError
from .market import MarketDataError, MarketProvider, clean_provider_value
from .models import (
    Instrument,
    PortfolioValuationSnapshot,
    PositionValuationSnapshot,
)
from .replay import ReplayedPosition, replay_state
from .schemas import utc_now
from .services import ZERO, _aware, get_portfolio

# Bump when the meaning of a stored snapshot changes, so old and new revisions stay comparable.
CALCULATION_VERSION = "v1"

# How far back a close may be carried forward before the price is flagged stale. Weekends and
# holidays routinely leave a valuation date without its own bar; a quarter-long gap is different.
STALE_PRICE_DAYS = 5

HUNDRED = Decimal("100")



class SnapshotStatus:
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True)
class PricePoint:
    price: Decimal
    as_of: datetime
    provider: str
    stale: bool
    warnings: list[str]


class HistoricalPricer:
    """Fetches the last close at or before a cutoff, and remembers what it found.

    One instance is used per snapshot run so that a range rebuild fetches each instrument's
    history once instead of once per day. Failures are cached too: a delisted ticker should not
    be re-requested for every date in the range.
    """

    def __init__(self, provider: MarketProvider, *, lookback_days: int = 30) -> None:
        self.provider = provider
        self.lookback_days = lookback_days
        self._bars: dict[str, list[tuple[date, Decimal]]] = {}
        self._failed: dict[str, str] = {}
        self._provider_name: dict[str, str] = {}

    def price_at(self, ticker: str, valuation_date: date) -> PricePoint | None:
        """The most recent close on or before `valuation_date`, or None if there is none."""
        if ticker in self._failed:
            return None
        bars = self._bars.get(ticker)
        if bars is None or (bars and bars[-1][0] < valuation_date):
            bars = self._load(ticker, valuation_date)
        if not bars:
            return None

        usable = [item for item in bars if item[0] <= valuation_date]
        if not usable:
            return None
        observed_date, price = usable[-1]

        gap = (valuation_date - observed_date).days
        warnings: list[str] = []
        if gap > STALE_PRICE_DAYS:
            warnings.append(
                f"No {ticker} price on or near {valuation_date.isoformat()}; carried forward the "
                f"close from {observed_date.isoformat()}, {gap} days earlier"
            )
        return PricePoint(
            price=price,
            as_of=datetime.combine(observed_date, time(0, 0), UTC),
            provider=self._provider_name.get(ticker, "unknown"),
            stale=gap > STALE_PRICE_DAYS,
            warnings=warnings,
        )

    def failure_for(self, ticker: str) -> str | None:
        return self._failed.get(ticker)

    def _load(self, ticker: str, valuation_date: date) -> list[tuple[date, Decimal]]:
        """Request history ending at the cutoff so a later price can never enter the result."""
        try:
            result = self.provider.history(
                ticker,
                start_date=valuation_date - timedelta(days=self.lookback_days),
                end_date=valuation_date,
            )
        except MarketDataError as exc:
            self._failed[ticker] = str(exc)
            self._bars[ticker] = []
            return []

        self._provider_name[ticker] = result.provider
        bars = sorted(
            (bar.timestamp.date(), clean_provider_value(bar.close))
            for bar in result.bars
            # Defensive: a provider that ignores `end_date` must not leak future prices.
            if bar.timestamp.date() <= valuation_date
        )
        self._bars[ticker] = bars
        if not bars:
            self._failed[ticker] = f"No {ticker} price data on or before {valuation_date}"
        return bars


def create_snapshot(
    session: Session,
    portfolio_id: str,
    valuation_date: date,
    provider: MarketProvider,
    *,
    pricer: HistoricalPricer | None = None,
    force_revision: bool = False,
) -> PortfolioValuationSnapshot:
    """Value one portfolio on one date, replacing an existing revision only when asked.

    Re-running for the same date is idempotent: the stored snapshot is returned untouched unless
    `force_revision` is set, so a retried job cannot quietly produce a second version of history.
    """
    portfolio = get_portfolio(session, portfolio_id)
    existing = _existing_snapshot(session, portfolio_id, valuation_date)
    if existing is not None and not force_revision:
        return existing

    now = utc_now()
    if valuation_date > now.date():
        raise DomainError(
            422,
            "valuation_date_in_future",
            "A portfolio cannot be valued on a date that has not happened",
            {"valuation_date": valuation_date.isoformat()},
        )
    # Today's snapshot is capped at the current instant: an end-of-day cutoff on a day still in
    # progress would claim to cover hours that have not happened.
    cutoff = min(_end_of_day(valuation_date), now)

    state = replay_state(session, portfolio_id, cutoff)
    pricer = pricer or HistoricalPricer(provider)
    tickers = _instrument_currencies(session)

    priced: list[tuple[ReplayedPosition, PricePoint | None]] = []
    for position in state.positions:
        if position.quantity == ZERO:
            continue  # Fully exited before the cutoff; it held nothing on this date.
        priced.append((position, pricer.price_at(position.ticker, valuation_date)))

    snapshot = _build_snapshot(portfolio, valuation_date, cutoff, state, priced, pricer)

    if existing is not None:
        session.delete(existing)
        session.flush()
    session.add(snapshot)
    for position, price in priced:
        session.add(
            _build_position_row(
                snapshot,
                position,
                price,
                valuation_date,
                tickers.get(position.ticker, portfolio.base_currency),
                pricer,
            )
        )
    session.commit()
    return snapshot


def _build_snapshot(
    portfolio,
    valuation_date: date,
    cutoff: datetime,
    state,
    priced: list[tuple[ReplayedPosition, PricePoint | None]],
    pricer: HistoricalPricer,
) -> PortfolioValuationSnapshot:
    securities = ZERO
    unpriced = ZERO
    cost_basis = ZERO
    warnings = list(state.coverage.warnings)
    priced_count = 0

    for position, price in priced:
        cost_basis += position.cost_basis
        if price is None:
            unpriced += position.cost_basis
            reason = pricer.failure_for(position.ticker) or "no price on or before this date"
            warnings.append(
                f"{position.ticker} could not be priced on {valuation_date.isoformat()} "
                f"({reason}); {position.quantity} units are excluded from securities_value"
            )
            continue
        priced_count += 1
        securities += position.quantity * price.price
        warnings.extend(price.warnings)

    total_positions = len(priced)
    coverage = (
        HUNDRED
        if total_positions == 0
        else (Decimal(priced_count) / Decimal(total_positions) * HUNDRED)
    )
    status = (
        SnapshotStatus.COMPLETE if priced_count == total_positions else SnapshotStatus.PARTIAL
    )

    return PortfolioValuationSnapshot(
        id=str(uuid.uuid4()),
        portfolio_id=portfolio.id,
        valuation_date=_start_of_day(valuation_date),
        valuation_as_of=cutoff,
        base_currency=portfolio.base_currency,
        securities_value=securities,
        unpriced_market_value=unpriced,
        cash_value=state.cash,
        total_value=securities + state.cash,
        cost_basis=cost_basis,
        external_flow_amount=state.flows.net_external,
        income_amount=state.flows.income,
        fee_amount=state.flows.fees,
        tax_amount=state.flows.taxes,
        pricing_coverage_percent=coverage,
        positions_total=total_positions,
        positions_priced=priced_count,
        calculation_version=CALCULATION_VERSION,
        status=status,
        warnings=json.dumps(warnings) if warnings else None,
        created_at=utc_now(),
    )


def _build_position_row(
    snapshot: PortfolioValuationSnapshot,
    position: ReplayedPosition,
    price: PricePoint | None,
    valuation_date: date,
    currency: str,
    pricer: HistoricalPricer,
) -> PositionValuationSnapshot:
    warnings = list(price.warnings) if price else [
        pricer.failure_for(position.ticker) or "No price available on or before this date"
    ]
    return PositionValuationSnapshot(
        id=str(uuid.uuid4()),
        portfolio_snapshot_id=snapshot.id,
        instrument_id=position.instrument_id,
        ticker_at_time=position.ticker,
        valuation_date=_start_of_day(valuation_date),
        quantity=position.quantity,
        average_cost=position.average_cost,
        cost_basis=position.cost_basis,
        local_currency=currency,
        price=price.price if price else None,
        market_value=position.quantity * price.price if price else None,
        price_as_of=price.as_of if price else None,
        price_provider=price.provider if price else None,
        price_stale=price.stale if price else False,
        warnings=json.dumps(warnings) if warnings else None,
    )


@dataclass
class RebuildReport:
    """What a range rebuild did, in enough detail to re-run it safely."""

    portfolio_id: str
    start_date: date
    end_date: date
    calculation_version: str
    created: int = 0
    skipped_existing: int = 0
    partial: int = 0
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def rebuild_snapshots(
    session: Session,
    portfolio_id: str,
    start_date: date,
    end_date: date,
    provider: MarketProvider,
    *,
    force_revision: bool = False,
) -> RebuildReport:
    """Build snapshots across a date range as a bounded, re-runnable job.

    Re-running is the expected way to recover from a partial failure: dates that already have a
    snapshot are skipped rather than rewritten, so an interrupted run can simply be repeated.
    One `HistoricalPricer` spans the whole range, which fetches each instrument's history once
    instead of once per day.
    """
    get_portfolio(session, portfolio_id)
    if start_date > end_date:
        raise DomainError(
            422,
            "invalid_date_range",
            "start_date must not be after end_date",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )

    today = utc_now().date()
    report = RebuildReport(
        portfolio_id=portfolio_id,
        start_date=start_date,
        end_date=end_date,
        calculation_version=CALCULATION_VERSION,
    )
    if end_date > today:
        report.warnings.append(
            f"end_date {end_date.isoformat()} is in the future; stopping at {today.isoformat()}"
        )
        end_date = today

    # Reach back far enough that the first date can still find a preceding close.
    span = (end_date - start_date).days + 30
    pricer = HistoricalPricer(provider, lookback_days=span)

    current = start_date
    while current <= end_date:
        existing = _existing_snapshot(session, portfolio_id, current)
        if existing is not None and not force_revision:
            report.skipped_existing += 1
            current += timedelta(days=1)
            continue
        try:
            snapshot = create_snapshot(
                session,
                portfolio_id,
                current,
                provider,
                pricer=pricer,
                force_revision=force_revision,
            )
        except DomainError as exc:
            # One bad date must not abandon the rest of the range.
            session.rollback()
            report.failed.append(f"{current.isoformat()}: {exc.message}")
        else:
            report.created += 1
            if snapshot.status == SnapshotStatus.PARTIAL:
                report.partial += 1
        current += timedelta(days=1)

    if report.partial:
        report.warnings.append(
            f"{report.partial} of {report.created} snapshots are partial because at least one "
            "holding could not be priced on that date"
        )
    return report


def snapshot_warnings(snapshot: PortfolioValuationSnapshot) -> list[str]:
    return json.loads(snapshot.warnings) if snapshot.warnings else []


def list_snapshots(
    session: Session, portfolio_id: str, start_date: date, end_date: date
) -> list[PortfolioValuationSnapshot]:
    """Stored snapshots in a range, oldest first. Absent dates stay absent."""
    return list(
        session.scalars(
            select(PortfolioValuationSnapshot)
            .where(
                PortfolioValuationSnapshot.portfolio_id == portfolio_id,
                PortfolioValuationSnapshot.valuation_date >= _start_of_day(start_date),
                PortfolioValuationSnapshot.valuation_date <= _start_of_day(end_date),
                PortfolioValuationSnapshot.calculation_version == CALCULATION_VERSION,
            )
            .order_by(PortfolioValuationSnapshot.valuation_date)
        ).all()
    )


def missing_dates(
    snapshots: list[PortfolioValuationSnapshot], start_date: date, end_date: date
) -> list[date]:
    """Dates in range with no snapshot.

    These are reported rather than interpolated: a value invented for a gap would be
    indistinguishable from one that was actually computed.
    """
    present = {_aware(snapshot.valuation_date).date() for snapshot in snapshots}
    span = (end_date - start_date).days
    return [
        day
        for day in (start_date + timedelta(days=offset) for offset in range(span + 1))
        if day not in present
    ]


def snapshot_positions(
    session: Session, snapshot_id: str
) -> list[PositionValuationSnapshot]:
    return list(
        session.scalars(
            select(PositionValuationSnapshot)
            .where(PositionValuationSnapshot.portfolio_snapshot_id == snapshot_id)
            .order_by(PositionValuationSnapshot.ticker_at_time)
        ).all()
    )


def _existing_snapshot(
    session: Session, portfolio_id: str, valuation_date: date
) -> PortfolioValuationSnapshot | None:
    return session.scalar(
        select(PortfolioValuationSnapshot).where(
            PortfolioValuationSnapshot.portfolio_id == portfolio_id,
            PortfolioValuationSnapshot.valuation_date == _start_of_day(valuation_date),
            PortfolioValuationSnapshot.calculation_version == CALCULATION_VERSION,
        )
    )


def _instrument_currencies(session: Session) -> dict[str, str]:
    return dict(session.execute(select(Instrument.ticker, Instrument.currency)).all())


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time(0, 0), UTC)


def _end_of_day(value: date) -> datetime:
    """The cutoff instant for a valuation date: everything that happened that day is included."""
    return datetime.combine(value, time(23, 59, 59, 999999), UTC)


__all__ = [
    "CALCULATION_VERSION",
    "HistoricalPricer",
    "PricePoint",
    "SnapshotStatus",
    "create_snapshot",
    "snapshot_warnings",
]
