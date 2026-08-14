"""Moving cash between two portfolios as one indivisible operation.

A journal event belongs to exactly one portfolio, so a transfer is two events -- an outflow from
the source and an inflow to the destination -- sharing a `transfer_id` and committed together.
Each event balances on its own, in its own portfolio's currency, which is what lets `replay` and
`valuation` stay untouched: every portfolio still sees one ordinary event of a type it knows.

The alternative, one event holding legs in two currencies, was rejected. `validate_balance` nets
legs into a single functional currency, so such an event balances only after converting one side,
and a residual that is zero in one currency's terms is not a balanced event -- it is an unbalanced
one wearing an exchange rate.

Cross-currency transfers carry the rate the user actually got. This module never asks `fx.py` for
one: a market rate differs from an executed rate, and the difference would land in the ledger as
cash that appeared from nowhere.
"""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError, not_found
from .journal import EventStatus, EventType, LegType, invert, require_balanced
from .models import JournalEvent, Portfolio
from .postings import (
    TransactionRequest,
    _apply_projections,
    _cash_pair,
    _legs_of,
    _persist,
    leg_metadata,
)
from .services import ZERO, _fingerprint, get_portfolio

TRANSFER_OUT = "out"
TRANSFER_IN = "in"


def _transfer_fingerprint(
    from_portfolio_id: str,
    to_portfolio_id: str,
    amount: Decimal,
    fx_rate: Decimal | None,
) -> str:
    return _fingerprint(
        {
            "from": from_portfolio_id,
            "to": to_portfolio_id,
            "amount": format(amount, "f"),
            "fx_rate": format(fx_rate, "f") if fx_rate is not None else None,
        }
    )


def _validate(
    source: Portfolio,
    destination: Portfolio,
    amount: Decimal,
    fx_rate: Decimal | None,
) -> Decimal:
    """Check the pair and return what the destination receives."""
    if source.id == destination.id:
        raise DomainError(
            422,
            "self_transfer",
            "The source and destination portfolios are the same",
            {"portfolio_id": source.id},
        )
    if amount <= ZERO:
        raise DomainError(
            422,
            "invalid_amount",
            "amount must be greater than zero; direction comes from the two portfolios",
            {"amount": format(amount, "f")},
        )

    same_currency = source.base_currency == destination.base_currency
    if same_currency and fx_rate is not None:
        raise DomainError(
            422,
            "unexpected_fx_rate",
            "Both portfolios use the same currency, so there is no rate to apply",
            {"currency": source.base_currency, "fx_rate": format(fx_rate, "f")},
        )
    if not same_currency and fx_rate is None:
        raise DomainError(
            422,
            "fx_rate_required",
            "A cross-currency transfer must carry the rate actually executed; this service will "
            "not supply a market rate, which would book a gain or loss that never happened",
            {
                "from_currency": source.base_currency,
                "to_currency": destination.base_currency,
            },
        )
    if fx_rate is not None and fx_rate <= ZERO:
        raise DomainError(
            422,
            "invalid_amount",
            "fx_rate must be greater than zero",
            {"fx_rate": format(fx_rate, "f")},
        )

    return amount if fx_rate is None else amount * fx_rate


def events_of(session: Session, transfer_id: str) -> list[JournalEvent]:
    """Every event carrying this transfer id, originals and reversals alike."""
    return list(
        session.scalars(
            select(JournalEvent)
            .where(JournalEvent.transfer_id == transfer_id)
            .order_by(JournalEvent.created_at, JournalEvent.id)
        ).all()
    )


def _originals(events: list[JournalEvent]) -> tuple[JournalEvent, JournalEvent]:
    """The two posted halves, out first."""
    out_event = next(
        (e for e in events if e.transfer_role == TRANSFER_OUT and e.reverses_event_id is None),
        None,
    )
    in_event = next(
        (e for e in events if e.transfer_role == TRANSFER_IN and e.reverses_event_id is None),
        None,
    )
    if out_event is None or in_event is None:
        raise not_found("transfer", events[0].transfer_id if events else "")
    return out_event, in_event


