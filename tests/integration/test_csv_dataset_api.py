"""Public multipart/JSON/SSE API backed by real local SQLite and DuckDB."""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.backend.db.models import DatasetModel
from djedai.backend.db.session import create_db_engine
from djedai.backend.local_user import bootstrap_local_user

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def configured(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "local")
    bootstrap_local_user(factory, "other", username="other")
    settings = Settings(
        _env_file=None,
        cp1_local_mode=True,
        cp1_local_user_id="local",
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
    )
    yield settings, factory
    engine.dispose()


def test_public_upload_catalog_list_and_terminal_sse_survive_restart(configured) -> None:
    settings, _ = configured
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/datasets/upload",
            files=[
                ("files", ("sales.csv", b"sale_id,amount\n001,10\n002,20\n", "text/csv")),
                ("files", ("customers.csv", b"customer_id,name\n01,Alice\n", "text/csv")),
            ],
        )
        assert response.status_code == 202
        dataset_id = response.json()["dataset_id"]
        assert response.json() == {"dataset_id": dataset_id, "status": "processing"}
        events = client.get(f"/api/datasets/{dataset_id}/events")
        assert events.status_code == 200
        assert events.headers["content-type"].startswith("text/event-stream")
        assert "event: done\n" in events.text
        assert "event: error\n" not in events.text
        assert f'"dataset_id":"{dataset_id}"' in events.text
        assert '"event":' not in events.text  # SSE data has no duplicate event wrapper.
        detail = client.get(f"/api/datasets/{dataset_id}").json()
        assert detail["status"] == "ready"
        assert (detail["table_count"], detail["row_count"]) == (2, 3)
        assert detail["source"] == {"kind": "csv_upload", "files": 2}
        assert client.get(f"/api/datasets/{dataset_id}/catalog").json() == detail["catalog"]
        assert client.get("/api/datasets").json()[0]["dataset_id"] == dataset_id

    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/api/datasets/{dataset_id}").json() == detail
        assert "event: done\n" in restarted.get(f"/api/datasets/{dataset_id}/events").text
        context = restarted.app.state.dataset_service.get_context("local", dataset_id)
        with duckdb.connect(str(context.duckdb_path), read_only=True) as connection:
            assert connection.execute("SELECT SUM(amount) FROM sales").fetchone() == (30,)

    settings.cp1_local_user_id = "other"
    with TestClient(create_app(settings)) as other:
        assert other.get("/api/datasets").json() == []
        for path in ("", "/catalog", "/events"):
            response = other.get(f"/api/datasets/{dataset_id}{path}")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"


def test_api_error_is_persisted_and_stream_ends_without_done(configured) -> None:
    settings, _ = configured
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/datasets/upload", files={"files": ("bad.csv", b"")})
        dataset_id = response.json()["dataset_id"]
        events = client.get(f"/api/datasets/{dataset_id}/events")
        assert "event: error\n" in events.text
        assert "event: done\n" not in events.text
        assert "пуст" in events.text
        detail = client.get(f"/api/datasets/{dataset_id}").json()
        assert detail["status"] == "error"
        assert detail["error"]["code"] == "dataset_processing_failed"
        assert client.get(f"/api/datasets/{dataset_id}/catalog").status_code == 409
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/api/datasets/{dataset_id}").json() == detail


def test_processing_does_not_block_http_event_loop(configured, monkeypatch) -> None:
    settings, _ = configured
    from djedai.backend import dataset_service

    original = dataset_service.ingest_csv_dataset
    entered, release = threading.Event(), threading.Event()

    def held_pipeline(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(dataset_service, "ingest_csv_dataset", held_pipeline)
    with TestClient(create_app(settings)) as client:
        try:
            response = client.post(
                "/api/datasets/upload", files={"files": ("slow.csv", b"amount\n10\n")}
            )
            assert response.status_code == 202
            assert entered.wait(timeout=5)
            # The CPU/I/O worker remains blocked while the server handles new requests.
            assert client.get("/health").status_code == 200
            detail = client.get(f"/api/datasets/{response.json()['dataset_id']}").json()
            assert detail["status"] == "processing"
            assert client.get("/api/datasets").json()[0]["status"] == "processing"
        finally:
            release.set()


def test_mode_and_bootstrapped_owner_are_required(configured) -> None:
    settings, _ = configured
    settings.cp1_local_mode = False
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/datasets").status_code == 503
    settings.cp1_local_mode = True
    settings.cp1_local_user_id = "unprepared"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/datasets").status_code == 503


def test_invalid_upload_and_missing_id_use_standard_error(configured) -> None:
    settings, _ = configured
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/datasets/upload").status_code == 422
        response = client.post("/api/datasets/upload", files={"files": ("bad.exe", b"x")})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "http_422"
        assert client.get("/api/datasets/unknown/events").status_code == 404
        assert client.get("/api/datasets").json() == []


def test_migration_preserves_existing_dataset_and_downgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    command.upgrade(cfg, "b7f3c1d2e901")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id,username,is_active,created_at,updated_at) "
            "VALUES ('existing','existing',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO datasets "
            "(dataset_id,owner_id,name,source_kind,status,created_at,updated_at) "
            "VALUES ('prior','existing','Prior data','csv_upload','ready',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
    command.upgrade(cfg, "head")
    with sessionmaker(bind=engine)() as db:
        prior = db.get(DatasetModel, "prior")
        assert prior.name == "Prior data"
        assert prior.duckdb_path is None
        assert prior.raw_files is None
        assert prior.progress is None
    command.downgrade(cfg, "b7f3c1d2e901")
    assert "duckdb_path" not in {
        column["name"] for column in inspect(engine).get_columns("datasets")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name FROM datasets").scalar() == "Prior data"
    engine.dispose()
