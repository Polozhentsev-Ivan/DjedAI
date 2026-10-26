"""Catalog revisions, source freshness and re-profiling (B1-11, #169).

CSV cases need no server. Live PostgreSQL cases use ``DJEDAI_TEST_PG_DSN`` and the
loader DSN ``DJEDAI_DEMO_PG_LOADER_DSN`` to change the source between reads; run them
with ``scripts/test_postgres_isolation.py``.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import sessionmaker

from djedai.backend.dataset_service import DatasetService
from djedai.backend.db.models import DatasetModel
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.local_user import bootstrap_local_user

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
