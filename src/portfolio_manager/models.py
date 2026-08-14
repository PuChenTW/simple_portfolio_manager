from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from .db import Base


class DecimalText(TypeDecorator[Decimal]):
    """Persist Decimal exactly; SQLite NUMERIC storage may use binary floats."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, _dialect: Any) -> str | None:
        return None if value is None else format(value, "f")

    def process_result_value(self, value: str | None, _dialect: Any) -> Decimal | None:
        return None if value is None else Decimal(value)


class Portfolio(Base):
    """One isolated, single-currency book.

    `kind` separates a securities account from a plain cash account (a bank balance, an e-wallet).
    Both are the same structure -- a journal, a cash projection, and snapshots -- because a cash
    account is simply one that never holds a position. `base_currency` is the cash currency too,
    which is why `cash_balances` needs no currency of its own.
    """

    __tablename__ = "portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="investment")
    institution: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Issuer(Base):
    """The economic entity behind one or more listings (TSM and 2330.TW share one)."""

    __tablename__ = "issuers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    country_of_domicile: Mapped[str | None] = mapped_column(String(2))
    lei: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Instrument(Base):
    """A tradable listing.

    `ticker` stays the primary key so every existing foreign key and tool response is unaffected.
    `instrument_id` is an additive stable surrogate that new identity-aware features reference;
    it is unique and backfilled for every pre-existing row.
    """

    __tablename__ = "instruments"

    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    issuer_id: Mapped[str | None] = mapped_column(ForeignKey("issuers.id", ondelete="SET NULL"))
    is_fund: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    active_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstrumentAlias(Base):
    """Provider-specific symbols that resolve to one instrument, including retired tickers."""

    __tablename__ = "instrument_aliases"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_symbol", "effective_from", name="uq_alias_provider_symbol"
        ),
        Index("ix_instrument_aliases_instrument", "instrument_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(100))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstrumentClassification(Base):
    """One classification field's value with its provenance.

    Rows are append-only per (instrument, field, provenance): a manual override never edits or
    deletes the provider's row, it outranks it. Retracting an override restores the provider view.
    """

    __tablename__ = "instrument_classifications"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "field", "provenance", name="uq_classification_field_provenance"
        ),
        Index("ix_instrument_classifications_instrument", "instrument_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="CASCADE"), nullable=False
    )
    field: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[str | None] = mapped_column(String(100))
    provenance: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[Decimal | None] = mapped_column(DecimalText())
    note: Mapped[str | None] = mapped_column(Text())
    is_retracted: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Position(Base):
    __tablename__ = "positions"

    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    ticker: Mapped[str] = mapped_column(
        ForeignKey("instruments.ticker"), primary_key=True
    )
    quantity: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionTag(Base):
    __tablename__ = "position_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["portfolio_id", "ticker"],
            ["positions.portfolio_id", "positions.ticker"],
            ondelete="CASCADE",
        ),
        Index("ix_position_tags_tag", "portfolio_id", "tag"),
    )

    portfolio_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    tag: Mapped[str] = mapped_column(String(50), primary_key=True)


class CashBalance(Base):
    __tablename__ = "cash_balances"

    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JournalEvent(Base):
    """One economic event. Immutable once posted: corrections are reversal + replacement."""

    __tablename__ = "journal_events"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "request_id", name="uq_journal_request"),
        Index("ix_journal_events_portfolio_occurred", "portfolio_id", "occurred_at"),
        Index("ix_journal_events_type", "portfolio_id", "event_type"),
        Index("ix_journal_events_transfer", "transfer_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    functional_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trade_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(200))
    memo: Mapped[str | None] = mapped_column(Text())
    reverses_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("journal_events.id", ondelete="RESTRICT")
    )
    # A transfer is two events, one per portfolio, sharing this id. Deliberately not a foreign
    # key: the pair spans two portfolios, so a constraint would either block deleting one side or
    # corrupt the survivor's link. A dangling id is honest -- the counterparty record is gone.
    transfer_id: Mapped[str | None] = mapped_column(String(36))
    # 'out' or 'in'. Stored rather than derived because a reversal of the out-side carries an
    # inflow sign yet is still the out-side of the pair; the cash sign alone cannot say which.
    transfer_role: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JournalLeg(Base):
    """One side of an event. Legs balance in the event's functional currency or nothing posts."""

    __tablename__ = "journal_legs"
    __table_args__ = (Index("ix_journal_legs_event", "event_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("journal_events.id", ondelete="CASCADE"), nullable=False
    )
    leg_type: Mapped[str] = mapped_column(String(20), nullable=False)
    instrument_id: Mapped[str | None] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quantity_delta: Mapped[Decimal | None] = mapped_column(DecimalText())
    amount_delta: Mapped[Decimal | None] = mapped_column(DecimalText())
    unit_price: Mapped[Decimal | None] = mapped_column(DecimalText())
    fx_rate: Mapped[Decimal | None] = mapped_column(DecimalText())
    account_role: Mapped[str] = mapped_column(String(30), nullable=False)
    leg_metadata: Mapped[str | None] = mapped_column(Text())


class CorporateAction(Base):
    """A corporate action as announced, independent of any portfolio it may affect."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_corporate_action_request"),
        Index("ix_corporate_actions_instrument", "instrument_id", "ex_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    instrument_id: Mapped[str] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="RESTRICT"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    announcement_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ex_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    record_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pay_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Ratio semantics depend on action_type: 2 for a 2-for-1 split, 0.5 for a reverse split.
    ratio: Mapped[Decimal | None] = mapped_column(DecimalText())
    cash_amount: Mapped[Decimal | None] = mapped_column(DecimalText())
    currency: Mapped[str | None] = mapped_column(String(3))
    withholding_tax: Mapped[Decimal | None] = mapped_column(DecimalText())
    new_instrument_id: Mapped[str | None] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="RESTRICT")
    )
    # Null when the correct basis split is genuinely unknown. Never filled with a guess: an
    # invented allocation silently corrupts every future gain calculation.
    cost_allocation_percent: Mapped[Decimal | None] = mapped_column(DecimalText())
    cost_basis_unresolved: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(200))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorporateActionApplication(Base):
    """A record that one action was applied to one portfolio, and what it did."""

    __tablename__ = "corporate_action_applications"
    __table_args__ = (
        UniqueConstraint(
            "corporate_action_id", "portfolio_id", name="uq_action_applied_once"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    corporate_action_id: Mapped[str] = mapped_column(
        ForeignKey("corporate_actions.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    journal_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("journal_events.id", ondelete="RESTRICT")
    )
    original_quantity: Mapped[Decimal | None] = mapped_column(DecimalText())
    original_average_cost: Mapped[Decimal | None] = mapped_column(DecimalText())
    resulting_quantity: Mapped[Decimal | None] = mapped_column(DecimalText())
    resulting_average_cost: Mapped[Decimal | None] = mapped_column(DecimalText())
    cash_in_lieu: Mapped[Decimal | None] = mapped_column(DecimalText())
    fractional_handling: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    warnings: Mapped[str | None] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PortfolioGroup(Base):
    """A set of portfolios reported together in one currency."""

    __tablename__ = "portfolio_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    reporting_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PortfolioGroupMember(Base):
    """One portfolio's membership of a group over a period.

    Membership is effective-dated and never deleted. Removing a portfolio closes its interval, so
    a report for last month still contains the portfolios that were in the group last month --
    editing membership in place would silently restate history.
    """

    __tablename__ = "portfolio_group_members"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "portfolio_id", "effective_from", name="uq_group_member_interval"
        ),
        Index("ix_group_members_group", "group_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("portfolio_groups.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FxRate(Base):
    """One observed exchange rate, kept so a conversion can be audited or reproduced.

    Storing only converted totals would make a report impossible to check: the rate that produced
    it would be gone. `price_as_of` is the rate's own date, which is what bounds a historical
    conversion; `fetched_at` records when it was retrieved.
    """

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint(
            "base_currency",
            "quote_currency",
            "price_as_of",
            "provider",
            name="uq_fx_rate_observation",
        ),
        Index("ix_fx_rates_pair_date", "base_currency", "quote_currency", "price_as_of"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    price_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    provider_symbol: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PortfolioValuationSnapshot(Base):
    """What a portfolio was worth on one date, priced with data available at that time.

    A snapshot is an auditable record of a computation, not a second source of truth: it can
    always be rebuilt from the journal plus point-in-time market data. `calculation_version`
    exists so a later methodology change produces a new revision that can be compared against the
    old one, rather than silently restating history.

    `status` is `partial` whenever any holding could not be priced. The unpriced value is carried
    in `unpriced_market_value` and excluded from `securities_value`, because substituting zero
    for an unknown price would understate the portfolio while looking like a real number.
    """

    __tablename__ = "portfolio_valuation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id",
            "valuation_date",
            "calculation_version",
            name="uq_valuation_snapshot_revision",
        ),
        Index("ix_valuation_snapshots_portfolio_date", "portfolio_id", "valuation_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    valuation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valuation_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    securities_value: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    unpriced_market_value: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    cash_value: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    total_value: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    external_flow_amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    income_amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    pricing_coverage_percent: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    positions_total: Mapped[int] = mapped_column(Integer(), nullable=False)
    positions_priced: Mapped[int] = mapped_column(Integer(), nullable=False)
    calculation_version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    warnings: Mapped[str | None] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionValuationSnapshot(Base):
    """One holding inside a snapshot, with the price and its provenance.

    `ticker_at_time` is stored alongside `instrument_id` because tickers change: reading the
    current ticker back onto a historical row would relabel the past.
    """

    __tablename__ = "position_valuation_snapshots"
    __table_args__ = (
        Index("ix_position_snapshots_parent", "portfolio_snapshot_id"),
        Index("ix_position_snapshots_instrument", "instrument_id", "valuation_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("portfolio_valuation_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ticker_at_time: Mapped[str] = mapped_column(String(30), nullable=False)
    valuation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    local_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Null when no price was available at the cutoff; market_value is then null too, never zero.
    price: Mapped[Decimal | None] = mapped_column(DecimalText())
    market_value: Mapped[Decimal | None] = mapped_column(DecimalText())
    price_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_provider: Mapped[str | None] = mapped_column(String(60))
    price_stale: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    warnings: Mapped[str | None] = mapped_column(Text())


class QuoteCache(Base):
    __tablename__ = "quote_cache"

    ticker: Mapped[str] = mapped_column(
        ForeignKey("instruments.ticker", ondelete="CASCADE"), primary_key=True
    )
    price: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    open: Mapped[Decimal | None] = mapped_column(DecimalText())
    high: Mapped[Decimal | None] = mapped_column(DecimalText())
    low: Mapped[Decimal | None] = mapped_column(DecimalText())
    previous_close: Mapped[Decimal | None] = mapped_column(DecimalText())
    volume: Mapped[Decimal | None] = mapped_column(DecimalText())
    change: Mapped[Decimal | None] = mapped_column(DecimalText())
    change_percent: Mapped[Decimal | None] = mapped_column(DecimalText())
    market_cap: Mapped[Decimal | None] = mapped_column(DecimalText())
    year_high: Mapped[Decimal | None] = mapped_column(DecimalText())
    year_low: Mapped[Decimal | None] = mapped_column(DecimalText())
    sma20: Mapped[Decimal | None] = mapped_column(DecimalText())
    sma50: Mapped[Decimal | None] = mapped_column(DecimalText())
    rsi14: Mapped[Decimal | None] = mapped_column(DecimalText())
    macd: Mapped[Decimal | None] = mapped_column(DecimalText())
    macd_signal: Mapped[Decimal | None] = mapped_column(DecimalText())
    macd_histogram: Mapped[Decimal | None] = mapped_column(DecimalText())
    provider_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    indicators_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
