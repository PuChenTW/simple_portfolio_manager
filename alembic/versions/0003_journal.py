"""Journal events and legs.

The journal becomes the audit record for asset and cash movement. Positions and cash balances
remain as projections maintained in the same transaction as the legs. Existing trades and cash
transactions are left in place and untouched here; migration 0004 backfills them.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "journal_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("functional_currency", sa.String(3), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_date", sa.DateTime(timezone=True)),
        sa.Column("settlement_date", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_reference", sa.String(200)),
        sa.Column("memo", sa.Text()),
        sa.Column("reverses_event_id", sa.String(36)),
        sa.Column("is_unlinked_legacy", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reverses_event_id"], ["journal_events.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("portfolio_id", "request_id", name="uq_journal_request"),
    )
    op.create_index(
        "ix_journal_events_portfolio_occurred",
        "journal_events",
        ["portfolio_id", "occurred_at"],
    )
    op.create_index("ix_journal_events_type", "journal_events", ["portfolio_id", "event_type"])

    op.create_table(
        "journal_legs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("leg_type", sa.String(20), nullable=False),
        sa.Column("instrument_id", sa.String(36)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("quantity_delta", sa.Text()),
        sa.Column("amount_delta", sa.Text()),
        sa.Column("unit_price", sa.Text()),
        sa.Column("fx_rate", sa.Text()),
        sa.Column("account_role", sa.String(30), nullable=False),
        sa.Column("leg_metadata", sa.Text()),
        sa.ForeignKeyConstraint(["event_id"], ["journal_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_journal_legs_event", "journal_legs", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_journal_legs_event", "journal_legs")
    op.drop_table("journal_legs")
    op.drop_index("ix_journal_events_type", "journal_events")
    op.drop_index("ix_journal_events_portfolio_occurred", "journal_events")
    op.drop_table("journal_events")
