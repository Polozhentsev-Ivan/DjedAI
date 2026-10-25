"""Owned source persistence and real PostgreSQL reconnection without copying rows.

The service boundary is exercised with SQLite and real DuckDB in every environment.
Live PostgreSQL cases additionally use DJEDAI_TEST_PG_DSN and the seeded demo tables;
the optional loader DSN is needed only for the external live-data mutation test.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import duckdb
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from djedai.analytics.tools import run_sql
from djedai.backend.dataset_service import DatasetService
from djedai.backend.db.models import DatasetModel
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.local_user import bootstrap_local_user
from djedai.data.postgres import ALIAS, parse_dsn
from djedai.data.typed_views import _lit

pytestmark = pytest.mark.integration
DSN = os.environ.get("DJEDAI_TEST_PG_DSN")
OTHER_DSN = os.environ.get("DJEDAI_TEST_PG_OTHER_DSN")
WRITER_DSN = os.environ.get("DJEDAI_DEMO_PG_LOADER_DSN")
live_postgres = pytest.mark.skipif(not DSN, reason="DJEDAI_TEST_PG_DSN is not configured")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def storage(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "alice")
    bootstrap_local_user(factory, "bob", username="bob")
    key = Fernet.generate_key().decode()
    yield factory, tmp_path / "datasets", key
    engine.dispose()


def make_service(storage, *, key=True):
    factory, directory, encryption_key = storage
    return DatasetService(factory, directory, source_encryption_key=encryption_key if key else None)


def connect(service, owner, dsn, tables, **kwargs):
    async def run():
        accepted = await service.connect_db(owner, dsn, tables, **kwargs)
        await service.close()
        return accepted.dataset_id

    return asyncio.run(run())


async def events(service, owner, dataset_id):
    return [event async for event in service.events(owner, dataset_id)]


def test_missing_key_bad_dsn_and_unimplemented_source_create_no_metadata(storage) -> None:
    factory, directory, _ = storage
    service = make_service(storage)
    dsn = "postgresql://reader:password@127.0.0.1:5432/test"
    for target, source, tables, kwargs, code in (
        (make_service(storage, key=False), dsn, None, {}, 503),
        (service, "password=private", None, {}, 422),
        (service, "postgresql://[bad", None, {}, 422),
        (service, dsn, [], {}, 422),
        (service, dsn, [""], {}, 422),
        (service, dsn, None, {"name": " "}, 422),
        (service, "clickhouse://reader@127.0.0.1/test", None, {"kind": "clickhouse"}, 422),
        (service, dsn, None, {"kind": "mysql"}, 501),
    ):
        with pytest.raises(HTTPException) as error:
            asyncio.run(target.connect_db("alice", source, tables, **kwargs))
        assert error.value.status_code == code
        assert "private" not in str(error.value)
    assert service.list_for_owner("alice") == []
    assert not directory.exists()


def test_encrypted_queued_source_is_owner_bound_and_driver_failure_is_safe(
    storage, monkeypatch, caplog
) -> None:
    factory, _, _ = storage
    service = make_service(storage)
    dsn = "postgresql://reader:private-password@private-host:5432/business"
    dataset_id = service._persist_db_source("alice", "postgresql", dsn, ["public.sales"], None)
    with factory() as db:
        model = db.get(DatasetModel, dataset_id)
        assert model.connection is None
        assert model.raw_files is None
        assert model.source_secret and "private-password" not in model.source_secret
        assert "private-host" not in json.dumps(
            {column.name: getattr(model, column.name) for column in model.__table__.columns},
            default=str,
        )
        ciphertext = model.source_secret
    assert dsn not in service.get_detail("alice", dataset_id).model_dump_json()
    for method in (service.get_detail, service.get_catalog, service.get_context):
        with pytest.raises(HTTPException) as denied:
            method("bob", dataset_id)
        assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        asyncio.run(events(service, "bob", dataset_id))
    assert denied.value.status_code == 404
    assert service.list_for_owner("bob") == []
    assert service._secrets.decrypt("alice", dataset_id, ciphertext).dsn == dsn

    def unsafe_driver(*args, **kwargs):
        raise RuntimeError(f"driver failure: {dsn}")

    monkeypatch.setattr("djedai.backend.dataset_service.connect_postgres_dataset", unsafe_driver)

    async def run():
        await service.recover_interrupted()
        await service.close()

    asyncio.run(run())
    detail = service.get_detail("alice", dataset_id)
    assert detail.status == "error"
    assert "private" not in detail.model_dump_json()
    assert "private" not in caplog.text
    terminal = asyncio.run(events(service, "alice", dataset_id))
    assert [event.event for event in terminal] == ["error"]
    assert "private" not in terminal[0].model_dump_json()


def test_encrypted_source_migration_preserves_csv_and_downgrades(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    command.upgrade(cfg, "c4d9f51a6240")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id,username,is_active,created_at,updated_at) "
            "VALUES ('alice','alice',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO datasets "
            "(dataset_id,owner_id,name,source_kind,status,duckdb_path,created_at,updated_at) "
            "VALUES ('existing','alice','CSV','csv_upload','ready','existing/dataset.duckdb',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
    command.upgrade(cfg, "head")
    with sessionmaker(bind=engine)() as db:
        existing = db.get(DatasetModel, "existing")
        assert existing.source_secret is None
        assert existing.duckdb_path == "existing/dataset.duckdb"
    command.downgrade(cfg, "c4d9f51a6240")
    assert "source_secret" not in {
        column["name"] for column in inspect(engine).get_columns("datasets")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name FROM datasets").scalar() == "CSV"
    engine.dispose()


@live_postgres
def test_real_sources_survive_restart_isolate_owners_and_read_live_without_copy(storage) -> None:
    factory, directory, key = storage
    first_service = make_service(storage)
    alice = connect(first_service, "alice", DSN, ["stores"], name="Stores")
    bob = connect(first_service, "bob", DSN, ["products"], name="Products")
    assert alice != bob
    assert first_service.get_detail("alice", alice).status == "ready"
    assert first_service.get_detail("bob", bob).status == "ready"
    restarted = DatasetService(factory, directory, source_encryption_key=key)
    assert [item.dataset_id for item in restarted.list_for_owner("alice")] == [alice]
    assert [item.dataset_id for item in restarted.list_for_owner("bob")] == [bob]
    for owner, foreign in (("alice", bob), ("bob", alice)):
        for method in (restarted.get_detail, restarted.get_catalog, restarted.get_context):
            with pytest.raises(HTTPException) as denied:
                method(owner, foreign)
            assert denied.value.status_code == 404

    for owner, dataset_id, table in (("alice", alice, "stores"), ("bob", bob, "products")):
        context = restarted.get_context(owner, dataset_id)
        assert [item.table_id for item in context.catalog.tables] == [table]
        with context.open_connection() as connection:
            count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            assert count == context.catalog.tables[0].row_count
            assert connection.execute(
                "SELECT current_setting('enable_external_access')"
            ).fetchone() == (False,)
            with pytest.raises(duckdb.Error):
                connection.execute(f'DELETE FROM {ALIAS}.public."{table}"')
            with pytest.raises(duckdb.Error):
                connection.execute("CREATE TABLE copied (n INTEGER)")
            # The persistent analytical file has views and metadata, not source row copies.
            assert connection.execute(
                "SELECT COUNT(*) FROM duckdb_tables() "
                "WHERE database_name=current_database() AND table_name=?",
                [table],
            ).fetchone() == (0,)
        raw_database = context.duckdb_path.read_bytes()
        assert parse_dsn(DSN).password.encode() not in raw_database
        assert DSN not in repr(context)
        with factory() as db:
            model = db.get(DatasetModel, dataset_id)
            assert model.connection is None
            assert parse_dsn(DSN).password not in model.source_secret
            assert DSN not in model.catalog.__repr__()

    # A restart without the encryption key must not fall back to any other source.
    no_key = make_service(storage, key=False)
    with pytest.raises(HTTPException) as unavailable:
        no_key.get_context("alice", alice)
    assert unavailable.value.status_code == 503
    assert no_key.get_catalog("alice", alice).dataset_id == alice


@live_postgres
def test_real_interrupted_postgres_source_recovers_for_its_original_owner(storage) -> None:
    service = make_service(storage)
    alice = service._persist_db_source("alice", "postgresql", DSN, ["stores"], "Recover Alice")
    bob = service._persist_db_source("bob", "postgresql", DSN, ["products"], "Recover Bob")
    restarted = make_service(storage)

    async def run():
        await restarted.recover_interrupted()
        await restarted.recover_interrupted()
        await restarted.close()

    asyncio.run(run())
    assert restarted.get_detail("alice", alice).status == "ready"
    assert restarted.get_detail("bob", bob).status == "ready"
    assert [event.event for event in asyncio.run(events(restarted, "alice", alice))] == ["done"]


@live_postgres
def test_nested_postgres_contexts_keep_their_attachments_and_security_boundaries(
    storage, tmp_path
) -> None:
    service = make_service(storage)
    dataset_id = connect(service, "alice", DSN, ["stores"])
    context = service.get_context("alice", dataset_id)
    expected = context.catalog.tables[0].row_count
    before = context.duckdb_path.read_bytes()
    private_file = tmp_path / "private.csv"
    private_file.write_text("secret\nprivate\n", encoding="utf-8")

    with context.open_connection() as first:
        assert first.execute("SELECT COUNT(*) FROM stores").fetchone() == (expected,)
        with context.open_connection() as second:
            assert second.execute("SELECT COUNT(*) FROM main.stores").fetchone() == (expected,)
            # Opening and configuring another context must not detach the first source.
            assert first.execute("SELECT COUNT(*) FROM stores").fetchone() == (expected,)
            for connection in (first, second):
                assert connection.execute(
                    "SELECT current_setting('enable_external_access')"
                ).fetchone() == (False,)
                with pytest.raises(duckdb.Error):
                    connection.execute("CREATE TABLE injected(n INTEGER)")
                with pytest.raises(duckdb.Error):
                    connection.execute("SELECT * FROM read_csv(?)", [str(private_file)])
                allowed = {table.table_id for table in context.catalog.tables}
                result = run_sql(
                    "SELECT COUNT(*) AS n FROM main.stores",
                    connection=connection,
                    allowed_tables=allowed,
                )
                assert result.status == "ok"
                assert result.data.rows == [[expected]]
                for forbidden in (
                    "SELECT COUNT(*) FROM pg_src.public.stores",
                    "SELECT COUNT(*) FROM _djedai_db_sources",
                    "SELECT * FROM postgres_query('pg_src', 'SELECT 1')",
                    "CREATE TABLE memory.main.injected(n INTEGER)",
                ):
                    assert (
                        run_sql(forbidden, connection=connection, allowed_tables=allowed).status
                        == "error"
                    )
        # Closing another context must not invalidate this one or drop its secret.
        assert first.execute("SELECT COUNT(*) FROM stores").fetchone() == (expected,)
    assert context.duckdb_path.read_bytes() == before
    assert parse_dsn(DSN).password.encode() not in before


@live_postgres
def test_postgres_contexts_for_one_dataset_can_open_and_query_from_parallel_threads(
    storage,
) -> None:
    service = make_service(storage)
    dataset_id = connect(service, "alice", DSN, ["stores"])
    context = service.get_context("alice", dataset_id)
    both_open = Barrier(2)

    def query() -> int:
        try:
            with context.open_connection() as connection:
                both_open.wait(timeout=15)
                return connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
        except BaseException:
            both_open.abort()
            raise

    with ThreadPoolExecutor(max_workers=2) as workers:
        counts = list(workers.map(lambda _: query(), range(2)))
    assert counts == [context.catalog.tables[0].row_count] * 2


@live_postgres
@pytest.mark.skipif(not OTHER_DSN, reason="DJEDAI_TEST_PG_OTHER_DSN is not configured")
def test_same_named_tables_are_queried_from_each_owners_exact_source(storage) -> None:
    service = make_service(storage)
    first = connect(service, "alice", DSN, ["stores"])
    second = connect(service, "bob", OTHER_DSN, ["stores"])
    restarted = make_service(storage)
    first_context = restarted.get_context("alice", first)
    other_context = restarted.get_context("bob", second)
    with first_context.open_connection() as alice:
        first_count = alice.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
        with other_context.open_connection() as bob:
            other_count = bob.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
            with first_context.open_connection() as another_alice:
                assert another_alice.execute("SELECT COUNT(*) FROM stores").fetchone() == (
                    first_count,
                )
                assert bob.execute("SELECT COUNT(*) FROM stores").fetchone() == (other_count,)
                assert alice.execute("SELECT COUNT(*) FROM stores").fetchone() == (first_count,)
            assert alice.execute("SELECT COUNT(*) FROM stores").fetchone() == (first_count,)
            assert bob.execute("SELECT COUNT(*) FROM stores").fetchone() == (other_count,)
        assert alice.execute("SELECT COUNT(*) FROM stores").fetchone() == (first_count,)
    assert first_count > other_count >= 1


@live_postgres
@pytest.mark.skipif(not WRITER_DSN, reason="DJEDAI_DEMO_PG_LOADER_DSN is not configured")
def test_reopened_owned_postgres_view_reads_external_changes(storage) -> None:
    table = f"owned_live_{uuid4().hex}"
    info = parse_dsn(WRITER_DSN)
    writer = duckdb.connect()
    writer.execute("LOAD postgres")
    writer.execute(f"CREATE TEMPORARY SECRET w (TYPE postgres, PASSWORD {_lit(info.password)})")
    writer.execute(f"ATTACH {_lit(info.libpq(read_only=False))} AS w (TYPE postgres, SECRET w)")

    def execute(sql):
        writer.execute(f"CALL postgres_execute('w', {_lit(sql)})")

    try:
        execute(f"CREATE TABLE {table} (id INTEGER, amount INTEGER)")
        execute(f"INSERT INTO {table} VALUES (1, 10)")
        service = make_service(storage)
        dataset_id = connect(service, "alice", DSN, [table])
        assert service.get_detail("alice", dataset_id).status == "ready"
        execute(f"INSERT INTO {table} VALUES (2, 25)")
        restarted = make_service(storage)
        with restarted.get_context("alice", dataset_id).open_connection() as connection:
            assert connection.execute(f"SELECT SUM(amount) FROM {table}").fetchone() == (35,)
        # Profiling is an import-time snapshot; query data remains live.
        assert restarted.get_catalog("alice", dataset_id).tables[0].row_count == 1
    finally:
        execute(f"DROP TABLE IF EXISTS {table}")
        writer.close()
