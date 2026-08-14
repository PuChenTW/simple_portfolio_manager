"""Cash accounts and the transfer link between two portfolios.

A portfolio was implicitly a securities account. `portfolios.kind` makes the distinction explicit
so a bank balance or an e-wallet can be recorded as what it is: a book that holds cash and never a
position. Nothing else about the structure changes -- a cash account replays, values, and measures
performance through exactly the same machinery, which is why this is two columns rather than a
second set of tables.

Existing rows backfill to `investment`, which is what they have always been. `institution` is left
null rather than guessed: the bank or broker behind an existing portfolio is not derivable from
anything recorded, and inventing one would be indistinguishable from a fact.

`journal_events.transfer_id` links the two events of one transfer -- one per portfolio, since an
event belongs to exactly one book. It is deliberately not a foreign key. The pair spans two
portfolios, so a constraint would either block deleting one side or leave the survivor pointing at
a row that no longer exists; a plain correlation id lets the survivor say honestly that its
counterparty is gone. `transfer_role` records which side an event is, because the reversal of a
transfer-out carries an inflow sign and the cash sign alone cannot tell the two apart.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot add a NOT NULL column without a default to fill existing rows with.
    with op.batch_alter_table("portfolios") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(16), nullable=False, server_default="investment")
        )
        batch.add_column(sa.Column("institution", sa.String(100), nullable=True))

    # Drop the server default now that every row carries a value. The ORM declares only a
    # Python-side default, and a schema that disagrees with the models is the split-brain the
    # migration tests exist to catch.
    with op.batch_alter_table("portfolios") as batch:
        batch.alter_column(
            "kind",
            existing_type=sa.String(16),
            existing_nullable=False,
            server_default=None,
        )

    with op.batch_alter_table("journal_events") as batch:
        batch.add_column(sa.Column("transfer_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("transfer_role", sa.String(10), nullable=True))

    op.create_index("ix_journal_events_transfer", "journal_events", ["transfer_id"])


def downgrade() -> None:
    op.drop_index("ix_journal_events_transfer", table_name="journal_events")

    with op.batch_alter_table("journal_events") as batch:
        batch.drop_column("transfer_role")
        batch.drop_column("transfer_id")

    with op.batch_alter_table("portfolios") as batch:
        batch.drop_column("institution")
        batch.drop_column("kind")
