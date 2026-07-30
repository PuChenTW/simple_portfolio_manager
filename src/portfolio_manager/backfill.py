"""Backfill legacy trades and cash transactions into the journal.

The old model kept two independent ledgers: a trade never moved cash, and a cash transaction never
referenced a trade. That linkage was never recorded, so it cannot be recovered. A buy and a
withdrawal minutes apart may be one settlement or two unrelated events, and no amount of timestamp
proximity distinguishes them.

Every migrated row therefore becomes its own event marked `is_unlinked_legacy`, which states
plainly that its counterpart is unknown. Pairing them on a heuristic would manufacture settlements
that never happened and would be indistinguishable from real ones once written.

Projections are deliberately not replayed: positions and cash already reflect these rows. This
backfill makes the history auditable, it does not restate it.
"""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .journal import EventStatus, EventType, Leg, LegType
from .models import (
    CashTransaction,
    Instrument,
    JournalEvent,
    JournalLeg,
    Portfolio,
    Trade,
)
from .schemas import utc_now
from .services import ZERO, _aware

LEGACY_SOURCE = "legacy_migration"


@dataclass
class BackfillReport:
    """What the backfill did, and what it deliberately left unresolved."""

    trades_migrated: int = 0
    cash_transactions_migrated: int = 0
    already_migrated: int = 0
    skipped_missing_instrument: int = 0
    unlinked_events: int = 0
    warnings: list[str] | None = None

    def as_dict(self) -> dict:
        return {
            "trades_migrated": self.trades_migrated,
            "cash_transactions_migrated": self.cash_transactions_migrated,
            "already_migrated": self.already_migrated,
            "skipped_missing_instrument": self.skipped_missing_instrument,
            "unlinked_events": self.unlinked_events,
            "warnings": self.warnings or [],
        }


def _legacy_request_id(prefix: str, row_id: str) -> str:
    """A deterministic key so re-running the backfill cannot duplicate an event."""
    return f"{LEGACY_SOURCE}:{prefix}:{row_id}"


def _already_posted(session: Session, portfolio_id: str, request_id: str) -> bool:
    return (
        session.scalar(
            select(JournalEvent.id).where(
                JournalEvent.portfolio_id == portfolio_id,
                JournalEvent.request_id == request_id,
            )
        )
        is not None
    )


def _write_event(
    session: Session,
    portfolio: Portfolio,
    *,
    request_id: str,
    event_type: EventType,
    occurred_at,
    legs: list[Leg],
    memo: str,
) -> JournalEvent:
    now = utc_now()
    event = JournalEvent(
        id=str(uuid4()),
        portfolio_id=portfolio.id,
        request_id=request_id,
        request_fingerprint=request_id,
        event_type=event_type.value,
        status=EventStatus.POSTED.value,
        functional_currency=portfolio.base_currency,
        occurred_at=_aware(occurred_at),
        source=LEGACY_SOURCE,
        memo=memo,
        is_unlinked_legacy=True,
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
                account_role=leg.account_role,
                leg_metadata=leg.metadata,
            )
        )
    return event


