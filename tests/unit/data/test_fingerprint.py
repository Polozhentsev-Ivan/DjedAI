"""Отпечатки схемы и источника, сравнение схем (B1-11, #169)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from djedai.data.fingerprint import (
    diff_schema,
    live_schema,
    read_source_schema,
    record_source_schema,
    schema_fingerprint,
    source_fingerprint,
)
from djedai.data.service import CsvSource, ingest_csv_dataset
from djedai.data.typed_views import TRANSFORM_VERSION

FIXTURES = Path(__file__).parents[2] / "fixtures" / "csv"
ORDERS = (("id", "BIGINT"), ("amount", "DOUBLE"))
CLIENTS = (("id", "BIGINT"), ("name", "VARCHAR"))


def test_schema_fingerprint_ignores_table_order_but_not_columns():
    first = schema_fingerprint({"public.orders": ORDERS, "public.clients": CLIENTS})
    assert first == schema_fingerprint({"public.clients": CLIENTS, "public.orders": ORDERS})
    assert len(first) == 64
    swapped = schema_fingerprint({"public.orders": ORDERS[::-1], "public.clients": CLIENTS})
    retyped = schema_fingerprint(
        {"public.orders": (("id", "INTEGER"), ORDERS[1]), "public.clients": CLIENTS}
    )
    assert len({first, swapped, retyped}) == 3


def test_source_fingerprint_hides_identity():
    value = source_fingerprint("postgres", "db.internal", 5432, "sales", ["public.orders"])
    assert "db.internal" not in value and "sales" not in value
    assert value != source_fingerprint("postgres", "db.internal", 5432, "other", ["public.orders"])


def test_diff_schema_classifies_every_change():
    saved = {"public.orders": ORDERS + (("note", "VARCHAR"),), "public.clients": CLIENTS}
    live = {"public.orders": (("id", "INTEGER"), ("amount", "DOUBLE"), ("extra", "DATE"))}
    changes = diff_schema(saved, live)
    assert [(c.kind, c.table, c.column) for c in changes] == [
        ("dropped_table", "public.clients", None),
        ("type_changed", "public.orders", "id"),
        ("dropped_column", "public.orders", "note"),
        ("added_column", "public.orders", "extra"),
    ]
    assert [c.compatible for c in changes] == [False, False, False, True]
    assert (changes[1].before, changes[1].after) == ("BIGINT", "INTEGER")
    assert diff_schema(saved, saved) == []


def test_new_source_tables_are_not_drift():
    assert diff_schema({"public.orders": ORDERS}, {"public.orders": ORDERS, "x.y": CLIENTS}) == []


def test_source_schema_round_trips_and_is_absent_in_old_files():
    with duckdb.connect() as connection:
        assert read_source_schema(connection) is None
        record_source_schema(connection, {"public.orders": ORDERS})
        record_source_schema(connection, {"public.orders": ORDERS[:1], "public.clients": CLIENTS})
        assert read_source_schema(connection) == {
            "public.clients": CLIENTS,
            "public.orders": ORDERS[:1],
        }


def test_live_schema_reads_only_the_requested_tables():
    with duckdb.connect() as connection:
        connection.execute("ATTACH ':memory:' AS src")
        connection.execute("CREATE SCHEMA src.sales")
        connection.execute("CREATE TABLE src.sales.orders (id BIGINT, amount DOUBLE)")
        connection.execute("CREATE TABLE src.sales.other (x INTEGER)")
        schema = live_schema(connection, ["sales.orders", "sales.missing"], "src")
        assert schema == {"sales.orders": ORDERS}
        assert live_schema(connection, [], "src") == {}


@pytest.fixture
def build(tmp_path):
    def run(path: Path, name: str, number: int = 1):
        target = tmp_path / name / "dataset.duckdb"
        catalog = ingest_csv_dataset(
            [CsvSource(path, path.name)],
            target,
            dataset_id="ds_1",
            name="Продажи",
            created_at=datetime(2026, 10, 1, tzinfo=UTC),
            revision=number,
        )
        with duckdb.connect(str(target), read_only=True) as connection:
            return catalog, read_source_schema(connection)

    return run


def test_csv_build_stamps_a_revision(build, tmp_path):
    before = datetime.now(UTC)
    catalog, schema = build(FIXTURES / "orders_utf8_comma.csv", "a", number=3)
    revision = catalog.revision
    assert (revision.number, revision.transform_version) == (3, TRANSFORM_VERSION)
    assert before <= revision.profiled_at <= datetime.now(UTC)
    assert revision.profiled_at > catalog.created_at
    assert revision.schema_fingerprint == schema_fingerprint(schema)
    assert list(schema) == [catalog.tables[0].raw_table]

    same, _ = build(FIXTURES / "orders_utf8_comma.csv", "b")
    assert same.revision.source_fingerprint == revision.source_fingerprint
    assert same.revision.schema_fingerprint == revision.schema_fingerprint

    edited = tmp_path / "orders_utf8_comma.csv"
    lines = (FIXTURES / "orders_utf8_comma.csv").read_bytes().splitlines(keepends=True)
    edited.write_bytes(b"".join([*lines, lines[-1]]))
    changed, _ = build(edited, "c")
    assert changed.revision.source_fingerprint != revision.source_fingerprint
