"""Catalog revisions, source freshness and re-profiling (B1-11, #169).

CSV cases need no server. Live PostgreSQL cases use ``DJEDAI_TEST_PG_DSN`` and the
loader DSN ``DJEDAI_DEMO_PG_LOADER_DSN`` to change the source between reads; run them
with ``scripts/test_postgres_isolation.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from djedai.agent.client import ModelResponse
from djedai.backend import chat_service
from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.backend.dataset_service import DatasetService
from djedai.backend.db.models import DatasetModel
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.local_user import bootstrap_local_user
from djedai.backend.source_reads import CatalogStaleError
from djedai.data.postgres import parse_dsn
from djedai.data.typed_views import _lit

pytestmark = pytest.mark.integration
ORDERS = b"order_id,amount\n001,10\n002,20\n"


@pytest.fixture
def storage(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "owner")
    bootstrap_local_user(factory, "other", username="other")
    yield factory, tmp_path / "datasets"
    engine.dispose()


def _csv_dataset(service: DatasetService) -> str:
    async def run() -> str:
        accepted = await service.ingest_uploads(
            "owner", [UploadFile(filename="orders.csv", file=BytesIO(ORDERS))]
        )
        await service.close()
        return accepted.dataset_id

    return asyncio.run(run())


def _refresh(service: DatasetService, owner_id: str, dataset_id: str):
    async def run():
        detail = await service.refresh(owner_id, dataset_id)
        await service.close()
        return detail

    return asyncio.run(run())


def test_csv_refresh_publishes_the_next_revision_while_old_reads_continue(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    dataset_id = _csv_dataset(service)
    first = service.get_detail("owner", dataset_id)
    assert first.revision.number == 1 and first.revision == first.catalog.revision
    assert first.freshness.state == "fresh"

    with service.get_context("owner", dataset_id).open_reader() as reader:
        accepted = _refresh(service, "owner", dataset_id)
        assert (accepted.status, accepted.refresh.status) == ("ready", "processing")
        # The connection opened before the swap keeps reading the previous file.
        assert reader.connection.execute("SELECT count(*) FROM orders").fetchone() == (2,)
        assert reader.catalog.revision.number == 1

    second = service.get_detail("owner", dataset_id)
    assert (second.status, second.refresh) == ("ready", None)
    assert second.revision.number == 2
    assert second.revision.profiled_at > first.revision.profiled_at
    assert second.revision.source_fingerprint == first.revision.source_fingerprint
    assert second.revision.schema_fingerprint == first.revision.schema_fingerprint
    with service.get_context("owner", dataset_id).open_reader() as reader:
        _, read = reader.run(lambda: None)
    assert (read.catalog_revision, read.read_mode, read.snapshot_id) == (2, "snapshot", "r2")


def test_failed_refresh_keeps_the_published_revision(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    dataset_id = _csv_dataset(service)
    with factory() as db:
        raw = directory / db.get(DatasetModel, dataset_id).raw_files[0]["path"]
    raw.write_bytes(b"")  # the retained source no longer parses

    _refresh(service, "owner", dataset_id)
    detail = service.get_detail("owner", dataset_id)
    assert (detail.status, detail.revision.number) == ("ready", 1)
    assert detail.refresh.status == "error" and detail.refresh.message
    assert "/" not in detail.refresh.message  # no internal paths
    with service.get_context("owner", dataset_id).open_reader() as reader:
        assert reader.connection.execute("SELECT count(*) FROM orders").fetchone() == (2,)

    raw.write_bytes(ORDERS)
    _refresh(service, "owner", dataset_id)
    detail = service.get_detail("owner", dataset_id)
    assert (detail.revision.number, detail.refresh) == (2, None)


def test_refresh_is_owned_and_needs_a_ready_dataset(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    dataset_id = _csv_dataset(service)
    with pytest.raises(HTTPException) as foreign:
        _refresh(service, "other", dataset_id)
    assert foreign.value.status_code == 404

    with factory() as db:
        db.get(DatasetModel, dataset_id).refresh = {
            "status": "processing",
            "started_at": "2026-10-26T10:00:00Z",
        }
        db.commit()
    with pytest.raises(HTTPException) as running:
        _refresh(service, "owner", dataset_id)
    assert running.value.status_code == 409

    with factory() as db:
        model = db.get(DatasetModel, dataset_id)
        model.refresh, model.status = None, "processing"
        db.commit()
    with pytest.raises(HTTPException) as building:
        _refresh(service, "owner", dataset_id)
    assert building.value.status_code == 409


def test_interrupted_refresh_resumes_after_restart(storage) -> None:
    factory, directory = storage
    dataset_id = _csv_dataset(DatasetService(factory, directory))
    with factory() as db:
        db.get(DatasetModel, dataset_id).refresh = {
            "status": "processing",
            "started_at": "2026-10-26T10:00:00Z",
        }
        db.commit()

    restarted = DatasetService(factory, directory)

    async def recover() -> None:
        await restarted.recover_interrupted()
        await restarted.close()

    asyncio.run(recover())
    detail = restarted.get_detail("owner", dataset_id)
    assert (detail.revision.number, detail.refresh) == (2, None)


def test_catalog_before_revisioning_is_reported_unknown(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    dataset_id = _csv_dataset(service)
    with factory() as db:
        model = db.get(DatasetModel, dataset_id)
        model.catalog = {key: value for key, value in model.catalog.items() if key != "revision"}
        db.commit()
    detail = service.get_detail("owner", dataset_id)
    assert detail.revision is None and detail.freshness.state == "unknown"
    with service.get_context("owner", dataset_id).open_reader() as reader:
        _, read = reader.run(lambda: None)
    assert (read.revision_known, read.catalog_revision, read.read_mode) == (False, None, "snapshot")

    _refresh(service, "owner", dataset_id)
    assert service.get_detail("owner", dataset_id).revision.number == 1


# --- Live PostgreSQL -------------------------------------------------------------------

DSN = os.environ.get("DJEDAI_TEST_PG_DSN")
WRITER_DSN = os.environ.get("DJEDAI_DEMO_PG_LOADER_DSN")
live_postgres = pytest.mark.skipif(
    not DSN or not WRITER_DSN,
    reason="Use scripts/test_postgres_isolation.py for a live source and its loader role",
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def source():
    """A private table changed through the loader role while datasets read it."""
    table = f"rev_{uuid4().hex[:12]}"
    info = parse_dsn(WRITER_DSN)
    writer = duckdb.connect()
    writer.execute("LOAD postgres")
    writer.execute(f"CREATE TEMPORARY SECRET w (TYPE postgres, PASSWORD {_lit(info.password)})")
    writer.execute(f"ATTACH {_lit(info.libpq(read_only=False))} AS w (TYPE postgres, SECRET w)")

    def execute(sql: str) -> None:
        writer.execute(f"CALL postgres_execute('w', {_lit(sql)})")

    execute(f"CREATE TABLE {table} (id INTEGER, amount INTEGER)")
    execute(f"INSERT INTO {table} VALUES (1, 10), (2, 20)")
    try:
        yield table, execute
    finally:
        execute(f"DROP TABLE IF EXISTS {table}")
        writer.close()


@pytest.fixture
def pg_service(storage):
    factory, directory = storage
    return DatasetService(factory, directory, source_encryption_key=Fernet.generate_key().decode())


def _connect(service: DatasetService, table: str) -> str:
    async def run() -> str:
        accepted = await service.connect_db("owner", DSN, [table])
        await service.close()
        return accepted.dataset_id

    return asyncio.run(run())


def _count(reader, table: str) -> int:
    return reader.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@live_postgres
def test_live_drift_is_detected_and_refresh_publishes_a_new_revision(pg_service, source) -> None:
    table, execute = source
    dataset_id = _connect(pg_service, table)
    detail = pg_service.get_detail("owner", dataset_id)
    assert (detail.status, detail.revision.number, detail.freshness.state) == ("ready", 1, "fresh")
    first = detail.revision
    context = pg_service.get_context("owner", dataset_id)

    with context.open_reader() as reader:
        assert reader.freshness.state == "fresh"
        count, read = reader.run(lambda: _count(reader, table))
    assert (count, read.read_mode, read.consistency) == (2, "live", "independent")

    # A compatible change keeps reads working, visibly.
    execute(f"ALTER TABLE {table} ADD COLUMN note TEXT")
    with context.open_reader() as reader:
        assert reader.freshness.state == "drifted"
        assert [(c.kind, c.column) for c in reader.freshness.changes] == [("added_column", "note")]
        _, read = reader.run(lambda: _count(reader, table))
    assert read.freshness.state == "drifted"
    assert pg_service.get_detail("owner", dataset_id).freshness.state == "drifted"

    # An incompatible change is never read silently.
    execute(f"ALTER TABLE {table} DROP COLUMN amount")
    with pytest.raises(CatalogStaleError) as stale:
        with context.open_reader():
            pass
    assert stale.value.status_code == 409
    assert "amount удалена или переименована" in stale.value.detail
    assert pg_service.check_freshness("owner", dataset_id).state == "incompatible"
    assert pg_service.get_detail("owner", dataset_id).freshness.state == "incompatible"

    _refresh(pg_service, "owner", dataset_id)
    refreshed = pg_service.get_detail("owner", dataset_id)
    assert (refreshed.revision.number, refreshed.freshness.state) == (2, "fresh")
    assert refreshed.revision.schema_fingerprint != first.schema_fingerprint
    assert refreshed.revision.source_fingerprint == first.source_fingerprint
    assert [c.name for c in refreshed.catalog.tables[0].columns] == ["id", "note"]
    with pg_service.get_context("owner", dataset_id).open_reader() as reader:
        assert reader.freshness.state == "fresh"
        _, read = reader.run(lambda: _count(reader, table))
    assert read.catalog_revision == 2

    execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE BIGINT")
    assert pg_service.check_freshness("owner", dataset_id).changes[0].kind == "type_changed"
    execute(f"DROP TABLE {table}")
    gone = pg_service.check_freshness("owner", dataset_id)
    assert (gone.state, gone.changes[0].kind) == ("incompatible", "dropped_table")


@live_postgres
def test_turn_snapshot_hides_concurrent_changes_until_it_is_renewed(pg_service, source) -> None:
    table, execute = source
    dataset_id = _connect(pg_service, table)
    with pg_service.get_context("owner", dataset_id).open_reader() as reader:
        reader.share_snapshot("run_t")
        before, first = reader.run(lambda: _count(reader, table))
        execute(f"INSERT INTO {table} VALUES (3, 30)")
        after, second = reader.run(lambda: _count(reader, table))
        assert (before, after) == (2, 2)
        assert first.snapshot_id == second.snapshot_id == "run_t:1"
        with pytest.raises(duckdb.Error):
            reader.run(lambda: reader.connection.execute("SELECT CAST('x' AS INTEGER)").fetchall())
        renewed, third = reader.run(lambda: _count(reader, table))
        assert (renewed, third.snapshot_id) == (3, "run_t:2")

    with pg_service.get_context("owner", dataset_id).open_reader() as reader:
        first_count, _ = reader.run(lambda: _count(reader, table))
        execute(f"INSERT INTO {table} VALUES (4, 40)")
        second_count, read = reader.run(lambda: _count(reader, table))
    # Without a shared snapshot every read is independent and says so.
    assert (first_count, second_count, read.consistency) == (3, 4, "independent")


class TwoStepModel:
    """A deterministic two-step plan; no language model server is used."""

    table = ""

    def generate_typed_response(self, system_prompt, user_prompt, response_model, **kwargs):
        name = response_model.__name__
        if name == "Understanding":
            value = {"intent": "data_question", "resolved_question": user_prompt}
        elif name == "PlanResponse":
            value = {
                "plan": [
                    {"tool": "run_sql", "args": {"sql": f"SELECT COUNT(*) AS n FROM {self.table}"}},
                    {
                        "tool": "run_sql",
                        "args": {"sql": f"SELECT SUM(id) AS s FROM {self.table}"},
                    },
                ]
            }
        elif name == "VisualizeResponse":
            value = {"charts": []}
        elif name == "ComposeResponse":
            value = {"answer_blocks": [{"type": "text", "markdown": "Расчёт завершён."}]}
        elif name == "VerifyResponse":
            value = {"is_valid": True, "issues": [], "corrected_markdown": "Расчёт завершён."}
        else:
            raise AssertionError(f"Unexpected model response contract {name}")
        return ModelResponse(status="success", data=response_model.model_validate(value))


@pytest.fixture
def api(tmp_path: Path, monkeypatch, source):
    table, execute = source
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", url)
    command.upgrade(migration, "head")
    TwoStepModel.table = table
    monkeypatch.setattr(chat_service, "get_agent_client", TwoStepModel)
    settings = Settings(
        _env_file=None,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
        results_data_dir=str(tmp_path / "results"),
        source_encryption_key=Fernet.generate_key().decode(),
        cp1_local_mode=False,
        auth_rate_limit_requests=20,
    )
    return settings, table, execute


def _account(client) -> dict:
    credentials = {"username": "owner169", "password": "test-only-account-password"}
    assert client.post("/api/auth/register", json=credentials).status_code == 201
    login = client.post("/api/auth/login", json=credentials)
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _login(client) -> dict:
    credentials = {"username": "owner169", "password": "test-only-account-password"}
    login = client.post("/api/auth/login", json=credentials)
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _ask(client, headers, chat_id) -> tuple[str, list[str]]:
    stream = client.post(
        f"/api/chats/{chat_id}/messages", headers=headers, json={"text": "Сколько и сумма?"}
    )
    assert stream.status_code == 200, stream.text
    ids = [
        json.loads(frame.split("data: ", 1)[1])["result_id"]
        for frame in stream.text.split("\n\n")
        if frame.startswith("event: tool_result\n")
    ]
    return stream.text, ids


@live_postgres
def test_chat_turn_reads_one_snapshot_and_saved_results_survive_drift(api, monkeypatch) -> None:
    settings, table, execute = api
    original = chat_service.assess
    calls = []

    def assess_then_change(result, connection, catalog):
        assessed = original(result, connection, catalog)
        if not calls:  # between the two plan steps, another session changes the source
            execute(f"INSERT INTO {table} VALUES (3, 30)")
        calls.append(result.result_id)
        return assessed

    monkeypatch.setattr(chat_service, "assess", assess_then_change)
    with TestClient(create_app(settings)) as client:
        headers = _account(client)
        accepted = client.post(
            "/api/datasets/connect",
            headers=headers,
            json={"kind": "postgres", "dsn": DSN, "tables": [table]},
        )
        dataset_id = accepted.json()["dataset_id"]
        assert (
            "event: done\n"
            in client.get(f"/api/datasets/{dataset_id}/events", headers=headers).text
        )
        chat_id = client.post(
            "/api/chats", headers=headers, json={"dataset_id": dataset_id}
        ).json()["chat_id"]
        text, (count_id, sum_id) = _ask(client, headers, chat_id)
        assert "event: done\n" in text
        rows = {
            rid: client.get(f"/api/results/{rid}/data", headers=headers).json()["rows"]
            for rid in (count_id, sum_id)
        }
        # Both steps saw the source before the concurrent insert.
        assert rows == {count_id: [[2]], sum_id: [[3]]}
        reads = [
            client.get(f"/api/results/{rid}", headers=headers).json()["read"]
            for rid in (count_id, sum_id)
        ]
        assert {r["snapshot_id"] for r in reads} == {reads[0]["snapshot_id"]}
        assert {(r["read_mode"], r["consistency"], r["catalog_revision"]) for r in reads} == {
            ("live", "turn_snapshot", 1)
        }
        answer = client.get(f"/api/chats/{chat_id}", headers=headers).json()["messages"][-1]
        assert any("живое чтение источника" in b.get("markdown", "") for b in answer["blocks"])

        execute(f"ALTER TABLE {table} DROP COLUMN amount")
        text, ids = _ask(client, headers, chat_id)
        assert "event: error\n" in text and '"catalog_stale"' in text and ids == []
        stale = client.get(f"/api/datasets/{dataset_id}", headers=headers).json()
        assert stale["freshness"]["state"] == "incompatible"

    with TestClient(create_app(settings)) as restarted:
        headers = _login(restarted)
        execute(f"DROP TABLE {table}")  # the saved result must not need the source
        saved = restarted.get(f"/api/results/{count_id}/data", headers=headers)
        assert saved.status_code == 200 and saved.json()["rows"] == [[2]]
        assert restarted.get(f"/api/results/{count_id}", headers=headers).json()["read"] == reads[0]
        check = restarted.post(f"/api/datasets/{dataset_id}/freshness", headers=headers)
        assert check.status_code == 200 and check.json()["changes"][0]["kind"] == "dropped_table"