def backfill_portfolio(session: Session, portfolio: Portfolio) -> BackfillReport:
    """Convert one portfolio's legacy rows into unlinked journal events."""
    report = BackfillReport(warnings=[])

    trades = session.scalars(
        select(Trade).where(Trade.portfolio_id == portfolio.id).order_by(Trade.executed_at)
    ).all()
    for trade in trades:
        request_id = _legacy_request_id("trade", trade.id)
        if _already_posted(session, portfolio.id, request_id):
            report.already_migrated += 1
            continue

        instrument = session.get(Instrument, trade.ticker)
        if instrument is None:
            report.skipped_missing_instrument += 1
            report.warnings.append(
                f"Trade {trade.id} references unknown instrument {trade.ticker}; it was not "
                "migrated"
            )
            continue

        quantity = trade.quantity if trade.side == "buy" else -trade.quantity
        consideration = trade.quantity * trade.unit_price
        amount = (
            consideration + trade.fee
            if trade.side == "buy"
            else -(consideration - trade.fee)
        )
        legs = [
            Leg(
                leg_type=LegType.SECURITY,
                currency=instrument.currency,
                account_role="position",
                amount_delta=amount,
                quantity_delta=quantity,
                unit_price=trade.unit_price,
                instrument_id=instrument.instrument_id,
            )
        ]
        # No cash leg: the original trade did not move cash, and inventing one here would assert
        # a settlement that was never recorded.
        _write_event(
            session,
            portfolio,
            request_id=request_id,
            event_type=EventType.BUY if trade.side == "buy" else EventType.SELL,
            occurred_at=trade.executed_at,
            legs=legs,
            memo=(
                f"Migrated legacy trade {trade.id}. Settlement cash was recorded separately, if "
                "at all; the counterpart is unknown."
            ),
        )
        report.trades_migrated += 1
        report.unlinked_events += 1

    transactions = session.scalars(
        select(CashTransaction)
        .where(CashTransaction.portfolio_id == portfolio.id)
        .order_by(CashTransaction.occurred_at)
    ).all()
    for transaction in transactions:
        request_id = _legacy_request_id("cash", transaction.id)
        if _already_posted(session, portfolio.id, request_id):
            report.already_migrated += 1
            continue

        inflow = transaction.action == "deposit"
        delta = transaction.amount if inflow else -transaction.amount
        legs = [
            Leg(
                leg_type=LegType.CASH,
                currency=portfolio.base_currency,
                account_role="settlement",
                amount_delta=delta,
            ),
            Leg(
                leg_type=LegType.OTHER,
                currency=portfolio.base_currency,
                account_role="external",
                amount_delta=-delta,
            ),
        ]
        _write_event(
            session,
            portfolio,
            request_id=request_id,
            event_type=EventType.DEPOSIT if inflow else EventType.WITHDRAWAL,
            occurred_at=transaction.occurred_at,
            legs=legs,
            memo=(
                f"Migrated legacy cash transaction {transaction.id}. Whether this settled a trade "
                "was never recorded, so it is classified as an external flow."
            ),
        )
        report.cash_transactions_migrated += 1
        report.unlinked_events += 1

    if report.unlinked_events:
        report.warnings.append(
            f"{report.unlinked_events} migrated events are marked unlinked_legacy: the original "
            "model never recorded which cash transaction settled which trade, so the linkage "
            "cannot be reconstructed and was not guessed. Performance measurement should treat "
            "migrated cash as an external flow only after human confirmation."
        )
    return report


def backfill_all(session: Session) -> BackfillReport:
    """Backfill every portfolio. Safe to re-run; already-migrated rows are skipped."""
    total = BackfillReport(warnings=[])
    portfolios = session.scalars(select(Portfolio).order_by(Portfolio.created_at)).all()
    for portfolio in portfolios:
        report = backfill_portfolio(session, portfolio)
        total.trades_migrated += report.trades_migrated
        total.cash_transactions_migrated += report.cash_transactions_migrated
        total.already_migrated += report.already_migrated
        total.skipped_missing_instrument += report.skipped_missing_instrument
        total.unlinked_events += report.unlinked_events
        total.warnings.extend(report.warnings or [])
    session.commit()
    return total


def verify_projection_consistency(session: Session, portfolio_id: str) -> dict:
    """Compare the journal against the stored balances it was derived from.

    Legacy events carry no settlement cash, so a portfolio with migrated trades is expected to
    disagree. The point is to surface the discrepancy rather than let it pass unnoticed.
    """
    from .models import CashBalance

    legs = session.scalars(
        select(JournalLeg)
        .join(JournalEvent, JournalLeg.event_id == JournalEvent.id)
        .where(
            JournalEvent.portfolio_id == portfolio_id,
            JournalEvent.status == EventStatus.POSTED.value,
        )
    ).all()

    journal_cash = sum(
        (leg.amount_delta or ZERO for leg in legs if leg.leg_type == LegType.CASH.value),
        start=ZERO,
    )
    balance = session.get(CashBalance, portfolio_id)
    stored_cash = balance.amount if balance else ZERO
    difference = stored_cash - journal_cash

    unlinked = session.scalar(
        select(JournalEvent.id).where(
            JournalEvent.portfolio_id == portfolio_id,
            JournalEvent.is_unlinked_legacy.is_(True),
        )
    )
    warnings = []
    if difference != ZERO:
        warnings.append(
            f"Stored cash differs from the journal by {format(difference, 'f')}"
            + (
                "; this portfolio contains unlinked legacy events whose settlement cash was "
                "never recorded, which explains a difference of this kind"
                if unlinked
                else ""
            )
        )
    return {
        "portfolio_id": portfolio_id,
        "stored_cash": stored_cash,
        "journal_cash": journal_cash,
        "difference": difference,
        "consistent": difference == ZERO,
        "has_unlinked_legacy_events": unlinked is not None,
        "warnings": warnings,
    }
