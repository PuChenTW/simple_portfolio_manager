"""Keep snapshots from different report timezones separate.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("portfolio_valuation_snapshots") as batch:
        batch.add_column(sa.Column("report_timezone", sa.String(100),
                                   nullable=False, server_default="UTC"))
        batch.drop_constraint("uq_valuation_snapshot_revision", type_="unique")
        batch.create_unique_constraint("uq_valuation_snapshot_revision", [
            "portfolio_id", "valuation_date", "calculation_version", "report_timezone",
        ])


def downgrade() -> None:
    connection = op.get_bind()
    non_utc = connection.scalar(sa.text(
        "SELECT count(*) FROM portfolio_valuation_snapshots WHERE report_timezone != 'UTC'"
    ))
    if non_utc:
        raise RuntimeError("Remove non-UTC snapshots explicitly before downgrading")
    with op.batch_alter_table("portfolio_valuation_snapshots") as batch:
        batch.drop_constraint("uq_valuation_snapshot_revision", type_="unique")
        batch.create_unique_constraint("uq_valuation_snapshot_revision", [
            "portfolio_id", "valuation_date", "calculation_version",
        ])
        batch.drop_column("report_timezone")