def transfer_cash(
    session: Session,
    from_portfolio_id: str,
    to_portfolio_id: str,
    request_id: str,
    amount: Decimal,
    *,
    fx_rate: Decimal | None = None,
    occurred_at: datetime | None = None,
    source_reference: str | None = None,
    memo: str | None = None,
) -> tuple[JournalEvent, JournalEvent]:
    """Move cash between two portfolios, writing both halves or neither.

    Repeating a `request_id` with the same details returns the existing pair; repeating it with
    different details is a conflict, exactly as it is for a single transaction.
    """
    source = get_portfolio(session, from_portfolio_id)
    destination = get_portfolio(session, to_portfolio_id)
    received = _validate(source, destination, amount, fx_rate)
    fingerprint = _transfer_fingerprint(from_portfolio_id, to_portfolio_id, amount, fx_rate)

    existing = session.scalar(
        select(JournalEvent).where(
            JournalEvent.portfolio_id == from_portfolio_id,
            JournalEvent.request_id == request_id,
        )
    )
    if existing is not None:
        if existing.transfer_id is None or existing.request_fingerprint != fingerprint:
            raise DomainError(
                409,
                "idempotency_conflict",
                "request_id was already used for different data",
                {"request_id": request_id, "event_id": existing.id},
            )
        return _originals(events_of(session, existing.transfer_id))

    transfer_id = str(uuid4())
    out_legs = _cash_pair(
        source.base_currency, amount, LegType.OTHER, "external", inflow=False
    )
    in_legs = _cash_pair(
        destination.base_currency, received, LegType.OTHER, "external", inflow=True
    )

    # The rate goes in metadata, never in Leg.fx_rate: `functional_amount` multiplies by that
    # field unconditionally, and both legs here are already in their event's own currency, so a
    # rate there would scale one side of a balanced pair and break the event.
    if fx_rate is not None:
        out_legs[1].metadata = leg_metadata(
            {
                "transfer_id": transfer_id,
                "counterparty_portfolio_id": destination.id,
                "fx_rate": format(fx_rate, "f"),
                "counterparty_amount": format(received, "f"),
                "counterparty_currency": destination.base_currency,
            }
        )
        in_legs[1].metadata = leg_metadata(
            {
                "transfer_id": transfer_id,
                "counterparty_portfolio_id": source.id,
                "fx_rate": format(fx_rate, "f"),
                "counterparty_amount": format(amount, "f"),
                "counterparty_currency": source.base_currency,
            }
        )

    require_balanced(out_legs, source.base_currency)
    require_balanced(in_legs, destination.base_currency)

    def request(event_type: EventType) -> TransactionRequest:
        return TransactionRequest(
            request_id=request_id,
            event_type=event_type,
            amount=amount,
            occurred_at=occurred_at,
            source="api",
            source_reference=source_reference,
            memo=memo,
        )

    try:
        out_event = _persist(
            session,
            source,
            request(EventType.TRANSFER_OUT),
            out_legs,
            fingerprint,
            event_type=EventType.TRANSFER_OUT,
            transfer_id=transfer_id,
            transfer_role=TRANSFER_OUT,
        )
        _apply_projections(session, from_portfolio_id, out_legs, allow_negative_cash=False)

        in_event = _persist(
            session,
            destination,
            request(EventType.TRANSFER_IN),
            in_legs,
            fingerprint,
            event_type=EventType.TRANSFER_IN,
            transfer_id=transfer_id,
            transfer_role=TRANSFER_IN,
        )
        _apply_projections(session, to_portfolio_id, in_legs, allow_negative_cash=False)

        # One commit for both halves: neither can survive the other's failure.
        session.commit()
    except Exception:
        session.rollback()
        raise
    return out_event, in_event


def reverse_transfer(
    session: Session,
    transfer_id: str,
    request_id: str,
    *,
    memo: str | None = None,
) -> tuple[JournalEvent, JournalEvent]:
    """Unwind both halves of a transfer together.

    Reversing the inflow can overdraw the destination if the money was already spent. For an
    asset book that is refused rather than allowed negative: the remedy is to move the cash back
    first, and an overdrawn bank balance is a fiction the ledger declines to record.

    A liability book is the exception, and it is not one this function has to make. A loan is
    negative for its whole life, so `_apply_projections` treats a negative balance there as the
    account's normal state; the `allow_negative_cash=False` below still means what it says for
    every other kind.
    """
    events = events_of(session, transfer_id)
    if not events:
        raise not_found("transfer", transfer_id)
    out_event, in_event = _originals(events)

    for event in (out_event, in_event):
        if event.status == EventStatus.REVERSED.value:
            raise DomainError(
                409,
                "already_reversed",
                "This transfer was reversed already; reversals are not repeatable",
                {"transfer_id": transfer_id, "event_id": event.id},
            )

    existing = session.scalar(
        select(JournalEvent).where(
            JournalEvent.portfolio_id == out_event.portfolio_id,
            JournalEvent.request_id == request_id,
        )
    )
    if existing is not None:
        return _originals(events_of(session, transfer_id))

    reversal_memo = memo or f"Reversal of transfer {transfer_id}"
    fingerprint = _fingerprint({"reverses_transfer": transfer_id})

    try:
        reversals: list[JournalEvent] = []
        for event in (out_event, in_event):
            legs = invert(_legs_of(session, event.id))
            require_balanced(legs, event.functional_currency)
            portfolio = session.get(Portfolio, event.portfolio_id)
            reversal = _persist(
                session,
                portfolio,
                TransactionRequest(
                    request_id=request_id,
                    event_type=EventType.REVERSAL,
                    source="api",
                    source_reference=event.source_reference,
                    memo=reversal_memo,
                ),
                legs,
                fingerprint,
                event_type=EventType.REVERSAL,
                reverses_event_id=event.id,
                transfer_id=transfer_id,
                transfer_role=event.transfer_role,
            )
            _apply_projections(
                session, event.portfolio_id, legs, allow_negative_cash=False
            )
            event.status = EventStatus.REVERSED.value
            reversals.append(reversal)

        session.commit()
    except Exception:
        session.rollback()
        raise
    return reversals[0], reversals[1]
