"""Atomic posting of journal events and their position/cash projections.

Every public function here does all of its work inside one database transaction: legs, position
rows, and the cash balance move together or not at all. The old `create_trade` path updated a
position without touching cash by design; `record_transaction` instead records both sides of a
settlement as one event, which is what makes the ledger auditable.

Nothing is inferred. A sell that would overdraw a position, a settlement that does not balance, or
a reused `request_id` carrying different data is rejected rather than reconciled.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .errors import DomainError, not_found
from .identity import resolve_instrument
from .journal import (
    EventStatus,
    EventType,
    Leg,
    LegType,
    classify_flow,
    invert,
    require_balanced,
)
from .models import CashBalance, Instrument, JournalEvent, JournalLeg, Portfolio, Position
from .schemas import utc_now
from .services import ZERO, _aware, _fingerprint, get_portfolio

# Event types whose legs move a security position.
_SECURITY_EVENTS = frozenset({EventType.BUY, EventType.SELL})


@dataclass(frozen=True)
class TransactionRequest:
    """Normalized input for one posting, independent of the HTTP schema."""

    request_id: str
    event_type: EventType
    amount: Decimal | None = None
    ticker: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    fee: Decimal = ZERO
    tax: Decimal = ZERO
    settlement_amount: Decimal | None = None
    occurred_at: datetime | None = None
    trade_date: datetime | None = None
    settlement_date: datetime | None = None
    source: str = "api"
    source_reference: str | None = None
    memo: str | None = None


def _transaction_fingerprint(data: TransactionRequest) -> str:
    """Identify the payload so a reused request_id with different data is a conflict."""
    payload = {
        "event_type": data.event_type.value,
        "ticker": (data.ticker or "").strip().upper(),
        "quantity": _optional_decimal(data.quantity),
        "unit_price": _optional_decimal(data.unit_price),
        "amount": _optional_decimal(data.amount),
        "fee": format(data.fee, "f"),
        "tax": format(data.tax, "f"),
        "settlement_amount": _optional_decimal(data.settlement_amount),
    }
    return _fingerprint(payload)


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _require(value: Decimal | None, field: str, event_type: EventType) -> Decimal:
    if value is None:
        raise DomainError(
            422,
            "missing_field",
            f"{field} is required for a {event_type.value} transaction",
            {"field": field, "event_type": event_type.value},
        )
    return value


def build_legs(
    data: TransactionRequest, currency: str, instrument: Instrument | None
) -> list[Leg]:
    """Construct the legs for one event.

    Buys and sells derive settlement cash from consideration plus costs unless the caller supplied
    an explicit `settlement_amount`, so a broker's exact figure always wins over a computed one.
    """
    event_type = data.event_type

    if event_type in _SECURITY_EVENTS:
        if instrument is None:
            raise DomainError(
                422,
                "missing_field",
                "ticker is required for a trade transaction",
                {"event_type": event_type.value},
            )
        quantity = _require(data.quantity, "quantity", event_type)
        unit_price = _require(data.unit_price, "unit_price", event_type)
        consideration = quantity * unit_price
        signed_quantity = quantity if event_type is EventType.BUY else -quantity
        costs = data.fee + data.tax
        # The security leg carries consideration *plus* transaction costs, because that total is
        # what capitalizes into cost basis (a buy) or nets against proceeds (a sell). Splitting the
        # costs onto their own legs alone would understate basis, matching the legacy trade path.
        security_amount = (
            consideration + costs if event_type is EventType.BUY else -(consideration - costs)
        )
        default_cash = (
            -(consideration + costs)
            if event_type is EventType.BUY
            else consideration - costs
        )
        cash_amount = (
            data.settlement_amount if data.settlement_amount is not None else default_cash
        )

        legs = [
            Leg(
                leg_type=LegType.SECURITY,
                currency=currency,
                account_role="position",
                amount_delta=security_amount,
                quantity_delta=signed_quantity,
                unit_price=unit_price,
                instrument_id=instrument.instrument_id,
            )
        ]
        # Fee and tax are recorded as their own legs so the amounts stay queryable, but the money
        # is already inside `security_amount`. They are marked `capitalized` and carry no monetary
        # delta, which keeps the event balanced while preserving the breakdown.
        for cost, leg_type in ((data.fee, LegType.FEE), (data.tax, LegType.TAX)):
            if cost:
                legs.append(
                    Leg(
                        leg_type=leg_type,
                        currency=currency,
                        account_role="capitalized",
                        amount_delta=None,
                        metadata=leg_metadata({"amount": format(cost, "f")}),
                    )
                )
        legs.append(
            Leg(
                leg_type=LegType.CASH,
                currency=currency,
                account_role="settlement",
                amount_delta=cash_amount,
            )
        )
        return legs

    amount = _require(data.amount, "amount", event_type)
    if amount <= ZERO:
        raise DomainError(
            422,
            "invalid_amount",
            "amount must be greater than zero; direction comes from the transaction type",
            {"event_type": event_type.value, "amount": format(amount, "f")},
        )

    if event_type in {EventType.DEPOSIT, EventType.TRANSFER_IN}:
        return _cash_pair(currency, amount, LegType.OTHER, "external", inflow=True)
    if event_type in {EventType.WITHDRAWAL, EventType.TRANSFER_OUT}:
        return _cash_pair(currency, amount, LegType.OTHER, "external", inflow=False)
    if event_type in {EventType.DIVIDEND, EventType.INTEREST}:
        return _income_legs(data, currency, instrument, amount)
    if event_type in {EventType.FEE, EventType.TAX}:
        leg_type = LegType.FEE if event_type is EventType.FEE else LegType.TAX
        return _cash_pair(currency, amount, leg_type, "expense", inflow=False)

    raise DomainError(
        422,
        "unsupported_event_type",
        "This transaction type cannot be recorded through record_transaction",
        {"event_type": event_type.value},
    )


def _cash_pair(
    currency: str, amount: Decimal, counter_type: LegType, role: str, *, inflow: bool
) -> list[Leg]:
    """A cash movement and its balancing counterpart leg."""
    cash_delta = amount if inflow else -amount
    return [
        Leg(
            leg_type=LegType.CASH,
            currency=currency,
            account_role="settlement",
            amount_delta=cash_delta,
        ),
        Leg(
            leg_type=counter_type,
            currency=currency,
            account_role=role,
            amount_delta=-cash_delta,
        ),
    ]


def _income_legs(
    data: TransactionRequest, currency: str, instrument: Instrument | None, gross: Decimal
) -> list[Leg]:
    """Gross income, withholding tax, and the net cash actually received.

    Keeping gross and withholding separate is the point: net-only records lose the tax figure
    permanently, and income must stay distinguishable from an investor contribution.
    """
    net = gross - data.tax
    legs = [
        Leg(
            leg_type=LegType.INCOME,
            currency=currency,
            account_role="income",
            amount_delta=-gross,
            instrument_id=instrument.instrument_id if instrument else None,
        )
    ]
    if data.tax:
        legs.append(
            Leg(
                leg_type=LegType.TAX,
                currency=currency,
                account_role="withholding",
                amount_delta=data.tax,
            )
        )
    legs.append(
        Leg(
            leg_type=LegType.CASH,
            currency=currency,
            account_role="settlement",
            amount_delta=net,
        )
    )
    return legs


def _apply_projections(
    session: Session, portfolio_id: str, legs: list[Leg], *, allow_negative_cash: bool
) -> None:
    """Move positions and cash to match the legs, inside the caller's transaction."""
    now = utc_now()

    for leg in legs:
        if leg.leg_type is not LegType.SECURITY or leg.instrument_id is None:
            continue
        instrument = session.scalar(
            select(Instrument).where(Instrument.instrument_id == leg.instrument_id)
        )
        if instrument is None:
            raise not_found("instrument", leg.instrument_id)
        _apply_position(session, portfolio_id, instrument, leg, now)

    cash_delta = sum(
        (leg.amount_delta or ZERO for leg in legs if leg.leg_type is LegType.CASH), start=ZERO
    )
    if cash_delta == ZERO:
        return

    balance = session.get(CashBalance, portfolio_id)
    if balance is None:
        balance = CashBalance(portfolio_id=portfolio_id, amount=ZERO, updated_at=now)
        session.add(balance)
    new_amount = balance.amount + cash_delta
    if new_amount < ZERO and not allow_negative_cash:
        raise DomainError(
            422,
            "insufficient_cash",
            "This transaction would overdraw the portfolio cash balance",
            {"available": format(balance.amount, "f"), "change": format(cash_delta, "f")},
        )
    balance.amount = new_amount
    balance.updated_at = now


