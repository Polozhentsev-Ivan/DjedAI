"""B1-9 / #61: owned ClickHouse datasets through the shared facade, catalog and tools.

Every case runs against the in-process fake server (tests/fake_clickhouse.py). With
DJEDAI_TEST_CH_URL (reader URL of the seeded demo database) the same cases repeat on a
real ClickHouse; scripts/test_clickhouse_isolation.py prepares one in a container.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from djedai.analytics.tools import run_sql
from djedai.backend.dataset_service import DatasetService
from djedai.backend.db.models import DatasetModel
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.local_user import bootstrap_local_user
from djedai.data.clickhouse import ClickHouseLimits, parse_dsn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from fake_clickhouse import FakeClickHouse  # noqa: E402

pytestmark = pytest.mark.integration
LIVE_URL = os.environ.get("DJEDAI_TEST_CH_URL")
SPEC = json.loads((ROOT / "eval" / "demo_db_expectations.json").read_text(encoding="utf-8"))


@pytest.fixture(
    params=[
        "fake",
        pytest.param(
            "live",
            marks=pytest.mark.skipif(not LIVE_URL, reason="DJEDAI_TEST_CH_URL is not configured"),
        ),
    ]
)
def source_url(request):
    if request.param == "live":
        yield LIVE_URL
        return
    with FakeClickHouse().load_demo() as fake:
        yield fake.url


@pytest.fixture
def storage(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "alice")
    bootstrap_local_user(factory, "bob", username="bob")
    yield factory, tmp_path / "datasets", Fernet.generate_key().decode()
    engine.dispose()


def make_service(storage, **kwargs):
    factory, directory, key = storage
    return DatasetService(factory, directory, source_encryption_key=key, **kwargs)


def connect(service, owner, dsn, tables=None, **kwargs):
    async def run():
        accepted = await service.connect_db(owner, dsn, tables, kind="clickhouse", **kwargs)
        await service.close()
        return accepted.dataset_id

    return asyncio.run(run())


def same(actual, expected: str) -> bool:
    if isinstance(actual, date):
        return actual.isoformat() == expected
    try:
        return Decimal(str(actual)) == Decimal(expected)
    except InvalidOperation:
        return str(actual) == expected


def test_catalog_and_control_aggregates_match_the_demo_dataset(storage, source_url) -> None:
    factory, _, _ = storage
    service = make_service(storage)
    dataset_id = connect(service, "alice", source_url, name="Demo ClickHouse")
    detail = service.get_detail("alice", dataset_id)
    assert detail.status == "ready", detail
    assert detail.source.kind == "clickhouse"
    catalog = service.get_catalog("alice", dataset_id)
    assert catalog.source.kind == "clickhouse"
    tables = {table.table_id: table for table in catalog.tables}
    assert set(tables) == set(SPEC["tables"])
    for name, expected in SPEC["tables"].items():
        assert tables[name].row_count == expected["rows"]
        assert [column.name for column in tables[name].columns] == expected["columns"]
        assert tables[name].source.relation == f"{parse_dsn(source_url).database}.{name}"
    relations = {
        (r.from_.table, r.from_.column, r.to.table, r.to.column) for r in catalog.relations
    }
    assert ("sales", "store_id", "stores", "store_id") in relations
    assert ("sales", "product_id", "products", "product_id") in relations

    context = service.get_context("alice", dataset_id)
    allowed = {table.table_id for table in catalog.tables}
    with context.open_connection() as connection:
        for name, check in SPEC["checks"].items():
            result = run_sql(check["sql"], connection=connection, allowed_tables=allowed)
            assert result.status == "ok", (name, result.error)
            row = result.data.rows[0]
            assert all(same(a, e) for a, e in zip(row, check["expected"], strict=True)), name
        # SQL агента видит только представления каталога, не «сырые» копии
        hidden = run_sql(
            "SELECT count(*) FROM ch_djedai_demo_sales",
            connection=connection,
            allowed_tables=allowed,
        )
        assert hidden.status == "error"

    password = parse_dsn(source_url).password
    assert password.encode() not in context.duckdb_path.read_bytes()
    assert password not in catalog.model_dump_json()
    with factory() as db:
        model = db.get(DatasetModel, dataset_id)
        assert model.source_kind == "clickhouse"
        assert model.connection is None
        assert password not in model.source_secret
    assert sorted(path.name for path in context.duckdb_path.parent.iterdir()) == ["dataset.duckdb"]


def test_snapshot_is_queried_without_the_source_and_stays_owned(storage) -> None:
    factory, directory, key = storage
    with FakeClickHouse().load_demo() as fake:
        alice = connect(make_service(storage), "alice", fake.url, ["stores"])
        bob = connect(make_service(storage), "bob", fake.url, ["products"])
        calls = len(fake.calls)
    # Сервер остановлен: анализ идёт по копии, секрет источника для запросов не нужен.
    restarted = DatasetService(factory, directory, source_encryption_key=None)
    for owner, foreign in (("alice", bob), ("bob", alice)):
        for method in (restarted.get_detail, restarted.get_catalog, restarted.get_context):
            with pytest.raises(HTTPException) as denied:
                method(owner, foreign)
            assert denied.value.status_code == 404
    for owner, dataset_id, table in (("alice", alice, "stores"), ("bob", bob, "products")):
        context = restarted.get_context(owner, dataset_id)
        assert [item.table_id for item in context.catalog.tables] == [table]
        with context.open_connection() as connection:
            count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            assert count == SPEC["tables"][table]["rows"]
            assert connection.execute(
                "SELECT current_setting('enable_external_access')"
            ).fetchone() == (False,)
        # A copied snapshot needs no live check: fresh for its revision (B1-11).
        detail = restarted.get_detail(owner, dataset_id)
        assert (detail.revision.number, detail.freshness.state) == (1, "fresh")
        with context.open_reader() as reader:
            _, read = reader.run(lambda: None)
        assert (read.read_mode, read.consistency, read.snapshot_id) == (
            "snapshot",
            "immutable",
            "r1",
        )
    alice_revision = restarted.get_detail("alice", alice).revision
    bob_revision = restarted.get_detail("bob", bob).revision
    # Same server, different tables: a different source and schema.
    assert alice_revision.source_fingerprint != bob_revision.source_fingerprint
    assert alice_revision.schema_fingerprint != bob_revision.schema_fingerprint
    assert len(fake.calls) == calls


def test_source_over_the_volume_limit_fails_safely(storage) -> None:
    service = make_service(storage, clickhouse_limits=ClickHouseLimits(max_rows=100))
    with FakeClickHouse().load_demo() as fake:
        dataset_id = connect(service, "alice", fake.url)
        assert not any("FORMAT Parquet" in call["sql"] for call in fake.calls)
    detail = service.get_detail("alice", dataset_id)
    assert detail.status == "error"
    with service.session_factory() as db:
        message = db.get(DatasetModel, dataset_id).error_message
    assert "слишком велик" in message and "4801" in message
    assert fake.password not in message


def test_bad_credentials_never_reach_status_or_logs(storage, caplog) -> None:
    service = make_service(storage)
    with FakeClickHouse().load_demo() as fake:
        dsn = fake.url.replace("reader-secret%21", "private-password")
        dataset_id = connect(service, "alice", dsn)
    with service.session_factory() as db:
        model = db.get(DatasetModel, dataset_id)
        assert model.status == "error"
        assert "private-password" not in (model.error_message or "") + caplog.text


def test_interrupted_clickhouse_build_recovers_for_its_original_owner(storage) -> None:
    with FakeClickHouse().load_demo() as fake:
        first = make_service(storage)
        alice = first._persist_db_source("alice", "clickhouse", fake.url, ["stores"], None)
        restarted = make_service(storage)

        async def run():
            await restarted.recover_interrupted()
            await restarted.close()

        asyncio.run(run())
    assert restarted.get_detail("alice", alice).status == "ready"
    assert restarted.get_detail("alice", alice).name == "ClickHouse"
    with pytest.raises(HTTPException) as denied:
        restarted.get_detail("bob", alice)
    assert denied.value.status_code == 404
