"""Daily valuation snapshots.

A snapshot records what a portfolio was worth on a date, priced only with data that existed at
that time. It is a cache of a computation, never a second ledger: everything here can be rebuilt
from the journal plus point-in-time market data, which is why dropping these tables loses no
facts and the downgrade is safe.

Two columns carry the honesty requirements. `status` plus `unpriced_market_value` record that a
holding could not be priced, instead of letting a missing quote read as a zero-valued position.
`calculation_version` lets a methodology change create a new revision alongside the old one
rather than restating history in place.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_valuation_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("valuation_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valuation_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        # Decimal values persist as text so exact arithmetic survives a round trip.
        sa.Column("securities_value", sa.Text(), nullable=False),
        sa.Column("unpriced_market_value", sa.Text(), nullable=False),
        sa.Column("cash_value", sa.Text(), nullable=False),
        sa.Column("total_value", sa.Text(), nullable=False),
        sa.Column("cost_basis", sa.Text(), nullable=False),
        sa.Column("external_flow_amount", sa.Text(), nullable=False),
        sa.Column("income_amount", sa.Text(), nullable=False),
        sa.Column("fee_amount", sa.Text(), nullable=False),
        sa.Column("tax_amount", sa.Text(), nullable=False),
        sa.Column("pricing_coverage_percent", sa.Text(), nullable=False),
        sa.Column("positions_total", sa.Integer(), nullable=False),
        sa.Column("positions_priced", sa.Integer(), nullable=False),
        sa.Column("has_unlinked_legacy_events", sa.Boolean(), nullable=False),
        sa.Column("calculation_version", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("warnings", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "portfolio_id",
            "valuation_date",
            "calculation_version",
            name="uq_valuation_snapshot_revision",
        ),
    )
    op.create_index(
        "ix_valuation_snapshots_portfolio_date",
        "portfolio_valuation_snapshots",
        ["portfolio_id", "valuation_date"],
    )

    op.create_table(
        "position_valuation_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("portfolio_snapshot_id", sa.String(36), nullable=False),
        sa.Column("instrument_id", sa.String(36), nullable=False),
        sa.Column("ticker_at_time", sa.String(30), nullable=False),
        sa.Column("valuation_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("average_cost", sa.Text(), nullable=False),
        sa.Column("cost_basis", sa.Text(), nullable=False),
        sa.Column("local_currency", sa.String(3), nullable=False),
        # Nullable: an unavailable price stays unknown rather than being recorded as zero.
        sa.Column("price", sa.Text()),
        sa.Column("market_value", sa.Text()),
        sa.Column("price_as_of", sa.DateTime(timezone=True)),
        sa.Column("price_provider", sa.String(60)),
        sa.Column("price_stale", sa.Boolean(), nullable=False),
        sa.Column("warnings", sa.Text()),
        sa.ForeignKeyConstraint(
            ["portfolio_snapshot_id"],
            ["portfolio_valuation_snapshots.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_position_snapshots_parent",
        "position_valuation_snapshots",
        ["portfolio_snapshot_id"],
    )
    op.create_index(
        "ix_position_snapshots_instrument",
        "position_valuation_snapshots",
        ["instrument_id", "valuation_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_position_snapshots_instrument", "position_valuation_snapshots")
    op.drop_index("ix_position_snapshots_parent", "position_valuation_snapshots")
    op.drop_table("position_valuation_snapshots")
    op.drop_index("ix_valuation_snapshots_portfolio_date", "portfolio_valuation_snapshots")
    op.drop_table("portfolio_valuation_snapshots")
