"""Rebuild portfolio state at any past cutoff by folding the journal.

The stored `positions` and `cash_balances` tables are projections of the present: they record
where the portfolio is now, not where it was. Every historical number this service reports --
NAV snapshots, TWR, XIRR -- needs the state as of some earlier instant, so it is derived here
instead of being read from those tables.

Two properties make the fold safe. Events are immutable, so replaying the same journal always
yields the same state. Corrections are reversals posted as their own events with inverted legs,
so folding every event in order cancels a reversed event against its reversal arithmetically --
no filtering is required, and none is done.

Where the journal is genuinely incomplete, this module says so. Legacy trades migrated by
`backfill` carry no cash leg, because the original schema never recorded which cash transaction
settled which trade. Replayed cash therefore overstates reality for those portfolios. That gap is
reported through `ReplayCoverage` so callers can mark a snapshot partial, and it is never closed
by inference: a guessed settlement is indistinguishable from a recorded one once written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .flows import resolve_flows
from .journal import EventType, FlowClassification, LegType, classify_flow
from .models import Instrument, JournalEvent, JournalLeg
from .services import ZERO, _aware


@dataclass
class ReplayedPosition:
    """A holding as it stood at the cutoff."""

    instrument_id: str
    ticker: str
    quantity: Decimal = ZERO
    average_cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.average_cost


@dataclass
class FlowTotals:
    """Cash movement over the replayed window, split by what it means for performance.

    External flows are investor contributions and withdrawals, which TWR must neutralize.
    Income, fees, and taxes are internal: the portfolio earned or incurred them, so they belong
    to the return rather than to the capital base.

    Signs: `external_in` is positive and `external_out` negative, so `net_external` is the net
    capital the investor added. `income`, `fees`, and `taxes` are magnitudes -- a fee of 5 reads
    as 5, not -5 -- because they are an informational breakdown rather than a running balance.
    """

    external_in: Decimal = ZERO
    external_out: Decimal = ZERO
    income: Decimal = ZERO
    fees: Decimal = ZERO
    taxes: Decimal = ZERO
    unknown: Decimal = ZERO

    @property
    def net_external(self) -> Decimal:
        return self.external_in + self.external_out


@dataclass
class ReplayCoverage:
    """How much of the replayed state rests on a complete record."""

    events_applied: int = 0
    unlinked_legacy_events: int = 0
    # Migrated events a person has since ruled on; no longer a gap, but still worth reporting.
    reclassified_legacy_events: int = 0
    unknown_flow_events: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.unlinked_legacy_events == 0 and self.unknown_flow_events == 0


@dataclass
class ReplayResult:
    portfolio_id: str
    cutoff: datetime
    cash: Decimal
    positions: list[ReplayedPosition]
    flows: FlowTotals
    coverage: ReplayCoverage


def replay_state(
    session: Session,
    portfolio_id: str,
    cutoff: datetime,
    *,
    since: datetime | None = None,
) -> ReplayResult:
    """Fold every event up to and including `cutoff` into positions, cash, and flow totals.

    `since` bounds only the flow totals, which are a per-period measure; positions and cash are
    always folded from the beginning, because a balance is cumulative and a partial fold would
    silently report a portfolio smaller than it is.
    """
    events = _events_through(session, portfolio_id, cutoff)
    tickers = _ticker_index(session)
    reversed_types = {event.id: event.event_type for event, _ in events}
    # A human ruling on what a migrated cash movement meant outranks the type-derived guess.
    overrides = resolve_flows(session, [event.id for event, _ in events])

    cash = ZERO
    positions: dict[str, ReplayedPosition] = {}
    flows = FlowTotals()
    coverage = ReplayCoverage()

    for event, legs in events:
        coverage.events_applied += 1
        if event.is_unlinked_legacy and event.id not in overrides:
            coverage.unlinked_legacy_events += 1
        elif event.is_unlinked_legacy:
            coverage.reclassified_legacy_events += 1

        # SQLite hands back naive datetimes even for timezone-aware columns.
        in_window = since is None or _aware(event.occurred_at) >= _aware(since)
        cash += _fold_event(
            event,
            legs,
            positions,
            tickers,
            flows,
            coverage,
            in_window,
            reversed_types,
            overrides,
        )

    _describe_gaps(coverage)
    return ReplayResult(
        portfolio_id=portfolio_id,
        cutoff=_aware(cutoff),
        cash=cash,
        positions=sorted(positions.values(), key=lambda item: item.ticker),
        flows=flows,
        coverage=coverage,
    )


def _fold_event(
    event: JournalEvent,
    legs: list[JournalLeg],
    positions: dict[str, ReplayedPosition],
    tickers: dict[str, str],
    flows: FlowTotals,
    coverage: ReplayCoverage,
    in_window: bool,
    reversed_types: dict[str, str],
    overrides: dict[str, FlowClassification],
) -> Decimal:
    """Apply one event's legs; returns its net cash delta."""
    cash_delta = ZERO
    for leg in legs:
        if leg.leg_type == LegType.SECURITY.value and leg.instrument_id:
            _fold_security_leg(leg, positions, tickers)
        elif leg.leg_type == LegType.CASH.value:
            cash_delta += leg.amount_delta or ZERO

    if in_window:
        _accumulate_flows(event, legs, cash_delta, flows, coverage, reversed_types, overrides)
    return cash_delta


