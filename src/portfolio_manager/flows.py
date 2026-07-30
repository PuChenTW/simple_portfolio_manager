"""Human rulings on what a legacy event's cash movement means.

The journal derives an event's flow classification from its type, which is correct for anything
posted through `record_transaction`. It is not reliable for migrated rows: the pre-journal model
had only `deposit` and `withdraw`, so an operator settling a day's trading recorded a withdrawal
that looks identical to money leaving the portfolio.

The distinction decides whether TWR treats a cash movement as investor capital to neutralize or
as part of the return being measured. Getting it backwards makes trading proceeds look like
contributions, which systematically understates performance.

Nothing here edits a posted event. An override is a separate, higher-ranked opinion that replay
reads in place of the derived value, and retracting it restores the original reading -- the same
provenance discipline the instrument classifications use.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError, not_found
from .identity import Provenance
from .journal import EventType, FlowClassification, LegType, classify_flow
from .models import EventFlowClassification, JournalEvent, JournalLeg
from .schemas import utc_now
from .services import ZERO

# Only a person can settle this; a provider has no view on what an operator meant.
OVERRIDE_PROVENANCE = Provenance.MANUAL_OVERRIDE


@dataclass
class FlowSuggestion:
    """Evidence for how one event probably should be classified.

    This is deliberately advisory. The evidence is presented so a person can rule on it; nothing
    applies a suggestion automatically, because a confident guess written into the record is
    indistinguishable from a confirmed fact.
    """

    event_id: str
    occurred_at: object
    event_type: str
    # The operator's original label from before the migration, when one was recorded.
    source_reference: str | None
    cash_delta: Decimal
    current: FlowClassification
    suggested: FlowClassification
    confidence: str
    evidence: list[str] = field(default_factory=list)


def resolve_flow(session: Session, event: JournalEvent) -> FlowClassification:
    """The classification in force for an event: an active override, else the derived value."""
    override = _active_override(session, event.id)
    if override is not None:
        return FlowClassification(override.classification)
    return _derived(event)


def resolve_flows(session: Session, event_ids: list[str]) -> dict[str, FlowClassification]:
    """Active overrides for many events at once, keyed by event id."""
    if not event_ids:
        return {}
    rows = session.scalars(
        select(EventFlowClassification).where(
            EventFlowClassification.event_id.in_(event_ids),
            EventFlowClassification.provenance == OVERRIDE_PROVENANCE.value,
            EventFlowClassification.is_retracted.is_(False),
        )
    ).all()
    return {row.event_id: FlowClassification(row.classification) for row in rows}


def set_flow_override(
    session: Session,
    event_id: str,
    *,
    classification: str,
    reason: str,
    source: str = "operator",
    retract: bool = False,
) -> EventFlowClassification:
    """Record or retract a ruling on one event. The event itself is never modified."""
    event = session.get(JournalEvent, event_id)
    if event is None:
        raise not_found("journal_event", event_id)

    try:
        value = FlowClassification(classification)
    except ValueError:
        raise DomainError(
            422,
            "unknown_flow_classification",
            "Flow classification must be external, internal, or unknown",
            {
                "classification": classification,
                "supported": [item.value for item in FlowClassification],
            },
        ) from None

    if not reason.strip():
        raise DomainError(
            422,
            "missing_reason",
            "A reclassification must record why, so it can be audited later",
            {"event_id": event_id},
        )

    now = utc_now()
    existing = session.scalar(
        select(EventFlowClassification).where(
            EventFlowClassification.event_id == event_id,
            EventFlowClassification.provenance == OVERRIDE_PROVENANCE.value,
        )
    )
    if existing is not None:
        existing.classification = value.value
        existing.reason = reason
        existing.source = source
        existing.is_retracted = retract
        existing.effective_at = now
        existing.updated_at = now
        return existing

    row = EventFlowClassification(
        id=str(uuid.uuid4()),
        event_id=event_id,
        classification=value.value,
        provenance=OVERRIDE_PROVENANCE.value,
        source=source,
        reason=reason,
        effective_at=now,
        is_retracted=retract,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row


def suggest_reclassifications(
    session: Session, portfolio_id: str, *, tolerance: Decimal = Decimal("1")
) -> list[FlowSuggestion]:
    """Propose a classification for each unruled legacy cash event, with the evidence.

    The signal is arithmetic, not naming: when a day's trades net to roughly the same amount as
    that day's cash movement, the cash almost certainly settled the trading rather than crossing
    the portfolio boundary. Names are reported as supporting evidence only, because an operator's
    labelling convention is not a fact about the money.
    """
    events = list(
        session.scalars(
            select(JournalEvent)
            .where(
                JournalEvent.portfolio_id == portfolio_id,
                JournalEvent.is_unlinked_legacy.is_(True),
            )
            .order_by(JournalEvent.occurred_at, JournalEvent.id)
        ).all()
    )
    if not events:
        return []

    ruled = set(resolve_flows(session, [event.id for event in events]))
    cash_by_event = _cash_deltas(session, [event.id for event in events])
    trade_net_by_day = _trade_net_by_day(events, cash_by_event)

    suggestions: list[FlowSuggestion] = []
    for event in events:
        if event.id in ruled:
            continue
        cash_delta = cash_by_event.get(event.id, ZERO)
        if cash_delta == ZERO:
            continue  # A migrated trade with no cash leg has nothing to reclassify.

        day = event.occurred_at.date()
        trade_net = trade_net_by_day.get(day, ZERO)
        suggestions.append(
            _suggest_one(event, cash_delta, trade_net, day, tolerance)
        )
    return suggestions


def _suggest_one(
    event: JournalEvent,
    cash_delta: Decimal,
    trade_net: Decimal,
    day: object,
    tolerance: Decimal,
) -> FlowSuggestion:
    evidence: list[str] = []
    difference = (cash_delta - trade_net).copy_abs()

    if trade_net != ZERO and difference <= tolerance:
        suggested = FlowClassification.INTERNAL
        confidence = "high"
        evidence.append(
            f"Trades on {day} net to {trade_net:,.2f} and this cash movement is "
            f"{cash_delta:,.2f}, a difference of {difference:,.2f}: the amounts match, so this "
            "records the settlement of that trading rather than money crossing the boundary"
        )
    elif trade_net == ZERO:
        suggested = FlowClassification.EXTERNAL
        confidence = "high"
        evidence.append(
            f"No trades occurred on {day}, so this cash movement cannot be a trade settlement"
        )
    else:
        suggested = FlowClassification.EXTERNAL
        confidence = "low"
        evidence.append(
            f"Trades on {day} net to {trade_net:,.2f} but this cash movement is "
            f"{cash_delta:,.2f}, a difference of {difference:,.2f}: too far apart to be that "
            "day's settlement, though it may be a partial one"
        )

    # The operator's own label, preserved by the backfill. Naming is weak evidence -- a habit,
    # not a fact about the money -- so it is reported alongside the arithmetic, never instead.
    original = event.source_reference or ""
    lowered = original.lower()
    if any(token in lowered for token in ("adjust", "settle", "net")):
        evidence.append(
            f"The original request_id ({original}) suggests a balance adjustment, which supports "
            "but does not prove an internal settlement"
        )
    elif any(token in lowered for token in ("opening", "initial", "fund")):
        evidence.append(
            f"The original request_id ({original}) suggests opening capital, which supports but "
            "does not prove an external contribution"
        )

    return FlowSuggestion(
        event_id=event.id,
        occurred_at=event.occurred_at,
        event_type=event.event_type,
        source_reference=event.source_reference,
        cash_delta=cash_delta,
        current=_derived(event),
        suggested=suggested,
        confidence=confidence,
        evidence=evidence,
    )


def _trade_net_by_day(
    events: list[JournalEvent], cash_by_event: dict[str, Decimal]
) -> dict[object, Decimal]:
    """Net cash a day's trades would have produced, had settlements been recorded.

    A migrated buy carries the consideration on its security leg and no cash leg, so the amount
    it *would* have settled is recoverable even though the settlement itself was never written.
    """
    totals: dict[object, Decimal] = {}
    for event in events:
        if event.event_type not in {EventType.BUY.value, EventType.SELL.value}:
            continue
        security_amount = cash_by_event.get(f"security:{event.id}", ZERO)
        if security_amount == ZERO:
            continue
        day = event.occurred_at.date()
        # A buy's security leg is positive (cost capitalized); the cash it consumed is negative.
        totals[day] = totals.get(day, ZERO) - security_amount
    return totals


def _cash_deltas(session: Session, event_ids: list[str]) -> dict[str, Decimal]:
    """Net cash per event, plus each event's security-leg total under a `security:` key."""
    totals: dict[str, Decimal] = {}
    for leg in session.scalars(
        select(JournalLeg).where(JournalLeg.event_id.in_(event_ids))
    ).all():
        amount = leg.amount_delta or ZERO
        if leg.leg_type == LegType.CASH.value:
            totals[leg.event_id] = totals.get(leg.event_id, ZERO) + amount
        elif leg.leg_type == LegType.SECURITY.value:
            key = f"security:{leg.event_id}"
            totals[key] = totals.get(key, ZERO) + amount
    return totals


def _active_override(session: Session, event_id: str) -> EventFlowClassification | None:
    return session.scalar(
        select(EventFlowClassification).where(
            EventFlowClassification.event_id == event_id,
            EventFlowClassification.provenance == OVERRIDE_PROVENANCE.value,
            EventFlowClassification.is_retracted.is_(False),
        )
    )


def _derived(event: JournalEvent) -> FlowClassification:
    try:
        return classify_flow(EventType(event.event_type))
    except ValueError:
        return FlowClassification.UNKNOWN
