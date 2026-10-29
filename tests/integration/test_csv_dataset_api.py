"""Public multipart/JSON/SSE API backed by real local SQLite and DuckDB."""

from __future__ import annotations

import errno
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from djedai.backend.app import create_app
from djedai.backend.auth import hash_password
from djedai.backend.config import Settings
from djedai.backend.db.models import DatasetModel, UserModel
from djedai.backend.db.session import create_db_engine
from djedai.backend.local_user import bootstrap_local_user

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from fake_full_disk import install_full_disk  # noqa: E402

PASSWORD = "test-csv-password"


@pytest.fixture(scope="module")
def account_password_hash() -> str:
    return hash_password(PASSWORD)


@contextmanager
def _client(settings: Settings, username: str = "local") -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        yield client


@pytest.fixture
def configured(tmp_path: Path, account_password_hash: str):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "local", username="local")
    bootstrap_local_user(factory, "other", username="other")
    with factory() as db:
        for user_id in ("local", "other"):
            db.get(UserModel, user_id).password_hash = account_password_hash
        db.commit()
    settings = Settings(
        _env_file=None,
        cp1_local_mode=False,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
    )
    yield settings, factory
    engine.dispose()


def test_public_upload_catalog_list_and_terminal_sse_survive_restart(configured) -> None:
    settings, _ = configured
    with _client(settings) as client:
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

    with _client(settings) as restarted:
        assert restarted.get(f"/api/datasets/{dataset_id}").json() == detail
        assert "event: done\n" in restarted.get(f"/api/datasets/{dataset_id}/events").text
        context = restarted.app.state.dataset_service.get_context("local", dataset_id)
        with duckdb.connect(str(context.duckdb_path), read_only=True) as connection:
            assert connection.execute("SELECT SUM(amount) FROM sales").fetchone() == (30,)

    with _client(settings, "other") as other:
        assert other.get("/api/datasets").json() == []
        for path in ("", "/catalog", "/events"):
            response = other.get(f"/api/datasets/{dataset_id}{path}")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "filenames", [("raw_sales.csv", "sales.csv"), ("sales.csv", "raw_sales.csv")]
)
def test_raw_prefix_filenames_upload_in_either_order_and_survive_restart(
    configured, filenames
) -> None:
    settings, _ = configured
    expected = {
        "raw_sales.csv": [(1, 10), (2, 20)],
        "sales.csv": [(3, 40), (4, 50)],
    }
    with _client(settings) as client:
        response = client.post(
            "/api/datasets/upload",
            files=[
                (
                    "files",
                    (
                        name,
                        "id,amount\n"
                        + "".join(f"{key},{amount}\n" for key, amount in expected[name]),
                        "text/csv",
                    ),
                )
                for name in filenames
            ],
        )
        assert response.status_code == 202
        dataset_id = response.json()["dataset_id"]
        events = client.get(f"/api/datasets/{dataset_id}/events")
        assert "event: done\n" in events.text
        assert "event: error\n" not in events.text
        detail = client.get(f"/api/datasets/{dataset_id}").json()
        assert detail["status"] == "ready"
        assert (detail["table_count"], detail["row_count"]) == (2, 4)
        tables = detail["catalog"]["tables"]
        assert {table["original_name"] for table in tables} == set(filenames)
        assert len({table["table_id"] for table in tables}) == 2
        assert len({table["raw_table"] for table in tables}) == 2

    with _client(settings) as restarted:
        assert restarted.get(f"/api/datasets/{dataset_id}").json() == detail
        assert restarted.get(f"/api/datasets/{dataset_id}/catalog").json() == detail["catalog"]
        context = restarted.app.state.dataset_service.get_context("local", dataset_id)
        with duckdb.connect(str(context.duckdb_path), read_only=True) as connection:
            for table in tables:
                original = expected[table["original_name"]]
                assert connection.execute(
                    f'SELECT id, amount FROM "{table["table_id"]}" ORDER BY id'
                ).fetchall() == [(str(key), amount) for key, amount in original]
                assert connection.execute(
                    f'SELECT id, amount FROM "{table["raw_table"]}" ORDER BY id'
                ).fetchall() == [(str(key), str(amount)) for key, amount in original]


def test_api_error_is_persisted_and_stream_ends_without_done(configured) -> None:
    settings, _ = configured
    with _client(settings) as client:
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
    with _client(settings) as restarted:
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
    with _client(settings) as client:
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


@pytest.mark.parametrize("local_mode", [False, True])
def test_local_mode_never_grants_anonymous_access(configured, local_mode: bool) -> None:
    settings, _ = configured
    settings.cp1_local_mode = local_mode
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/datasets")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_invalid_upload_and_missing_id_use_standard_error(configured) -> None:
    settings, _ = configured
    with _client(settings) as client:
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


def test_upload_without_temp_space_returns_507_and_closes_files(configured, monkeypatch) -> None:
    settings, _ = configured
    full_disk = install_full_disk(monkeypatch, capacity=3000)
    content = b"a,b\n" + b"1,2\n" * 400
    with _client(settings) as client:
        response = client.post(
            "/api/datasets/upload",
            files=[("files", (f"part{index}.csv", content, "text/csv")) for index in range(3)],
        )
        assert response.status_code == 507, response.text
        error = response.json()["error"]
        assert error["code"] == "insufficient_storage"
        assert "Недостаточно места для загрузки" in error["message"]
        assert full_disk.created
        assert all(file.closed for file in full_disk.created)
        assert client.get("/api/datasets").json() == []
    data_dir = Path(settings.datasets_data_dir)
    assert not data_dir.exists() or not any(data_dir.iterdir())


def test_upload_without_raw_space_returns_507_and_rolls_back(configured, monkeypatch) -> None:
    settings, _ = configured
    original_open = Path.open

    class FullRawFile:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self) -> FullRawFile:
            return self

        def __exit__(self, *exc_info) -> None:
            self.handle.close()

        def write(self, data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    def open_raw(self: Path, mode: str = "r", *args, **kwargs):
        handle = original_open(self, mode, *args, **kwargs)
        return FullRawFile(handle) if self.parent.name == "raw" and "w" in mode else handle

    monkeypatch.setattr(Path, "open", open_raw)
    with _client(settings) as client:
        response = client.post(
            "/api/datasets/upload", files={"files": ("sales.csv", b"a,b\n1,2\n", "text/csv")}
        )
        assert response.status_code == 507, response.text
        error = response.json()["error"]
        assert error["code"] == "insufficient_storage"
        assert "Недостаточно места для загрузки" in error["message"]
        assert client.app.state.dataset_service.quota_manager.snapshot().get("local", 0) == 0
        assert client.get("/api/datasets").json() == []
    data_dir = Path(settings.datasets_data_dir)
    assert not data_dir.exists() or not any(data_dir.iterdir())
