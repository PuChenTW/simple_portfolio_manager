from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
    __tablename__ = "portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
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


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "request_id", name="uq_trade_request"),
        Index("ix_trades_portfolio_executed", "portfolio_id", "executed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.ticker"), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    fee: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CashBalance(Base):
    __tablename__ = "cash_balances"

    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CashTransaction(Base):
    __tablename__ = "cash_transactions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "request_id", name="uq_cash_request"),
        Index("ix_cash_portfolio_occurred", "portfolio_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JournalEvent(Base):
    """One economic event. Immutable once posted: corrections are reversal + replacement."""

    __tablename__ = "journal_events"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "request_id", name="uq_journal_request"),
        Index("ix_journal_events_portfolio_occurred", "portfolio_id", "occurred_at"),
        Index("ix_journal_events_type", "portfolio_id", "event_type"),
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
    # Set when a legacy trade or cash row was migrated without a provable counterpart leg.
    is_unlinked_legacy: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
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
