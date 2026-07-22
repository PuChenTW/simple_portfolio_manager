from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
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


class Instrument(Base):
    __tablename__ = "instruments"

    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
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