def _fold_security_leg(
    leg: JournalLeg, positions: dict[str, ReplayedPosition], tickers: dict[str, str]
) -> None:
    """Mirror `postings._apply_position` exactly: replay must agree with what was posted."""
    instrument_id = leg.instrument_id
    assert instrument_id is not None  # guarded by the caller
    position = positions.get(instrument_id)
    if position is None:
        position = ReplayedPosition(
            instrument_id=instrument_id,
            ticker=tickers.get(instrument_id, instrument_id),
        )
        positions[instrument_id] = position

    quantity_delta = leg.quantity_delta or ZERO
    amount_delta = leg.amount_delta or ZERO

    if quantity_delta > ZERO:
        total_cost = position.cost_basis + amount_delta
        position.quantity += quantity_delta
        position.average_cost = total_cost / position.quantity
    elif quantity_delta < ZERO:
        sold = -quantity_delta
        position.realized_pnl += -amount_delta - sold * position.average_cost
        position.quantity -= sold
        if position.quantity == ZERO:
            position.average_cost = ZERO
    elif amount_delta != ZERO and position.quantity > ZERO:
        # Return of capital: cash arrives against basis without changing the share count.
        adjusted = position.cost_basis + amount_delta
        position.average_cost = adjusted / position.quantity if adjusted > ZERO else ZERO


def _accumulate_flows(
    event: JournalEvent,
    legs: list[JournalLeg],
    cash_delta: Decimal,
    flows: FlowTotals,
    coverage: ReplayCoverage,
    reversed_types: dict[str, str],
    overrides: dict[str, FlowClassification],
) -> None:
    """Attribute an event's cash movement to the category that performance measurement needs."""
    override = overrides.get(event.id)
    if override is not None:
        # A ruling settles the question; the event type is what was unreliable in the first place.
        _apply_classification(override, event, legs, cash_delta, flows, coverage)
        return

    event_type = _effective_type(event, reversed_types)
    if event_type is None:
        coverage.unknown_flow_events += 1
        flows.unknown += cash_delta
        return

    _apply_classification(classify_flow(event_type), event, legs, cash_delta, flows, coverage)


def _apply_classification(
    classification: FlowClassification,
    event: JournalEvent,
    legs: list[JournalLeg],
    cash_delta: Decimal,
    flows: FlowTotals,
    coverage: ReplayCoverage,
) -> None:
    if classification is FlowClassification.EXTERNAL:
        if cash_delta >= ZERO:
            flows.external_in += cash_delta
        else:
            flows.external_out += cash_delta
        return
    if classification is FlowClassification.UNKNOWN:
        coverage.unknown_flow_events += 1
        flows.unknown += cash_delta
        return

    # Internal events: report the components without letting them touch the capital base.
    for leg in legs:
        amount = _leg_magnitude(leg)
        if leg.leg_type == LegType.INCOME.value:
            flows.income += amount
        elif leg.leg_type == LegType.FEE.value:
            flows.fees += amount
        elif leg.leg_type == LegType.TAX.value:
            flows.taxes += amount