def _apply_position(
    session: Session,
    portfolio_id: str,
    instrument: Instrument,
    leg: Leg,
    now: datetime,
) -> None:
    """Update quantity, moving-average cost, and realized P&L for one security leg."""
    quantity_delta = leg.quantity_delta or ZERO
    position = session.get(Position, (portfolio_id, instrument.ticker))
    if position is None:
        position = Position(
            portfolio_id=portfolio_id,
            ticker=instrument.ticker,
            quantity=ZERO,
            average_cost=ZERO,
            realized_pnl=ZERO,
            updated_at=now,
        )
        session.add(position)

    if quantity_delta > ZERO:
        # Cost carried into the position is the consideration plus the costs on this leg's event.
        total_cost = position.quantity * position.average_cost + (leg.amount_delta or ZERO)
        position.quantity += quantity_delta
        position.average_cost = total_cost / position.quantity
    elif quantity_delta < ZERO:
        sold = -quantity_delta
        if sold > position.quantity:
            raise DomainError(
                422,
                "insufficient_position",
                "Sell quantity exceeds the current position",
                {
                    "ticker": instrument.ticker,
                    "available": format(position.quantity, "f"),
                    "requested": format(sold, "f"),
                },
            )
        proceeds = -(leg.amount_delta or ZERO)
        position.realized_pnl += proceeds - sold * position.average_cost
        position.quantity -= sold
        if position.quantity == ZERO:
            position.average_cost = ZERO
    position.updated_at = now


