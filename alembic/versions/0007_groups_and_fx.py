"""Portfolio groups and stored FX rates.

A group reports several portfolios together in one currency. Membership is effective-dated
because a group whose members can be edited in place would silently restate every past report:
removing a portfolio today must not remove it from last month's numbers.

`fx_rates` stores each observed rate rather than only the converted totals. Without the rate and
its date, a consolidated figure cannot be checked or reproduced, which is the whole point of
recording it.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("reporting_currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "portfolio_group_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("group_id", sa.String(36), nullable=False),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        # Null means "still a member"; a closed interval preserves the historical membership.
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["portfolio_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "group_id", "portfolio_id", "effective_from", name="uq_group_member_interval"
        ),
    )
    op.create_index("ix_group_members_group", "portfolio_group_members", ["group_id"])

    op.create_table(
        "fx_rates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("rate", sa.Text(), nullable=False),
        sa.Column("price_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("provider_symbol", sa.String(30)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "base_currency",
            "quote_currency",
            "price_as_of",
            "provider",
            name="uq_fx_rate_observation",
        ),
    )
    op.create_index(
        "ix_fx_rates_pair_date", "fx_rates", ["base_currency", "quote_currency", "price_as_of"]
    )


def downgrade() -> None:
    op.drop_index("ix_fx_rates_pair_date", "fx_rates")
    op.drop_table("fx_rates")
    op.drop_index("ix_group_members_group", "portfolio_group_members")
    op.drop_table("portfolio_group_members")
    op.drop_table("portfolio_groups")