def _leg_magnitude(leg: JournalLeg) -> Decimal:
    """The size of an income, fee, or tax leg, independent of its bookkeeping sign.

    Legs are signed so that every event sums to zero, which puts income at a credit (negative)
    and expenses at a debit. These totals are reported as magnitudes instead: a dividend is
    positive income and a fee is a positive cost, which is what a performance breakdown means.

    Trade fees and taxes are `capitalized` -- their money already sits inside the security leg,
    so they carry no `amount_delta` and keep their value in metadata to avoid double-counting.
    """
    if leg.amount_delta is not None:
        return abs(leg.amount_delta)
    if not leg.leg_metadata:
        return ZERO
    try:
        return abs(Decimal(str(json.loads(leg.leg_metadata).get("amount", "0"))))
    except (ValueError, ArithmeticError, TypeError):
        return ZERO


def _describe_gaps(coverage: ReplayCoverage) -> None:
    if coverage.reclassified_legacy_events:
        coverage.warnings.append(
            f"{coverage.reclassified_legacy_events} migrated events were reclassified by hand "
            "rather than derived from their event type; see the flow-classification overrides "
            "for the reason recorded against each"
        )
    if coverage.unlinked_legacy_events:
        coverage.warnings.append(
            f"{coverage.unlinked_legacy_events} migrated legacy events carry no settlement "
            "linkage, so replayed cash does not reflect the trades that consumed it; the "
            "linkage was never recorded and has not been inferred"
        )
    if coverage.unknown_flow_events:
        coverage.warnings.append(
            f"{coverage.unknown_flow_events} events could not be classified as external or "
            "internal cash flow, which makes any return computed over this period unreliable"
        )


def _events_through(
    session: Session, portfolio_id: str, cutoff: datetime
) -> list[tuple[JournalEvent, list[JournalLeg]]]:
    """Every event at or before the cutoff, oldest first, each with its legs.

    Ordering ties break on `id` so that two events sharing a timestamp always fold in the same
    sequence -- without it, a rebuild could produce a different average cost than the original.
    """
    events = list(
        session.scalars(
            select(JournalEvent)
            .where(
                JournalEvent.portfolio_id == portfolio_id,
                JournalEvent.occurred_at <= _aware(cutoff),
            )
            .order_by(JournalEvent.occurred_at, JournalEvent.id)
        ).all()
    )
    if not events:
        return []

    legs_by_event: dict[str, list[JournalLeg]] = {}
    for leg in session.scalars(
        select(JournalLeg)
        .where(JournalLeg.event_id.in_([event.id for event in events]))
        .order_by(JournalLeg.id)
    ).all():
        legs_by_event.setdefault(leg.event_id, []).append(leg)

    return [(event, legs_by_event.get(event.id, [])) for event in events]


def _ticker_index(session: Session) -> dict[str, str]:
    return dict(session.execute(select(Instrument.instrument_id, Instrument.ticker)).all())


def _effective_type(event: JournalEvent, reversed_types: dict[str, str]) -> EventType | None:
    """The event type that governs flow classification.

    A reversal has no economic meaning of its own -- it undoes another event and must land in the
    same flow category, with the opposite sign. Classifying it on its own `reversal` type would
    push a reversed deposit into `unknown` and raise a data-quality warning for what is actually
    a fully recorded correction.
    """
    value = event.event_type
    if value == EventType.REVERSAL.value and event.reverses_event_id:
        value = reversed_types.get(event.reverses_event_id, value)
    try:
        return EventType(value)
    except ValueError:
        return None