def _persist(
    session: Session,
    portfolio: Portfolio,
    data: TransactionRequest,
    legs: list[Leg],
    fingerprint: str,
    *,
    event_type: EventType,
    reverses_event_id: str | None = None,
) -> JournalEvent:
    """Write the event header and its legs. The caller owns the transaction boundary."""
    now = utc_now()
    event = JournalEvent(
        id=str(uuid4()),
        portfolio_id=portfolio.id,
        request_id=data.request_id,
        request_fingerprint=fingerprint,
        event_type=event_type.value,
        status=EventStatus.POSTED.value,
        functional_currency=portfolio.base_currency,
        occurred_at=_aware(data.occurred_at) if data.occurred_at else now,
        trade_date=_aware(data.trade_date) if data.trade_date else None,
        settlement_date=_aware(data.settlement_date) if data.settlement_date else None,
        source=data.source,
        source_reference=data.source_reference,
        memo=data.memo,
        reverses_event_id=reverses_event_id,
        created_at=now,
    )
    session.add(event)
    session.flush()
    for leg in legs:
        session.add(
            JournalLeg(
                id=str(uuid4()),
                event_id=event.id,
                leg_type=leg.leg_type.value,
                instrument_id=leg.instrument_id,
                currency=leg.currency,
                quantity_delta=leg.quantity_delta,
                amount_delta=leg.amount_delta,
                unit_price=leg.unit_price,
                fx_rate=leg.fx_rate,
                account_role=leg.account_role,
                leg_metadata=leg.metadata,
            )
        )
    return event


