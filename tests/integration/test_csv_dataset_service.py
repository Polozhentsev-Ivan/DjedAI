"""Real CSV/DuckDB/SQLite processing, restart, ownership and HTTP boundaries."""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
from pathlib import Path

import duckdb
import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from djedai.agent.client import ModelResponse
from djedai.backend.dataset_service import DatasetService
from djedai.backend.db.models import DatasetModel, UserModel
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.local_user import bootstrap_local_user
from djedai.data.descriptions import DatasetSummary, TableDescriptions
from djedai.data.service import CsvSource, ingest_csv_dataset

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "tests" / "fixtures" / "synthetic"


@pytest.fixture
def storage(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "owner")
    bootstrap_local_user(factory, "other", username="other")
    yield factory, tmp_path / "datasets"
    engine.dispose()


def upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=BytesIO(content))


def test_multiple_csvs_survive_new_service_and_keep_original_bytes(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    orders = b"order_id,amount\n001,10\n002,20\n"
    customers = "customer_id;name\n01;Иван\n".encode("cp1251")

    async def run():
        accepted = await service.ingest_uploads(
            "owner", [upload("orders.csv", orders), upload("customers.csv", customers)]
        )
        assert accepted.status == "processing"
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(run())
    restarted = DatasetService(factory, directory)
    detail = restarted.get_detail("owner", dataset_id)
    assert detail.status == "ready"
    assert (detail.table_count, detail.row_count, detail.source.files) == (2, 3, 2)
    assert {table.table_id for table in detail.catalog.tables} == {"orders", "customers"}
    assert detail.catalog.table("customers").source.encoding == "cp1251"
    with factory() as db:
        model = db.get(DatasetModel, dataset_id)
        assert not Path(model.duckdb_path).is_absolute()
        assert (directory / model.raw_files[0]["path"]).read_bytes() == orders
        assert (directory / model.raw_files[1]["path"]).read_bytes() == customers
    context = restarted.get_context("owner", dataset_id)
    with duckdb.connect(str(context.duckdb_path), read_only=True) as connection:
        assert connection.execute(
            "SELECT order_id, amount FROM orders ORDER BY order_id"
        ).fetchall() == [("001", 10), ("002", 20)]
        assert connection.execute("SELECT COUNT(*) FROM raw_orders").fetchone() == (2,)
        assert connection.execute("SELECT name FROM customers").fetchone() == ("Иван",)
    assert len(restarted.list_for_owner("owner")) == 1
    assert restarted.list_for_owner("other") == []
    for method in (restarted.get_detail, restarted.get_context):
        with pytest.raises(HTTPException) as denied:
            method("other", dataset_id)
        assert denied.value.status_code == 404
    terminal = asyncio.run(_events(restarted, "owner", dataset_id))
    assert [event.event for event in terminal] == ["done"]
    assert terminal[0].data.dataset_id == dataset_id


async def _events(service, owner_id, dataset_id):
    return [event async for event in service.events(owner_id, dataset_id)]


def test_empty_csv_persists_terminal_error(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)

    async def run():
        accepted = await service.ingest_uploads("owner", [upload("empty.csv", b"")])
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(run())
    restarted = DatasetService(factory, directory)
    detail = restarted.get_detail("owner", dataset_id)
    assert detail.status == "error"
    assert detail.catalog is None
    assert "пуст" in detail.error.message
    assert detail.error.code == "dataset_processing_failed"
    events = asyncio.run(_events(restarted, "owner", dataset_id))
    assert [event.event for event in events] == ["error"]
    assert "пуст" in events[0].data.message
    with pytest.raises(HTTPException) as pending:
        restarted.get_context("owner", dataset_id)
    assert pending.value.status_code == 409


def test_recovery_rebuilds_interrupted_database_from_retained_raw_files(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    # Persisted upload has no active task, as after an abruptly interrupted process.
    dataset_id = service._persist_upload("owner", [upload("fresh.csv", b"amount\n10\n20\n")])
    partial = directory / dataset_id / "dataset.building.duckdb"
    partial.write_bytes(b"interrupted build")
    Path(str(partial) + ".wal").write_bytes(b"interrupted write ahead log")
    restarted = DatasetService(factory, directory)

    async def run():
        await restarted.recover_interrupted()
        await restarted.recover_interrupted()  # Does not duplicate an active job.
        await restarted.close()

    asyncio.run(run())
    assert restarted.get_detail("owner", dataset_id).status == "ready"
    assert not partial.exists()
    assert not Path(str(partial) + ".wal").exists()
    with duckdb.connect(str(restarted.get_context("owner", dataset_id).duckdb_path)) as connection:
        assert connection.execute("SELECT SUM(amount) FROM fresh").fetchone() == (30,)


def test_recovery_missing_source_becomes_stored_error(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    dataset_id = service._persist_upload("owner", [upload("lost.csv", b"n\n1\n")])
    (directory / dataset_id / "raw" / "0000.csv").unlink()

    async def run():
        await service.recover_interrupted()
        await service.close()

    asyncio.run(run())
    assert service.get_detail("owner", dataset_id).status == "error"
    assert [event.event for event in asyncio.run(_events(service, "owner", dataset_id))] == [
        "error"
    ]


@pytest.mark.parametrize("files", [[], [upload("not-a-csv.exe", b"abc")]])
def test_invalid_upload_creates_no_dataset(storage, files) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(service.ingest_uploads("owner", files))
    assert invalid.value.status_code == 422
    assert service.list_for_owner("owner") == []
    assert not directory.exists()


def test_duplicate_filenames_have_distinct_tables_and_cannot_escape_storage(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)

    async def run():
        accepted = await service.ingest_uploads(
            "owner",
            [
                upload("../../orders.csv", b"amount\n10\n"),
                upload("C:\\outside\\orders.csv", b"amount\n20\n"),
            ],
        )
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(run())
    detail = service.get_detail("owner", dataset_id)
    assert [table.table_id for table in detail.catalog.tables] == ["orders", "orders_2"]
    assert [table.original_name for table in detail.catalog.tables] == ["orders.csv", "orders.csv"]
    with pytest.raises(ValueError, match="каталог"):
        service._path("../outside.csv")


def test_internal_failure_is_sanitized_and_saved(storage, monkeypatch) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)

    def broken_pipeline(*args, **kwargs):
        raise RuntimeError("secret://password@private-host/path")

    monkeypatch.setattr("djedai.backend.dataset_service.ingest_csv_dataset", broken_pipeline)

    async def run():
        accepted = await service.ingest_uploads("owner", [upload("rows.csv", b"n\n1\n")])
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(run())
    detail = service.get_detail("owner", dataset_id)
    assert detail.status == "error"
    assert "secret" not in detail.model_dump_json()
    assert "private-host" not in detail.model_dump_json()
    with factory() as db:
        assert "secret" not in db.get(DatasetModel, dataset_id).error_message


def test_deleted_database_cannot_be_used_as_ready_context(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)

    async def run():
        accepted = await service.ingest_uploads("owner", [upload("rows.csv", b"n\n1\n")])
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(run())
    service.get_context("owner", dataset_id).duckdb_path.unlink()
    with pytest.raises(HTTPException) as missing:
        service.get_context("owner", dataset_id)
    assert missing.value.status_code == 409


def test_bootstrap_is_idempotent_and_preserves_user(storage) -> None:
    factory, _ = storage
    assert bootstrap_local_user(factory, "owner") == "owner"
    with factory() as db:
        assert len(list(db.scalars(select(UserModel)))) == 2
        user = db.get(UserModel, "owner")
        assert user.password_hash is None
        user.is_active = False
        db.commit()
    with pytest.raises(ValueError, match="неактивен"):
        bootstrap_local_user(factory, "owner")
    with pytest.raises(ValueError, match="занято"):
        bootstrap_local_user(factory, "new", username="other")


def test_public_progress_is_monotonic_when_recovery_repeats_stages(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    dataset_id = service._persist_upload("owner", [upload("rows.csv", b"n\n1\n")])
    service._update_progress("owner", dataset_id, "profile", 0.8, "Профилирование")
    service._update_progress("owner", dataset_id, "read", 0.0, "Повторное чтение")
    with factory() as db:
        assert db.get(DatasetModel, dataset_id).progress["progress"] == 0.8

    async def read_progress():
        events = service.events("owner", dataset_id)
        try:
            first = await anext(events)
            service._update_progress("owner", dataset_id, "types", 0.9, "Типизация")
            second = await anext(events)
            return first, second
        finally:
            await events.aclose()

    first, second = asyncio.run(read_progress())
    assert first.event == second.event == "progress"
    assert (first.data.stage, first.data.progress) == ("read", 0.8)
    assert (second.data.stage, second.data.progress) == ("types", 0.9)


def test_legacy_csv_metadata_is_exposed_with_canonical_source_kind(storage) -> None:
    factory, directory = storage
    with factory() as db:
        db.add(
            DatasetModel(
                dataset_id="prior",
                owner_id="owner",
                name="Existing CSV",
                source_kind="csv",
                status="ready",
            )
        )
        db.commit()
    service = DatasetService(factory, directory)
    assert service.list_for_owner("owner")[0].source.kind == "csv_upload"
    assert service.get_detail("owner", "prior").source.kind == "csv_upload"
    with pytest.raises(HTTPException) as unavailable:
        service.get_context("owner", "prior")
    assert unavailable.value.status_code == 409


def test_upload_io_failure_leaves_no_partial_metadata_or_files(storage) -> None:
    factory, directory = storage

    class BrokenUpload(BytesIO):
        def read(self, *args, **kwargs):
            raise OSError("Source stream interrupted")

    service = DatasetService(factory, directory)
    broken = UploadFile(filename="interrupted.csv", file=BrokenUpload(b"n\n1\n"))
    with pytest.raises(OSError, match="interrupted"):
        asyncio.run(service.ingest_uploads("owner", [broken]))
    assert service.list_for_owner("owner") == []
    assert list(directory.iterdir()) == []


def test_data_pipeline_progress_matches_ui_stage_ids(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    sources = []
    for index in range(2):
        source = tmp_path / f"rows{index}.csv"
        source.write_text("amount\n10\n20\n", encoding="utf-8")
        sources.append(CsvSource(source, source.name))
    progress = []
    catalog = ingest_csv_dataset(
        sources,
        tmp_path / "data.duckdb",
        dataset_id="id",
        name="Multiple files",
        created_at=datetime.now(UTC),
        progress=lambda *values: progress.append(values),
    )
    assert len(catalog.tables) == 2
    assert [stage for stage, _, _ in progress] == ["read", "types", "profile"] * 2 + ["profile"]
    assert progress[-1][2] == "Поиск связей"
    fractions = [fraction for _, fraction, _ in progress]
    assert fractions == sorted(fractions)
    with pytest.raises(ValueError, match="хотя бы один"):
        ingest_csv_dataset(
            [],
            tmp_path / "empty.duckdb",
            dataset_id="empty",
            name="Empty",
            created_at=datetime.now(UTC),
        )


def test_python_csv_facade_snapshots_paths_checks_owner_and_survives_restart(
    storage, tmp_path: Path
) -> None:
    factory, directory = storage
    original = tmp_path / "caller-original.csv"
    original.write_bytes(b"order_id,amount\n001,10\n002,20\n")
    service = DatasetService(factory, directory)

    async def ingest():
        accepted = await service.ingest_csv("owner", [CsvSource(original, "orders.csv")])
        assert accepted.status == "processing"
        # Processing must use the service's retained copy, even if the caller
        # removes its source immediately after acceptance.
        original.unlink()
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(ingest())
    restarted = DatasetService(factory, directory)
    catalog = restarted.get_catalog("owner", dataset_id)
    assert catalog.dataset_id == dataset_id
    assert catalog.tables[0].table_id == "orders"
    assert catalog.tables[0].row_count == 2
    assert restarted.get_detail("owner", dataset_id).catalog == catalog
    with pytest.raises(HTTPException) as denied:
        restarted.get_catalog("other", dataset_id)
    assert denied.value.status_code == 404
    with duckdb.connect(str(restarted.get_context("owner", dataset_id).duckdb_path)) as connection:
        assert connection.execute(
            "SELECT order_id, amount FROM orders ORDER BY order_id"
        ).fetchall() == [("001", 10), ("002", 20)]


def test_database_reference_cannot_point_to_another_owned_directory(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)

    async def run():
        first = await service.ingest_uploads("owner", [upload("a.csv", b"amount\n10\n")])
        second = await service.ingest_uploads("other", [upload("b.csv", b"amount\n999\n")])
        await service.close()
        return first.dataset_id, second.dataset_id

    first, second = asyncio.run(run())
    with factory() as db:
        db.get(DatasetModel, first).duckdb_path = f"{second}/dataset.duckdb"
        db.commit()
    with pytest.raises(HTTPException) as denied:
        service.get_context("owner", first)
    assert denied.value.status_code == 409
    with service.get_context("other", second).open_connection() as connection:
        assert connection.execute("SELECT amount FROM b").fetchone() == (999,)


def test_recovery_rejects_cross_dataset_raw_reference_and_wrong_owner(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)
    first = service._persist_upload("owner", [upload("a.csv", b"amount\n10\n")])
    second = service._persist_upload("other", [upload("b.csv", b"amount\n999\n")])
    with factory() as db:
        db.get(DatasetModel, first).raw_files = [
            {"name": "a.csv", "path": f"{second}/raw/0000.csv"}
        ]
        db.commit()
    service._process("owner", second)
    service._update_progress("owner", second, "ready", 1.0, "Wrong owner")
    assert service.get_detail("other", second).status == "processing"
    with factory() as db:
        assert db.get(DatasetModel, second).progress["stage"] == "queued"

    async def run():
        await service.recover_interrupted()
        await service.close()

    asyncio.run(run())
    assert service.get_detail("owner", first).status == "error"
    assert service.get_detail("other", second).status == "ready"


def test_symbolic_database_reference_is_rejected_at_lookup_and_open(storage) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)

    async def run():
        first = await service.ingest_uploads("owner", [upload("a.csv", b"n\n1\n")])
        second = await service.ingest_uploads("other", [upload("b.csv", b"n\n2\n")])
        await service.close()
        return first.dataset_id, second.dataset_id

    first, second = asyncio.run(run())
    context = service.get_context("owner", first)
    path = context.duckdb_path
    path.unlink()
    try:
        path.symlink_to(directory / second / "dataset.duckdb")
    except OSError:
        pytest.skip("Creating symbolic links is not permitted on this host")
    with pytest.raises(HTTPException) as lookup:
        service.get_context("owner", first)
    assert lookup.value.status_code == 409
    with pytest.raises(HTTPException) as opened, context.open_connection():
        pytest.fail("A replaced path must not open")
    assert opened.value.status_code == 409


def test_csv_owned_context_disables_external_files_and_database_writes(storage, tmp_path) -> None:
    factory, directory = storage
    service = DatasetService(factory, directory)

    async def run():
        accepted = await service.ingest_uploads("owner", [upload("a.csv", b"amount\n10\n")])
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(run())
    outside = tmp_path / "private.csv"
    outside.write_text("secret\nprivate\n", encoding="utf-8")
    with service.get_context("owner", dataset_id).open_connection() as connection:
        assert connection.execute("SELECT amount FROM a").fetchone() == (10,)
        with pytest.raises(duckdb.Error):
            connection.execute("CREATE TABLE injected (n INTEGER)")
        with pytest.raises(duckdb.Error):
            connection.execute("SELECT * FROM read_csv(?)", [str(outside)])


def _relation_key(relation) -> tuple[str, str, str, str]:
    return (
        relation["from"]["table"],
        relation["from"]["column"],
        relation["to"]["table"],
        relation["to"]["column"],
    )


def test_csv_catalog_includes_synthetic_relations_for_the_agent(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    expected = json.loads((ROOT / "eval" / "synthetic_expectations.json").read_text("utf-8"))
    files = sorted(SYNTHETIC.glob("*.csv"))
    catalog = ingest_csv_dataset(
        [CsvSource(path, path.name) for path in files],
        tmp_path / "synthetic.duckdb",
        dataset_id="synthetic",
        name="Синтетика",
        created_at=datetime.now(UTC),
    )
    found = {
        _relation_key(relation): relation["cardinality"]
        for relation in catalog.model_dump(mode="json", by_alias=True)["relations"]
    }
    for relation in expected["relations"]["expected"]:
        assert found[_relation_key(relation)] == relation["cardinality"]
    for relation in expected["relations"]["not_expected"]:
        assert _relation_key(relation) not in found
    prompt = catalog.to_prompt()
    assert "FK → products.product_id" in prompt
    assert "- sales.store_id → stores.store_id (N:1" in prompt


class FakeClient:
    """Answers description prompts from their JSON input; no model server is used."""

    model = "fake-model"

    def __init__(self, error_type: str | None = None) -> None:
        self.error_type = error_type
        self.calls = 0

    def generate_typed_response(self, system_prompt, user_prompt, response_model, **kwargs):
        self.calls += 1
        if self.error_type:
            return ModelResponse(status="error", error_type=self.error_type, message="сбой")
        payload = json.loads(user_prompt)
        if response_model is DatasetSummary:
            names = ", ".join(table["table"] for table in payload["tables"])
            return ModelResponse(status="success", data=DatasetSummary(summary=f"Данные: {names}"))
        columns = [
            {"name": column["name"], "description": f"Смысл колонки {column['name']}"}
            for column in payload["columns"]
        ]
        data = TableDescriptions(table=f"Строка таблицы {payload['table']}", columns=columns)
        return ModelResponse(status="success", data=data)


def _ingest_described(storage, tmp_path: Path, client_factory):
    factory, directory = storage
    source = tmp_path / "orders.csv"
    source.write_bytes(b"order_id,amount\n001,10\n002,20\n")
    service = DatasetService(factory, directory, description_client=client_factory)

    async def ingest():
        accepted = await service.ingest_csv("owner", [CsvSource(source, "orders.csv")])
        await service.close()
        return accepted.dataset_id

    dataset_id = asyncio.run(ingest())
    assert service.get_detail("owner", dataset_id).status == "ready"
    return service.get_catalog("owner", dataset_id)


def test_published_catalog_has_llm_descriptions_when_enabled(storage, tmp_path: Path) -> None:
    client = FakeClient()
    catalog = _ingest_described(storage, tmp_path, lambda: client)
    table = catalog.tables[0]
    assert table.description == "Строка таблицы orders"
    assert [column.description for column in table.columns] == [
        "Смысл колонки order_id",
        "Смысл колонки amount",
    ]
    assert catalog.summary == "Данные: orders"
    assert client.calls == 2
    assert catalog.warnings == []


def test_unavailable_model_still_publishes_the_statistical_catalog(storage, tmp_path: Path) -> None:
    catalog = _ingest_described(storage, tmp_path, lambda: FakeClient("TIMEOUT"))
    assert catalog.tables[0].row_count == 2
    assert catalog.tables[0].description is None
    assert any("модель недоступна" in warning for warning in catalog.warnings)


def test_misconfigured_model_never_fails_the_dataset(storage, tmp_path: Path) -> None:
    def unconfigured():
        raise ValueError("MODEL_API_KEY=secret-must-not-leak")

    catalog = _ingest_described(storage, tmp_path, unconfigured)
    assert catalog.tables[0].row_count == 2
    assert catalog.summary is None
    assert len(catalog.warnings) == 1
    assert "не настроена или недоступна" in catalog.warnings[0]
    assert "secret" not in catalog.model_dump_json()
