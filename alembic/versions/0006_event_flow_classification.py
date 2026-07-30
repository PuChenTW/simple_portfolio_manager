"""Manual rulings on an event's flow classification.

The journal derives whether cash crossed the portfolio boundary from the event type, which is
right for anything posted through the journal and unreliable for migrated rows: the pre-journal
model had only `deposit` and `withdraw`, so a trade settlement recorded by an operator is
indistinguishable from investor capital. Treating a settlement as a contribution makes TWR
neutralize trading proceeds and understate the return.

This table records a person's ruling as a separate, higher-ranked opinion. The posted event is
never modified, and retracting the override restores the derived reading -- the same provenance
discipline `instrument_classifications` uses. Dropping the table loses only the rulings, so the
downgrade is safe.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_flow_classifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("classification", sa.String(10), nullable=False),
        sa.Column("provenance", sa.String(20), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        # Required: a reclassification nobody can justify later is indistinguishable from an error.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_retracted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["journal_events.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", "provenance", name="uq_event_flow_provenance"),
    )
    op.create_index("ix_event_flow_event", "event_flow_classifications", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_event_flow_event", "event_flow_classifications")
    op.drop_table("event_flow_classifications")