def record_transaction(
    session: Session,
    portfolio_id: str,
    data: TransactionRequest,
    *,
    allow_negative_cash: bool = False,
) -> JournalEvent:
    """Post one balanced event and its projections atomically.

    A repeat of the same `request_id` with the same payload returns the original event without
    posting again; the same ID with different data is a conflict, never a second entry.
    """
    portfolio = get_portfolio(session, portfolio_id)
    fingerprint = _transaction_fingerprint(data)

    existing = session.scalar(
        select(JournalEvent).where(
            JournalEvent.portfolio_id == portfolio_id,
            JournalEvent.request_id == data.request_id,
        )
    )
    if existing is not None:
        if existing.request_fingerprint == fingerprint:
            return existing
        raise DomainError(
            409,
            "idempotency_conflict",
            "request_id was already used with different transaction data",
            {"request_id": data.request_id},
        )

    instrument = resolve_instrument(session, data.ticker) if data.ticker else None
    if instrument is not None and instrument.currency != portfolio.base_currency:
        raise DomainError(
            422,
            "currency_mismatch",
            "Instrument currency must match the portfolio base currency",
            {
                "ticker": instrument.ticker,
                "instrument_currency": instrument.currency,
                "portfolio_currency": portfolio.base_currency,
            },
        )

    legs = build_legs(data, portfolio.base_currency, instrument)
    require_balanced(legs, portfolio.base_currency)

    try:
        event = _persist(
            session, portfolio, data, legs, fingerprint, event_type=data.event_type
        )
        _apply_projections(
            session, portfolio_id, legs, allow_negative_cash=allow_negative_cash
        )
        session.commit()
    except Exception:
        # Legs, positions, and cash must never survive independently of one another.
        session.rollback()
        raise
    return event


def reverse_transaction(
    session: Session,
    portfolio_id: str,
    event_id: str,
    request_id: str,
    *,
    memo: str | None = None,
    allow_negative_cash: bool = False,
) -> JournalEvent:
    """Undo a posted event by writing its mirror image; the original is never modified."""
    portfolio = get_portfolio(session, portfolio_id)
    original = session.get(JournalEvent, event_id)
    if original is None or original.portfolio_id != portfolio_id:
        raise not_found("journal_event", event_id)
    if original.status == EventStatus.REVERSED.value:
        raise DomainError(
            409,
            "already_reversed",
            "This event was already reversed; post a replacement instead",
            {"event_id": event_id},
        )
    if original.reverses_event_id is not None:
        raise DomainError(
            409,
            "cannot_reverse_a_reversal",
            "A reversal cannot itself be reversed; post a replacement instead",
            {"event_id": event_id},
        )

    existing = session.scalar(
        select(JournalEvent).where(
            JournalEvent.portfolio_id == portfolio_id, JournalEvent.request_id == request_id
        )
    )
    if existing is not None:
        return existing

    legs = invert(_legs_of(session, event_id))
    require_balanced(legs, original.functional_currency)

    request = TransactionRequest(
        request_id=request_id,
        event_type=EventType.REVERSAL,
        source="api",
        source_reference=original.source_reference,
        memo=memo or f"Reversal of {original.event_type} event {event_id}",
    )
    try:
        reversal = _persist(
            session,
            portfolio,
            request,
            legs,
            _fingerprint({"reverses": event_id}),
            event_type=EventType.REVERSAL,
            reverses_event_id=event_id,
        )
        _apply_projections(
            session, portfolio_id, legs, allow_negative_cash=allow_negative_cash
        )
        original.status = EventStatus.REVERSED.value
        session.commit()
    except Exception:
        session.rollback()
        raise
    return reversal


def _row_to_leg(row: JournalLeg) -> Leg:
    """Rebuild the in-memory leg from its stored row, renaming `leg_metadata` back to `metadata`."""
    return Leg(
        leg_type=LegType(row.leg_type),
        currency=row.currency,
        account_role=row.account_role,
        amount_delta=row.amount_delta,
        quantity_delta=row.quantity_delta,
        instrument_id=row.instrument_id,
        unit_price=row.unit_price,
        fx_rate=row.fx_rate,
        metadata=row.leg_metadata,
    )


