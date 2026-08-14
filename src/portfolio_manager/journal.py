"""Double-entry journal: atomic multi-leg posting, balance validation, and reversal.

The journal is the audit record of what happened. Positions and cash balances are projections
derived from it, updated in the same database transaction as the legs so the two can never
disagree -- the failure mode this module exists to prevent is "position updated but cash was not".

Events are immutable once posted. A correction is a reversal event carrying opposing legs and a
link back to the original, so the original entry and the fact that it was undone both survive.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .errors import DomainError

ZERO = Decimal("0")


class EventType(StrEnum):
    """The economic events the journal can post."""

    BUY = "buy"
    SELL = "sell"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    DIVIDEND = "dividend"
    INTEREST = "interest"
    FEE = "fee"
    TAX = "tax"
    # Corporate actions post through the same ledger.
    SPLIT = "split"
    STOCK_DIVIDEND = "stock_dividend"
    RETURN_OF_CAPITAL = "return_of_capital"
    SYMBOL_CHANGE = "symbol_change"
    MERGER = "merger"
    SPINOFF = "spinoff"
    REVERSAL = "reversal"


class LegType(StrEnum):
    SECURITY = "security"
    CASH = "cash"
    FEE = "fee"
    TAX = "tax"
    INCOME = "income"
    RECEIVABLE = "receivable"
    OTHER = "other"


class EventStatus(StrEnum):
    POSTED = "posted"
    REVERSED = "reversed"


class PortfolioKind(StrEnum):
    """What a book is for.

    Lives here for the same reason `ActionType` does: the API schemas name these values, and the
    schemas cannot import the service layer that would otherwise own them.

    A cash account is not a different structure -- it is a portfolio that never holds a position.
    The distinction exists so that posting a security into a bank balance can be rejected, and so
    a reader can tell a brokerage account from a savings account.

    A liability account is the same book with the sign reversed: a loan, where the balance is
    what is owed rather than what is held. Nothing about the journal changes -- cash legs already
    carry a sign and every total is a plain sum -- so the kind exists to say that a negative
    balance here is the normal state rather than the overdraft the ledger refuses everywhere
    else, and to keep a debt out of the return figures, since repaying a loan is not an
    investment decision.
    """

    INVESTMENT = "investment"
    CASH = "cash"
    LIABILITY = "liability"


class ActionType(StrEnum):
    """Corporate action vocabulary.

    Lives here rather than in `corporate_actions` so the API schemas can name these values
    without importing the service layer, which imports the schemas in turn.
    """

    CASH_DIVIDEND = "cash_dividend"
    INTEREST = "interest"
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    STOCK_DIVIDEND = "stock_dividend"
    RETURN_OF_CAPITAL = "return_of_capital"
    SYMBOL_CHANGE = "symbol_change"
    MERGER = "merger"
    SPINOFF = "spinoff"


class ActionStatus(StrEnum):
    ANNOUNCED = "announced"
    CONFIRMED = "confirmed"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class FlowClassification(StrEnum):
    """Whether an event moves money across the portfolio boundary.

    This drives performance measurement: external flows are investor contributions and
    withdrawals, which TWR must neutralize. Dividends and interest are internal -- they are
    returns the portfolio earned, and counting them as contributions would understate performance.
    """

    EXTERNAL = "external"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


_EXTERNAL_EVENTS = frozenset(
    {
        EventType.DEPOSIT,
        EventType.WITHDRAWAL,
        EventType.TRANSFER_IN,
        EventType.TRANSFER_OUT,
    }
)

# Trades restructure assets, and income/fees/taxes arise from holdings; none is investor capital.
_INTERNAL_EVENTS = frozenset(
    {
        EventType.BUY,
        EventType.SELL,
        EventType.DIVIDEND,
        EventType.INTEREST,
        EventType.FEE,
        EventType.TAX,
        EventType.SPLIT,
        EventType.STOCK_DIVIDEND,
        EventType.RETURN_OF_CAPITAL,
        EventType.SYMBOL_CHANGE,
        EventType.MERGER,
        EventType.SPINOFF,
    }
)

# REVERSAL is deliberately in neither set. It belongs to whichever category the event it undoes
# belongs to, which `effective_type` resolves; a literal classification would be a guess.


def classify_flow(event_type: EventType) -> FlowClassification:
    """Classify a resolved event type. Callers holding a raw string want `derived_flow`."""
    if event_type in _EXTERNAL_EVENTS:
        return FlowClassification.EXTERNAL
    if event_type in _INTERNAL_EVENTS:
        return FlowClassification.INTERNAL
    return FlowClassification.UNKNOWN


def effective_type(event_type: str, reversed_type: str | None = None) -> EventType | None:
    """The event type that governs flow classification.

    A reversal has no economic meaning of its own -- it undoes another event and must land in the
    same flow category, with the opposite sign. Classifying it on its own `reversal` type would
    push a reversed deposit into UNKNOWN and flag a fully recorded correction as a data-quality
    problem the reader can never resolve, which is how a warning that matters gets ignored.

    The sign is not this function's business: a reversal's legs are already inverted, so a
    reversed deposit lands in the same category with the opposite sign on its own.
    """
    value = event_type
    if value == EventType.REVERSAL.value and reversed_type:
        value = reversed_type
    try:
        return EventType(value)
    except ValueError:
        return None


def derived_flow(event_type: str, reversed_type: str | None = None) -> FlowClassification:
    """Classify a stored event type, which is a plain string on the row.

    Pass `reversed_type` -- the type of the event this one reverses -- whenever the caller can
    resolve it, or every reversal reports UNKNOWN.

    An unrecognized value stays UNKNOWN rather than defaulting to either side: a flow guessed
    wrong is silently absorbed into the capital base or the return, and neither is recoverable.
    """
    resolved = effective_type(event_type, reversed_type)
    return FlowClassification.UNKNOWN if resolved is None else classify_flow(resolved)


@dataclass
class Leg:
    """One side of an event, in the currency named on the leg."""

    leg_type: LegType
    currency: str
    account_role: str
    amount_delta: Decimal | None = None
    quantity_delta: Decimal | None = None
    instrument_id: str | None = None
    unit_price: Decimal | None = None
    fx_rate: Decimal | None = None
    metadata: str | None = None

    def functional_amount(self) -> Decimal:
        """Value of this leg in the event's functional currency.

        A leg in a foreign currency must carry an explicit `fx_rate`; there is no fallback lookup,
        because silently applying today's rate to a past event would corrupt the audit record.
        """
        if self.amount_delta is None:
            return ZERO
        if self.fx_rate is None:
            return self.amount_delta
        return self.amount_delta * self.fx_rate


@dataclass
class BalanceReport:
    """Why an event balanced or did not, retained so the API can explain a rejection."""

    balanced: bool
    residual: Decimal
    functional_currency: str
    leg_count: int
    warnings: list[str] = field(default_factory=list)


def validate_balance(
    legs: list[Leg], functional_currency: str, *, tolerance: Decimal = ZERO
) -> BalanceReport:
    """Check that cash-valued legs net to zero in the functional currency.

    Security legs carry `quantity_delta` for the position projection and `amount_delta` for the
    consideration paid or received, so they participate in the balance like any other leg. A buy
    balances because the security's positive consideration offsets the negative cash, fee, and tax.

    Quantity-only legs (a split, which changes share count without moving money) contribute
    nothing to the monetary balance, which is why they are permitted to have no `amount_delta`.
    """
    if not legs:
        raise DomainError(
            422, "empty_journal_event", "A journal event must have at least one leg", {}
        )

    residual = sum((leg.functional_amount() for leg in legs), start=ZERO)
    warnings: list[str] = []
    for leg in legs:
        if leg.currency != functional_currency and leg.fx_rate is None and leg.amount_delta:
            warnings.append(
                f"{leg.leg_type.value} leg in {leg.currency} has no fx_rate; it was treated as "
                f"{functional_currency} at parity"
            )
    return BalanceReport(
        balanced=abs(residual) <= tolerance,
        residual=residual,
        functional_currency=functional_currency,
        leg_count=len(legs),
        warnings=warnings,
    )


def require_balanced(legs: list[Leg], functional_currency: str) -> BalanceReport:
    """Validate, or raise the error that aborts the surrounding database transaction."""
    report = validate_balance(legs, functional_currency)
    if not report.balanced:
        raise DomainError(
            422,
            "journal_out_of_balance",
            "Journal legs do not net to zero in the event's functional currency",
            {
                "residual": format(report.residual, "f"),
                "functional_currency": functional_currency,
                "leg_count": report.leg_count,
                "legs": [
                    {
                        "leg_type": leg.leg_type.value,
                        "currency": leg.currency,
                        "amount_delta": (
                            format(leg.amount_delta, "f") if leg.amount_delta is not None else None
                        ),
                        "functional_amount": format(leg.functional_amount(), "f"),
                    }
                    for leg in legs
                ],
            },
        )
    return report


def invert(legs: list[Leg]) -> list[Leg]:
    """Build the opposing legs of a reversal.

    Every signed amount flips while instrument, currency, and role are preserved, so a reversal
    restores the prior position and cash exactly and remains recognizable as the mirror of the
    original event.
    """
    return [
        Leg(
            leg_type=leg.leg_type,
            currency=leg.currency,
            account_role=leg.account_role,
            amount_delta=None if leg.amount_delta is None else -leg.amount_delta,
            quantity_delta=None if leg.quantity_delta is None else -leg.quantity_delta,
            instrument_id=leg.instrument_id,
            unit_price=leg.unit_price,
            fx_rate=leg.fx_rate,
            metadata=leg.metadata,
        )
        for leg in legs
    ]
