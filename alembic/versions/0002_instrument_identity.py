"""Instrument identity: issuers, stable instrument IDs, aliases, and classifications.

Additive by design. `instruments.ticker` remains the primary key and every existing foreign key
(positions, trades, quote_cache) is untouched, so the pre-existing API and MCP surface keeps its
exact semantics. `instrument_id` is a new stable surrogate that identity-aware features join on;
existing rows are backfilled with generated UUIDs before the NOT NULL constraint is applied.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issuers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("legal_name", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("country_of_domicile", sa.String(2)),
        sa.Column("lei", sa.String(20)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Added nullable, backfilled, then tightened: SQLite cannot add a NOT NULL column without a
    # default, and a random per-row default is not expressible in DDL.
    with op.batch_alter_table("instruments") as batch:
        batch.add_column(sa.Column("instrument_id", sa.String(36)))
        batch.add_column(sa.Column("issuer_id", sa.String(36)))
        batch.add_column(sa.Column("is_fund", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("active_from", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("active_to", sa.DateTime(timezone=True)))

    connection = op.get_bind()
    tickers = connection.execute(sa.text("SELECT ticker FROM instruments")).scalars().all()
    for ticker in tickers:
        connection.execute(
            sa.text("UPDATE instruments SET instrument_id = :iid WHERE ticker = :ticker"),
            {"iid": str(uuid4()), "ticker": ticker},
        )

    with op.batch_alter_table("instruments") as batch:
        batch.alter_column("instrument_id", existing_type=sa.String(36), nullable=False)
        batch.create_unique_constraint("uq_instruments_instrument_id", ["instrument_id"])
        batch.create_foreign_key(
            "fk_instruments_issuer", "issuers", ["issuer_id"], ["id"], ondelete="SET NULL"
        )

    op.create_table(
        "instrument_aliases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instrument_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(100)),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "provider", "provider_symbol", "effective_from", name="uq_alias_provider_symbol"
        ),
    )
    op.create_index("ix_instrument_aliases_instrument", "instrument_aliases", ["instrument_id"])

    op.create_table(
        "instrument_classifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instrument_id", sa.String(36), nullable=False),
        sa.Column("field", sa.String(50), nullable=False),
        sa.Column("value", sa.String(100)),
        sa.Column("provenance", sa.String(20), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("confidence", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("is_retracted", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "instrument_id", "field", "provenance", name="uq_classification_field_provenance"
        ),
    )
    op.create_index(
        "ix_instrument_classifications_instrument", "instrument_classifications", ["instrument_id"]
    )

    # Every pre-existing instrument gets an alias row so provider-symbol lookup works uniformly
    # for legacy and newly-resolved rows.
    now = datetime.now(UTC)
    rows = connection.execute(
        sa.text("SELECT instrument_id, ticker, exchange FROM instruments")
    ).all()
    for instrument_id, ticker, exchange in rows:
        connection.execute(
            sa.text(
                "INSERT INTO instrument_aliases "
                "(id, instrument_id, provider, provider_symbol, exchange, effective_from, "
                "created_at) VALUES (:id, :iid, :provider, :symbol, :exchange, :now, :now)"
            ),
            {
                "id": str(uuid4()),
                "iid": instrument_id,
                "provider": "yahoo",
                "symbol": ticker,
                "exchange": exchange,
                "now": now,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_instrument_classifications_instrument", "instrument_classifications")
    op.drop_table("instrument_classifications")
    op.drop_index("ix_instrument_aliases_instrument", "instrument_aliases")
    op.drop_table("instrument_aliases")
    with op.batch_alter_table("instruments") as batch:
        batch.drop_constraint("fk_instruments_issuer", type_="foreignkey")
        batch.drop_constraint("uq_instruments_instrument_id", type_="unique")
        batch.drop_column("active_to")
        batch.drop_column("active_from")
        batch.drop_column("is_fund")
        batch.drop_column("issuer_id")
        batch.drop_column("instrument_id")
    op.drop_table("issuers")
