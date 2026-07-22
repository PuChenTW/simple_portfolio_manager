"""Initial portfolio schema.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "instruments",
        sa.Column("ticker", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("asset_type", sa.String(16), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("exchange", sa.String(100)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "cash_balances",
        sa.Column(
            "portfolio_id",
            sa.String(36),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "cash_transactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "portfolio_id",
            sa.String(36),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("portfolio_id", "request_id", name="uq_cash_request"),
    )
    op.create_index(
        "ix_cash_portfolio_occurred",
        "cash_transactions",
        ["portfolio_id", "occurred_at"],
    )
    op.create_table(
        "positions",
        sa.Column(
            "portfolio_id",
            sa.String(36),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "ticker", sa.String(32), sa.ForeignKey("instruments.ticker"), primary_key=True
        ),
        sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("average_cost", sa.Text(), nullable=False),
        sa.Column("realized_pnl", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "position_tags",
        sa.Column("portfolio_id", sa.String(36), primary_key=True),
        sa.Column("ticker", sa.String(32), primary_key=True),
        sa.Column("tag", sa.String(50), primary_key=True),
        sa.ForeignKeyConstraint(
            ["portfolio_id", "ticker"],
            ["positions.portfolio_id", "positions.ticker"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_position_tags_tag", "position_tags", ["portfolio_id", "tag"]
    )
    op.create_table(
        "quote_cache",
        sa.Column(
            "ticker",
            sa.String(32),
            sa.ForeignKey("instruments.ticker", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("price", sa.Text(), nullable=False),
        sa.Column("open", sa.Text()),
        sa.Column("high", sa.Text()),
        sa.Column("low", sa.Text()),
        sa.Column("previous_close", sa.Text()),
        sa.Column("volume", sa.Text()),
        sa.Column("change", sa.Text()),
        sa.Column("change_percent", sa.Text()),
        sa.Column("market_cap", sa.Text()),
        sa.Column("year_high", sa.Text()),
        sa.Column("year_low", sa.Text()),
        sa.Column("sma20", sa.Text()),
        sa.Column("sma50", sa.Text()),
        sa.Column("rsi14", sa.Text()),
        sa.Column("macd", sa.Text()),
        sa.Column("macd_signal", sa.Text()),
        sa.Column("macd_histogram", sa.Text()),
        sa.Column("provider_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indicators_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "portfolio_id",
            sa.String(36),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("ticker", sa.String(32), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("unit_price", sa.Text(), nullable=False),
        sa.Column("fee", sa.Text(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("portfolio_id", "request_id", name="uq_trade_request"),
    )
    op.create_index(
        "ix_trades_portfolio_executed", "trades", ["portfolio_id", "executed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_trades_portfolio_executed", table_name="trades")
    op.drop_table("trades")
    op.drop_table("quote_cache")
    op.drop_index("ix_position_tags_tag", table_name="position_tags")
    op.drop_table("position_tags")
    op.drop_table("positions")
    op.drop_index("ix_cash_portfolio_occurred", table_name="cash_transactions")
    op.drop_table("cash_transactions")
    op.drop_table("cash_balances")
    op.drop_table("instruments")
    op.drop_table("portfolios")
