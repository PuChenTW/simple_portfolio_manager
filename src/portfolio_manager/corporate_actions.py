"""Corporate actions: record the facts, preview the effect, then apply it atomically.

Two rules govern this module:

* Recording an action and applying it are separate steps. An announced action is a fact about an
  instrument; applying it to a portfolio changes holdings. `preview_application` shows exactly
  what would happen without writing, so an agent can inspect rounding and unresolved questions
  before committing.
* Where a jurisdiction's cost-basis treatment is genuinely unknown -- a spin-off's allocation
  between parent and child, a return of capital exceeding basis -- the action is recorded with
  `cost_basis_unresolved` and applied without inventing a number. A plausible-looking guess would
  silently corrupt every gain calculation that follows, and would be indistinguishable from a fact.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError, not_found
from .identity import resolve_instrument
from .journal import ActionStatus, ActionType, EventType, Leg, LegType, require_balanced
from .models import (
    CorporateAction,
    CorporateActionApplication,
    Instrument,
    Position,
)
from .postings import TransactionRequest, _apply_projections, _persist
from .schemas import utc_now
from .services import ZERO, _aware, _fingerprint, get_portfolio

HUNDRED = Decimal("100")


class FractionalHandling(StrEnum):
    """What happens to fractional shares a ratio produces."""

    KEEP_FRACTIONAL = "keep_fractional"
    CASH_IN_LIEU = "cash_in_lieu"
    ROUND_DOWN = "round_down"


logger = logging.getLogger(__name__)

# Actions whose correct cost-basis treatment depends on jurisdiction or issuer disclosure that
# this service does not have. They are recorded and reported, never silently allocated.
_UNRESOLVED_BASIS_ACTIONS = frozenset(
    {ActionType.SPINOFF, ActionType.MERGER, ActionType.STOCK_DIVIDEND}
)

# Actions after which a provider retroactively restates its auto-adjusted price history. Cached
# bars for the instrument are stale from the ex-date onward, and no rule here can tell when.
_PRICE_RESTATING_ACTIONS = frozenset(
    {
        ActionType.SPLIT,
        ActionType.REVERSE_SPLIT,
        ActionType.STOCK_DIVIDEND,
        ActionType.CASH_DIVIDEND,
    }
)

_ACTION_EVENT_TYPES = {
    ActionType.CASH_DIVIDEND: EventType.DIVIDEND,
    ActionType.INTEREST: EventType.INTEREST,
    ActionType.SPLIT: EventType.SPLIT,
    ActionType.REVERSE_SPLIT: EventType.SPLIT,
    ActionType.STOCK_DIVIDEND: EventType.STOCK_DIVIDEND,
    ActionType.RETURN_OF_CAPITAL: EventType.RETURN_OF_CAPITAL,
    ActionType.SYMBOL_CHANGE: EventType.SYMBOL_CHANGE,
    ActionType.MERGER: EventType.MERGER,
    ActionType.SPINOFF: EventType.SPINOFF,
}


@dataclass
class ActionPreview:
    """What applying an action would do, computed without writing anything."""

    portfolio_id: str
    action_id: str
    action_type: str
    applicable: bool
    original_quantity: Decimal | None = None
    original_average_cost: Decimal | None = None
    resulting_quantity: Decimal | None = None
    resulting_average_cost: Decimal | None = None
    cash_amount: Decimal | None = None
    withholding_tax: Decimal | None = None
    cash_in_lieu: Decimal | None = None
    fractional_handling: str | None = None
    cost_basis_unresolved: bool = False
    legs: list[Leg] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def record_corporate_action(
    session: Session,
    *,
    request_id: str,
    instrument_reference: str,
    action_type: ActionType,
    ex_date: datetime,
    source: str,
    ratio: Decimal | None = None,
    cash_amount: Decimal | None = None,
    currency: str | None = None,
    withholding_tax: Decimal | None = None,
    new_instrument_reference: str | None = None,
    cost_allocation_percent: Decimal | None = None,
    announcement_date: datetime | None = None,
    record_date: datetime | None = None,
    pay_date: datetime | None = None,
    effective_at: datetime | None = None,
    source_reference: str | None = None,
    status: ActionStatus = ActionStatus.ANNOUNCED,
) -> CorporateAction:
    """Store an announced action. Idempotent on `request_id`."""
    existing = session.scalar(
        select(CorporateAction).where(CorporateAction.request_id == request_id)
    )
    if existing is not None:
        return existing

    # Validate the payload before any lookup so a malformed action reports the bad field rather
    # than an incidental instrument_not_found.
    _validate_action_inputs(action_type, ratio, cash_amount)

    instrument = resolve_instrument(session, instrument_reference)
    new_instrument = (
        resolve_instrument(session, new_instrument_reference)
        if new_instrument_reference
        else None
    )

    # An action whose basis treatment we cannot determine is flagged rather than approximated.
    unresolved = action_type in _UNRESOLVED_BASIS_ACTIONS and cost_allocation_percent is None

    action = CorporateAction(
        id=str(uuid4()),
        request_id=request_id,
        instrument_id=instrument.instrument_id,
        action_type=action_type.value,
        status=status.value,
        announcement_date=_aware(announcement_date) if announcement_date else None,
        ex_date=_aware(ex_date),
        record_date=_aware(record_date) if record_date else None,
        pay_date=_aware(pay_date) if pay_date else None,
        effective_at=_aware(effective_at) if effective_at else _aware(ex_date),
        ratio=ratio,
        cash_amount=cash_amount,
        currency=currency,
        withholding_tax=withholding_tax,
        new_instrument_id=new_instrument.instrument_id if new_instrument else None,
        cost_allocation_percent=cost_allocation_percent,
        cost_basis_unresolved=unresolved,
        source=source,
        source_reference=source_reference,
        fetched_at=utc_now(),
        created_at=utc_now(),
    )
    session.add(action)
    session.commit()

    if action_type in _PRICE_RESTATING_ACTIONS:
        # Auto-adjusted history is restated by the provider once this takes effect, and a cache
        # cannot tell that happened. Say so plainly instead of expiring entries on a guess.
        logger.warning(
            "%s recorded for %s (ex-date %s); cached price history is now suspect. "
            "Call clear_market_cache for this ticker.",
            action_type.value,
            instrument.ticker,
            _aware(ex_date).date().isoformat(),
        )
    return action


def _validate_action_inputs(
    action_type: ActionType, ratio: Decimal | None, cash_amount: Decimal | None
) -> None:
    ratio_required = {ActionType.SPLIT, ActionType.REVERSE_SPLIT, ActionType.STOCK_DIVIDEND}
    if action_type in ratio_required and (ratio is None or ratio <= ZERO):
        raise DomainError(
            422,
            "missing_field",
            f"ratio is required and must be positive for {action_type.value}",
            {"action_type": action_type.value},
        )

    cash_required = {
        ActionType.CASH_DIVIDEND,
        ActionType.INTEREST,
        ActionType.RETURN_OF_CAPITAL,
    }
    if action_type in cash_required and (cash_amount is None or cash_amount <= ZERO):
        raise DomainError(
            422,
            "missing_field",
            f"cash_amount per share is required for {action_type.value}",
            {"action_type": action_type.value},
        )


def get_action(session: Session, action_id: str) -> CorporateAction:
    action = session.get(CorporateAction, action_id)
    if action is None:
        raise not_found("corporate_action", action_id)
    return action


def _held_position(
    session: Session, portfolio_id: str, instrument: Instrument
) -> Position | None:
    position = session.get(Position, (portfolio_id, instrument.ticker))
    if position is None or position.quantity <= ZERO:
        return None
    return position


def preview_application(
    session: Session, portfolio_id: str, action_id: str
) -> ActionPreview:
    """Compute the effect of an action without writing anything."""
    get_portfolio(session, portfolio_id)
    action = get_action(session, action_id)
    instrument = session.scalar(
        select(Instrument).where(Instrument.instrument_id == action.instrument_id)
    )
    if instrument is None:
        raise not_found("instrument", action.instrument_id)

    preview = ActionPreview(
        portfolio_id=portfolio_id,
        action_id=action.id,
        action_type=action.action_type,
        applicable=False,
        cost_basis_unresolved=action.cost_basis_unresolved,
    )

    if action.status == ActionStatus.CANCELLED.value:
        preview.warnings.append("This action is cancelled and cannot be applied")
        return preview

    already = session.scalar(
        select(CorporateActionApplication).where(
            CorporateActionApplication.corporate_action_id == action_id,
            CorporateActionApplication.portfolio_id == portfolio_id,
            CorporateActionApplication.status == "applied",
        )
    )
    if already is not None:
        preview.warnings.append("This action was already applied to this portfolio")
        return preview

    position = _held_position(session, portfolio_id, instrument)
    if position is None:
        preview.warnings.append(
            f"The portfolio holds no {instrument.ticker} position on the effective date"
        )
        return preview

    preview.applicable = True
    preview.original_quantity = position.quantity
    preview.original_average_cost = position.average_cost

    action_type = ActionType(action.action_type)
    if action_type in {ActionType.SPLIT, ActionType.REVERSE_SPLIT}:
        _preview_split(preview, action, position, instrument)
    elif action_type in {ActionType.CASH_DIVIDEND, ActionType.INTEREST}:
        _preview_cash_income(preview, action, position, instrument)
    elif action_type is ActionType.RETURN_OF_CAPITAL:
        _preview_return_of_capital(preview, action, position, instrument)
    else:
        preview.applicable = False
        preview.warnings.append(
            f"{action.action_type} carries cost-basis treatment this service cannot determine; "
            "the action is recorded but must be applied manually"
        )
    return preview


def _preview_split(
    preview: ActionPreview,
    action: CorporateAction,
    position: Position,
    instrument: Instrument,
) -> None:
    """Quantity scales by the ratio; total cost basis is unchanged, so unit cost scales inversely.

    A split creates no income and no cash: it is a redenomination of the same economic stake.
    """
    ratio = action.ratio or Decimal("1")
    raw_quantity = position.quantity * ratio
    handling = FractionalHandling.KEEP_FRACTIONAL
    resulting_quantity = raw_quantity
    cash_in_lieu: Decimal | None = None

    fractional = raw_quantity - int(raw_quantity)
    if fractional != ZERO:
        # Whole-share settlement is the common market convention; the remainder is reported
        # rather than silently dropped.
        handling = FractionalHandling.ROUND_DOWN
        resulting_quantity = Decimal(int(raw_quantity))
        preview.warnings.append(
            f"The ratio produces {format(fractional, 'f')} of a fractional share; it is dropped "
            "and reported as cash_in_lieu is unknown"
        )

    total_cost = position.quantity * position.average_cost
    preview.resulting_quantity = resulting_quantity
    preview.resulting_average_cost = (
        total_cost / resulting_quantity if resulting_quantity > ZERO else ZERO
    )
    preview.fractional_handling = handling.value
    preview.cash_in_lieu = cash_in_lieu
    preview.legs = [
        Leg(
            leg_type=LegType.SECURITY,
            currency=instrument.currency,
            account_role="position",
            quantity_delta=resulting_quantity - position.quantity,
            instrument_id=instrument.instrument_id,
            metadata=json.dumps(
                {"ratio": format(action.ratio or Decimal("1"), "f"), "basis": "unchanged"},
                sort_keys=True,
            ),
        )
    ]


def _preview_cash_income(
    preview: ActionPreview,
    action: CorporateAction,
    position: Position,
    instrument: Instrument,
) -> None:
    """Gross income on the held quantity, less withholding, settles as net cash."""
    per_share = action.cash_amount or ZERO
    gross = per_share * position.quantity
    withholding = action.withholding_tax or ZERO
    net = gross - withholding

    preview.cash_amount = gross
    preview.withholding_tax = withholding
    preview.resulting_quantity = position.quantity
    preview.resulting_average_cost = position.average_cost

    legs = [
        Leg(
            leg_type=LegType.INCOME,
            currency=instrument.currency,
            account_role="income",
            amount_delta=-gross,
            instrument_id=instrument.instrument_id,
        )
    ]
    if withholding:
        legs.append(
            Leg(
                leg_type=LegType.TAX,
                currency=instrument.currency,
                account_role="withholding",
                amount_delta=withholding,
            )
        )
    legs.append(
        Leg(
            leg_type=LegType.CASH,
            currency=instrument.currency,
            account_role="settlement",
            amount_delta=net,
        )
    )
    preview.legs = legs


def _preview_return_of_capital(
    preview: ActionPreview,
    action: CorporateAction,
    position: Position,
    instrument: Instrument,
) -> None:
    """Cash increases and cost basis decreases; it is a repayment, not income.

    Basis cannot go negative. If the distribution exceeds remaining basis, the excess is taxable
    under rules that vary by jurisdiction, so basis stops at zero and the excess is reported
    unresolved rather than booked as a gain.
    """
    per_share = action.cash_amount or ZERO
    total_cash = per_share * position.quantity
    total_basis = position.quantity * position.average_cost
    excess = total_cash - total_basis

    if excess > ZERO:
        preview.cost_basis_unresolved = True
        preview.warnings.append(
            f"The distribution exceeds remaining cost basis by {format(excess, 'f')}; the excess "
            "is usually a taxable gain, but the treatment varies by jurisdiction and is left "
            "unresolved"
        )
        new_basis = ZERO
    else:
        new_basis = (total_basis - total_cash) / position.quantity

    preview.cash_amount = total_cash
    preview.resulting_quantity = position.quantity
    preview.resulting_average_cost = new_basis
    preview.legs = [
        Leg(
            leg_type=LegType.SECURITY,
            currency=instrument.currency,
            account_role="basis_reduction",
            amount_delta=-total_cash,
            instrument_id=instrument.instrument_id,
        ),
        Leg(
            leg_type=LegType.CASH,
            currency=instrument.currency,
            account_role="settlement",
            amount_delta=total_cash,
        ),
    ]


def apply_corporate_action(
    session: Session, portfolio_id: str, action_id: str, request_id: str
) -> CorporateActionApplication:
    """Apply an action atomically, recording the journal event and the application record."""
    portfolio = get_portfolio(session, portfolio_id)
    preview = preview_application(session, portfolio_id, action_id)
    action = get_action(session, action_id)

    if not preview.applicable:
        raise DomainError(
            422,
            "action_not_applicable",
            "This corporate action cannot be applied to this portfolio",
            {
                "action_id": action_id,
                "portfolio_id": portfolio_id,
                "warnings": preview.warnings,
            },
        )

    require_balanced(preview.legs, portfolio.base_currency)
    event_type = _ACTION_EVENT_TYPES[ActionType(action.action_type)]

    try:
        event = _persist(
            session,
            portfolio,
            TransactionRequest(
                request_id=request_id,
                event_type=event_type,
                source="corporate_action",
                source_reference=action.source_reference or action.id,
                memo=f"{action.action_type} applied from action {action.id}",
                occurred_at=action.effective_at,
            ),
            preview.legs,
            _fingerprint({"corporate_action": action.id, "portfolio": portfolio_id}),
            event_type=event_type,
        )
        _apply_projections(session, portfolio_id, preview.legs, allow_negative_cash=False)
        _override_position_basis(session, portfolio_id, action, preview)

        application = CorporateActionApplication(
            id=str(uuid4()),
            corporate_action_id=action.id,
            portfolio_id=portfolio_id,
            journal_event_id=event.id,
            original_quantity=preview.original_quantity,
            original_average_cost=preview.original_average_cost,
            resulting_quantity=preview.resulting_quantity,
            resulting_average_cost=preview.resulting_average_cost,
            cash_in_lieu=preview.cash_in_lieu,
            fractional_handling=preview.fractional_handling,
            status="applied",
            warnings=json.dumps(preview.warnings) if preview.warnings else None,
            created_at=utc_now(),
        )
        session.add(application)
        action.status = ActionStatus.APPLIED.value
        session.commit()
    except Exception:
        session.rollback()
        raise
    return application


def _override_position_basis(
    session: Session, portfolio_id: str, action: CorporateAction, preview: ActionPreview
) -> None:
    """Set the average cost a corporate action implies.

    The generic projection derives cost from consideration paid, which is the wrong model for an
    action that redenominates or returns basis without a purchase, so the computed figure is
    written directly.
    """
    if preview.resulting_average_cost is None:
        return
    instrument = session.scalar(
        select(Instrument).where(Instrument.instrument_id == action.instrument_id)
    )
    if instrument is None:
        return
    position = session.get(Position, (portfolio_id, instrument.ticker))
    if position is None:
        return
    position.average_cost = preview.resulting_average_cost
    position.updated_at = utc_now()


def list_actions(
    session: Session,
    *,
    instrument_reference: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[CorporateAction], int]:
    from sqlalchemy import func

    query = select(CorporateAction)
    counter = select(func.count()).select_from(CorporateAction)
    if instrument_reference:
        instrument = resolve_instrument(session, instrument_reference)
        query = query.where(CorporateAction.instrument_id == instrument.instrument_id)
        counter = counter.where(CorporateAction.instrument_id == instrument.instrument_id)
    if status:
        query = query.where(CorporateAction.status == status)
        counter = counter.where(CorporateAction.status == status)

    total = session.scalar(counter) or 0
    actions = session.scalars(
        query.order_by(CorporateAction.ex_date.desc(), CorporateAction.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return list(actions), total
