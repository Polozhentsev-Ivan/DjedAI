"""Свежесть живого источника и снимки чтений хода (B1-11, #169).

Живой PostgreSQL здесь заменён подключённой in-memory базой DuckDB с той же схемой
``schema.table``; на реальном сервере то же проверяет
``tests/integration/test_catalog_revision.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from djedai.backend.source_reads import (
    CatalogStaleError,
    SourceReader,
    check_live_freshness,
    snapshot_freshness,
)
from djedai.contracts.catalog import DatasetCatalog
from djedai.data.fingerprint import record_source_schema, schema_fingerprint

EXAMPLES = Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples"
FULL = json.loads((EXAMPLES / "dataset_catalog.json").read_text(encoding="utf-8"))
MINIMAL = json.loads((EXAMPLES / "dataset_catalog_minimal.json").read_text(encoding="utf-8"))
SCHEMA = {"sales.orders": (("id", "BIGINT"), ("amount", "DOUBLE"))}
PROFILED = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


def catalog(profiled_at: datetime = PROFILED) -> DatasetCatalog:
    revision = {
        **FULL["revision"],
        "profiled_at": profiled_at.isoformat(),
        "schema_fingerprint": schema_fingerprint(SCHEMA),
    }
    return DatasetCatalog.model_validate({**FULL, "revision": revision})


@pytest.fixture
def con():
    connection = duckdb.connect()
    record_source_schema(connection, SCHEMA)
    connection.execute("ATTACH ':memory:' AS src")
    connection.execute("CREATE SCHEMA src.sales")
    connection.execute("CREATE TABLE src.sales.orders (id BIGINT, amount DOUBLE)")
    yield connection
    connection.close()


def check(con, now=PROFILED + timedelta(hours=1), current=None):
    return check_live_freshness(con, current or catalog(), "src", DAY, now=now)


def test_matching_schema_is_fresh(con):
    freshness = check(con)
    assert (freshness.state, freshness.changes) == ("fresh", [])
    assert freshness.profiled_at == PROFILED
    assert freshness.checked_at == PROFILED + timedelta(hours=1)


def test_old_profile_is_aged_with_its_age(con):
    freshness = check(con, now=PROFILED + timedelta(hours=30))
    assert freshness.state == "aged"
    assert "30 ч назад" in freshness.reason


def test_added_column_is_compatible_drift(con):
    con.execute("ALTER TABLE src.sales.orders ADD COLUMN note VARCHAR")
    freshness = check(con)
    assert freshness.state == "drifted"
    assert [(c.kind, c.column) for c in freshness.changes] == [("added_column", "note")]
    assert "добавлена колонка sales.orders.note" in freshness.reason


@pytest.mark.parametrize(
    ("ddl", "kind"),
    [
        ("ALTER TABLE src.sales.orders DROP COLUMN amount", "dropped_column"),
        ("ALTER TABLE src.sales.orders RENAME COLUMN amount TO total", "dropped_column"),
        ("ALTER TABLE src.sales.orders ALTER amount TYPE VARCHAR", "type_changed"),
        ("DROP TABLE src.sales.orders", "dropped_table"),
    ],
    ids=["drop column", "rename column", "change type", "drop table"],
)
def test_breaking_changes_are_incompatible(con, ddl, kind):
    con.execute(ddl)
    freshness = check(con)
    assert freshness.state == "incompatible"
    assert kind in {c.kind for c in freshness.changes}
    assert "Каталог не используется" in freshness.reason
    error = CatalogStaleError(freshness)
    assert (error.status_code, error.code, error.detail) == (409, "catalog_stale", freshness.reason)


def test_catalog_without_revision_is_unknown(con):
    freshness = check(con, current=DatasetCatalog.model_validate(MINIMAL))
    assert freshness.state == "unknown" and "Перепрофилируйте" in freshness.reason


def test_file_without_source_schema_is_unknown():
    with duckdb.connect() as connection:
        assert check(connection).state == "unknown"


def test_snapshots_are_fresh_for_their_revision():
    assert snapshot_freshness(catalog()).state == "fresh"
    assert snapshot_freshness(DatasetCatalog.model_validate(MINIMAL)).state == "unknown"


def test_snapshot_reads_are_immutable(con):
    reader = SourceReader(con, catalog(), snapshot_freshness(catalog()), live=False)
    reader.share_snapshot("run_1")  # снимок файла не меняется: общий id не нужен
    value, read = reader.run(lambda: 42)
    assert value == 42
    assert (read.read_mode, read.consistency, read.snapshot_id) == ("snapshot", "immutable", "r2")
    assert read.catalog_revision == 2 and read.started_at <= read.finished_at
    with pytest.raises(duckdb.TransactionException):  # no transaction was opened
        con.execute("COMMIT")


def test_live_reads_without_a_turn_are_independent(con):
    reader = SourceReader(con, catalog(), check(con), live=True)
    _, read = reader.run(lambda: None)
    assert (read.read_mode, read.consistency, read.snapshot_id) == ("live", "independent", None)


def test_turn_shares_one_snapshot_until_a_failed_read(con):
    reader = SourceReader(con, catalog(), check(con), live=True)
    reader.share_snapshot("run_1")
    _, first = reader.run(lambda: con.execute("SELECT count(*) FROM src.sales.orders").fetchall())
    _, second = reader.run(lambda: con.execute("SELECT 1").fetchall())
    assert first.consistency == "turn_snapshot"
    assert first.snapshot_id == second.snapshot_id == "run_1:1"

    def failing() -> None:
        con.execute("SELECT CAST('x' AS INTEGER)").fetchall()

    with pytest.raises(duckdb.ConversionException):
        reader.run(failing)
    # The runtime error aborted the transaction: the next read opens a new snapshot.
    value, third = reader.run(lambda: con.execute("SELECT 7").fetchone()[0])
    assert value == 7 and third.snapshot_id == "run_1:2"
