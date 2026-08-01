"""Drop the pre-journal ledgers and the migration machinery built for them.

The `trades` and `cash_transactions` tables were independent ledgers: recording a buy moved the
position without touching cash, so the two could disagree and nothing in the schema said which was
right. The journal replaced them with balanced events, and `record_transaction` posts a position
and its settlement together or not at all.

Three things go with them. `event_flow_classifications` held manual rulings on whether a migrated
cash row was investor capital or a trade settlement -- a question that only existed because the old
model could not distinguish them; every journal event now derives its flow from its own type.
`journal_events.is_unlinked_legacy` marked rows that arrived without a provable counterpart leg,
and `portfolio_valuation_snapshots.has_unlinked_legacy_events` carried that fact into snapshots.
Neither can be true of an event posted through the journal.

This is a breaking change, taken deliberately with the API version bump to 0.2.0.

The downgrade rebuilds the schema but not the data. Dropped rows are gone: this migration deletes
the only copy of the legacy ledgers, and a downgrade that silently produced empty tables where
history used to be would be worse than one that says so plainly here.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_event_flow_event", table_name="event_flow_classifications")
    op.drop_table("event_flow_classifications")

    op.drop_index("ix_trades_portfolio_executed", table_name="trades")
    op.drop_table("trades")

    op.drop_index("ix_cash_portfolio_occurred", table_name="cash_transactions")
    op.drop_table("cash_transactions")

    with op.batch_alter_table("journal_events") as batch:
        batch.drop_column("is_unlinked_legacy")
    with op.batch_alter_table("portfolio_valuation_snapshots") as batch:
        batch.drop_column("has_unlinked_legacy_events")


def downgrade() -> None:
    with op.batch_alter_table("portfolio_valuation_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "has_unlinked_legacy_events",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("journal_events") as batch:
        batch.add_column(
            sa.Column(
                "is_unlinked_legacy", sa.Boolean(), nullable=False, server_default=sa.false()
            )
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
        "ix_cash_portfolio_occurred", "cash_transactions", ["portfolio_id", "occurred_at"]
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
    op.create_index("ix_trades_portfolio_executed", "trades", ["portfolio_id", "executed_at"])

    op.create_table(
        "event_flow_classifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("classification", sa.String(10), nullable=False),
        sa.Column("provenance", sa.String(20), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_retracted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["journal_events.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", "provenance", name="uq_event_flow_provenance"),
    )
    op.create_index("ix_event_flow_event", "event_flow_classifications", ["event_id"])
