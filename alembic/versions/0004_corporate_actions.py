"""Corporate actions and their per-portfolio applications.

Actions are recorded as facts about an instrument, separately from applying them to a portfolio.
`cost_basis_unresolved` marks an action whose basis treatment depends on jurisdiction or issuer
disclosure this service does not have; the column exists so that gap is stored explicitly instead
of being papered over with an invented allocation.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("instrument_id", sa.String(36), nullable=False),
        sa.Column("action_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("announcement_date", sa.DateTime(timezone=True)),
        sa.Column("ex_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record_date", sa.DateTime(timezone=True)),
        sa.Column("pay_date", sa.DateTime(timezone=True)),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ratio", sa.Text()),
        sa.Column("cash_amount", sa.Text()),
        sa.Column("currency", sa.String(3)),
        sa.Column("withholding_tax", sa.Text()),
        sa.Column("new_instrument_id", sa.String(36)),
        sa.Column("cost_allocation_percent", sa.Text()),
        sa.Column("cost_basis_unresolved", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_reference", sa.String(200)),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["new_instrument_id"], ["instruments.instrument_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("request_id", name="uq_corporate_action_request"),
    )
    op.create_index(
        "ix_corporate_actions_instrument", "corporate_actions", ["instrument_id", "ex_date"]
    )

    op.create_table(
        "corporate_action_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("corporate_action_id", sa.String(36), nullable=False),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("journal_event_id", sa.String(36)),
        sa.Column("original_quantity", sa.Text()),
        sa.Column("original_average_cost", sa.Text()),
        sa.Column("resulting_quantity", sa.Text()),
        sa.Column("resulting_average_cost", sa.Text()),
        sa.Column("cash_in_lieu", sa.Text()),
        sa.Column("fractional_handling", sa.String(30)),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("warnings", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["corporate_action_id"], ["corporate_actions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["journal_event_id"], ["journal_events.id"], ondelete="RESTRICT"
        ),
        # An action may only be applied to a portfolio once; a rerun must not double-apply it.
        sa.UniqueConstraint(
            "corporate_action_id", "portfolio_id", name="uq_action_applied_once"
        ),
    )


def downgrade() -> None:
    op.drop_table("corporate_action_applications")
    op.drop_index("ix_corporate_actions_instrument", "corporate_actions")
    op.drop_table("corporate_actions")
