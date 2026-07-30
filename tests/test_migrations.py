"""Alembic migrations must run on an empty database and on one holding pre-migration data.

The `harness` fixture builds tables with `Base.metadata.create_all`, which never exercises the
migration path. These tests drive Alembic directly so a schema change that only works on a fresh
database cannot pass unnoticed.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from portfolio_manager.db import Base, create_sqlite_engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'migration.db'}"


def table_names(database_url: str) -> set[str]:
    engine = create_sqlite_engine(database_url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def column_names(database_url: str, table: str) -> set[str]:
    engine = create_sqlite_engine(database_url)
    try:
        return {column["name"] for column in sa.inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_upgrade_head_on_empty_database(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")

    tables = table_names(database_url)
    assert {"issuers", "instrument_aliases", "instrument_classifications"} <= tables
    assert "instrument_id" in column_names(database_url, "instruments")


def test_upgrade_backfills_instrument_ids_for_existing_rows(database_url: str) -> None:
    """A database already holding instruments must come out with every row assigned an ID."""
    config = alembic_config(database_url)
    command.upgrade(config, "0001")

    engine = create_sqlite_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for ticker in ("AAPL", "2330.TW"):
            connection.execute(
                sa.text(
                    "INSERT INTO instruments "
                    "(ticker, name, asset_type, market, exchange, currency, created_at, updated_at)"
                    " VALUES (:t, :t, 'stock', 'US', 'NMS', 'USD', :now, :now)"
                ),
                {"t": ticker, "now": now},
            )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_sqlite_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT ticker, instrument_id FROM instruments ORDER BY ticker")
            ).all()
            aliases = connection.execute(
                sa.text("SELECT provider_symbol FROM instrument_aliases ORDER BY provider_symbol")
            ).scalars().all()
    finally:
        engine.dispose()

    assert [row[0] for row in rows] == ["2330.TW", "AAPL"]
    instrument_ids = [row[1] for row in rows]
    assert all(instrument_ids), "every pre-existing instrument must be backfilled"
    assert len(set(instrument_ids)) == 2, "backfilled IDs must be distinct"
    assert aliases == ["2330.TW", "AAPL"], "legacy rows must get provider alias rows"


def test_migrated_schema_matches_the_orm_models(database_url: str) -> None:
    """Guards the split brain where models.py and alembic/versions drift apart.

    The test harness builds tables from the ORM while production runs migrations, so a table or
    column added to only one of them would otherwise pass every other test.
    """
    command.upgrade(alembic_config(database_url), "head")

    engine = create_sqlite_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        migrated = set(inspector.get_table_names()) - {"alembic_version"}
        declared = set(Base.metadata.tables)
        assert migrated == declared

        for table in sorted(migrated):
            migrated_columns = {column["name"] for column in inspector.get_columns(table)}
            declared_columns = set(Base.metadata.tables[table].columns.keys())
            assert migrated_columns == declared_columns, f"column drift in {table}"
    finally:
        engine.dispose()


def test_downgrade_unwinds_every_revision(database_url: str) -> None:
    """Each revision must be reversible, so a bad deploy can be rolled back rather than restored."""
    config = alembic_config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, "0002")
    assert "journal_events" not in table_names(database_url)
    assert "journal_legs" not in table_names(database_url)

    command.downgrade(config, "0001")
    tables = table_names(database_url)
    assert "issuers" not in tables
    assert "instrument_aliases" not in tables
    assert "instrument_classifications" not in tables
    assert "instrument_id" not in column_names(database_url, "instruments")