def _legs_of(session: Session, event_id: str) -> list[Leg]:
    rows = session.scalars(
        select(JournalLeg).where(JournalLeg.event_id == event_id).order_by(JournalLeg.id)
    ).all()
    return [_row_to_leg(row) for row in rows]


def legs_for_events(session: Session, event_ids: list[str]) -> dict[str, list[Leg]]:
    """Legs for a whole page of events in one query, keyed by event id.

    Reading a page and then fetching each event's legs individually costs a query per row; a
    caller rendering a day of activity pays for the page twice over. Events with no legs are
    simply absent from the mapping, so callers use `.get(event_id, [])`.
    """
    if not event_ids:
        return {}

    legs_by_event: dict[str, list[Leg]] = {}
    for row in session.scalars(
        select(JournalLeg).where(JournalLeg.event_id.in_(event_ids)).order_by(JournalLeg.id)
    ).all():
        legs_by_event.setdefault(row.event_id, []).append(_row_to_leg(row))
    return legs_by_event


def ticker_index(session: Session) -> dict[str, str]:
    """Map every instrument id to its ticker, so a page of legs resolves in one query.

    Legs store `instrument_id`, a surrogate nobody can read. Resolving one ticker per leg would
    reintroduce the per-row cost this module exists to avoid.
    """
    return dict(session.execute(select(Instrument.instrument_id, Instrument.ticker)).all())


def event_detail(session: Session, portfolio_id: str, event_id: str) -> dict:
    """Return an event with its legs, balance validation, and reversal chain."""
    event = session.get(JournalEvent, event_id)
    if event is None or event.portfolio_id != portfolio_id:
        raise not_found("journal_event", event_id)

    legs = _legs_of(session, event_id)
    from .journal import validate_balance

    report = validate_balance(legs, event.functional_currency) if legs else None
    reversed_by = session.scalar(
        select(JournalEvent.id).where(JournalEvent.reverses_event_id == event_id)
    )
    return {
        "event": event,
        "legs": legs,
        "balance": report,
        "flow_classification": classify_flow(EventType(event.event_type))
        if event.event_type in {member.value for member in EventType}
        else None,
        "reverses_event_id": event.reverses_event_id,
        "reversed_by_event_id": reversed_by,
    }


def list_events(
    session: Session,
    portfolio_id: str,
    *,
    event_type: str | None = None,
    instrument_reference: str | None = None,
    source_reference: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[JournalEvent], int]:
    """Page the journal, newest first, with the filters an audit actually needs."""
    get_portfolio(session, portfolio_id)
    query = select(JournalEvent).where(JournalEvent.portfolio_id == portfolio_id)
    counter = (
        select(func.count())
        .select_from(JournalEvent)
        .where(JournalEvent.portfolio_id == portfolio_id)
    )

    if event_type:
        query = query.where(JournalEvent.event_type == event_type)
        counter = counter.where(JournalEvent.event_type == event_type)
    if source_reference:
        query = query.where(JournalEvent.source_reference == source_reference)
        counter = counter.where(JournalEvent.source_reference == source_reference)
    if start:
        query = query.where(JournalEvent.occurred_at >= _aware(start))
        counter = counter.where(JournalEvent.occurred_at >= _aware(start))
    if end:
        query = query.where(JournalEvent.occurred_at <= _aware(end))
        counter = counter.where(JournalEvent.occurred_at <= _aware(end))
    if instrument_reference:
        instrument = resolve_instrument(session, instrument_reference)
        matching = select(JournalLeg.event_id).where(
            JournalLeg.instrument_id == instrument.instrument_id
        )
        query = query.where(JournalEvent.id.in_(matching))
        counter = counter.where(JournalEvent.id.in_(matching))

    total = session.scalar(counter) or 0
    events = session.scalars(
        query.order_by(JournalEvent.occurred_at.desc(), JournalEvent.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return list(events), total


def leg_metadata(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
