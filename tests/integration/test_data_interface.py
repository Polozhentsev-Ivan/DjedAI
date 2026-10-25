"""B1-10 parity: the Python data facade and the HTTP API share IDs, catalogs and errors.

Both paths run against one live app: Python calls go to ``app.state.dataset_service``
on the app's event loop, HTTP calls use a real Bearer session. Live PostgreSQL cases
additionally use DJEDAI_TEST_PG_DSN and the seeded demo tables.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from djedai.backend.app import create_app
from djedai.backend.auth import hash_password
from djedai.backend.config import Settings
from djedai.backend.db.models import DatasetModel, UserModel
from djedai.backend.db.session import create_db_engine
from djedai.backend.local_user import bootstrap_local_user
from djedai.data.service import CsvSource

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "test-interface-password"
DSN = os.environ.get("DJEDAI_TEST_PG_DSN")
CSV = b"order_id,amount,ordered_at\n001,10.5,2026-01-02\n002,20,2026-01-03\n"
# Per-call metadata; everything else in the catalog must not depend on the entry point.
VOLATILE = {"dataset_id", "created_at"}
# Valid DSN syntax for validation paths; nothing connects to it.
DSN_SHAPE = "postgresql://reader@db.invalid/shop"


@pytest.fixture
def configured(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    factory = sessionmaker(bind=engine)
    password_hash = hash_password(PASSWORD)
    for user_id in ("local", "other"):
        bootstrap_local_user(factory, user_id, username=user_id)
    with factory() as db:
        for user_id in ("local", "other"):
            db.get(UserModel, user_id).password_hash = password_hash
        db.commit()
    settings = Settings(
        _env_file=None,
        cp1_local_mode=False,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
        source_encryption_key=Fernet.generate_key().decode(),
    )
    yield settings, factory
    engine.dispose()


def _bearer(client: TestClient, username: str) -> dict[str, str]:
    login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        client.headers.update(_bearer(client, "local"))
        yield client


def _wait_ready(client: TestClient, dataset_id: str) -> None:
    # The SSE stream ends with exactly one terminal event.
    events = client.get(f"/api/datasets/{dataset_id}/events")
    assert "event: done\n" in events.text, events.text


def _terminal(client: TestClient, dataset_id: str) -> tuple[str, dict]:
    frames = client.get(f"/api/datasets/{dataset_id}/events").text.strip().split("\n\n")
    event, data = frames[-1].split("\n")
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


def _same_catalog(python: dict, http: dict) -> None:
    assert {key: value for key, value in python.items() if key not in VOLATILE} == {
        key: value for key, value in http.items() if key not in VOLATILE
    }


def _python_error(call) -> int:
    with pytest.raises(HTTPException) as error:
        call()
    return error.value.status_code


def test_csv_python_and_http_entry_points_share_ids_catalogs_and_errors(
    configured, tmp_path: Path
) -> None:
    settings, factory = configured
    source = tmp_path / "orders.csv"
    source.write_bytes(CSV)
    with _client(settings) as client:
        service = client.app.state.dataset_service
        http = client.post("/api/datasets/upload", files={"files": ("orders.csv", CSV)})
        assert http.status_code == 202
        assert http.json()["status"] == "processing"
        accepted = client.portal.call(
            service.ingest_csv, "local", [CsvSource(source, "orders.csv")]
        )
        assert accepted.status == "processing"
        http_id, python_id = http.json()["dataset_id"], accepted.dataset_id
        assert http_id != python_id
        _wait_ready(client, http_id)
        _wait_ready(client, python_id)

        # The dataset created in Python is the same resource over HTTP, and vice versa.
        for dataset_id in (http_id, python_id):
            catalog = service.get_catalog("local", dataset_id).model_dump(mode="json")
            assert catalog["dataset_id"] == dataset_id
            assert client.get(f"/api/datasets/{dataset_id}/catalog").json() == catalog
            detail = client.get(f"/api/datasets/{dataset_id}").json()
            assert detail == service.get_detail("local", dataset_id).model_dump(mode="json")
            assert detail["status"] == "ready"
        _same_catalog(
            service.get_catalog("local", python_id).model_dump(mode="json"),
            client.get(f"/api/datasets/{http_id}/catalog").json(),
        )
        listed = {item["dataset_id"] for item in client.get("/api/datasets").json()}
        assert listed == {http_id, python_id}

        # Owner context: another user sees neither dataset on either path.
        other = _bearer(client, "other")
        for dataset_id in (http_id, python_id):
            assert _python_error(partial(service.get_catalog, "other", dataset_id)) == 404
            response = client.get(f"/api/datasets/{dataset_id}/catalog", headers=other)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
        assert client.get("/api/datasets", headers=other).json() == []

        # A dataset that is still processing has no catalog yet.
        with factory() as db:
            db.add(
                DatasetModel(
                    dataset_id="queued",
                    owner_id="local",
                    name="queued.csv",
                    source_kind="csv_upload",
                    status="processing",
                )
            )
            db.commit()
        assert _python_error(partial(service.get_catalog, "local", "queued")) == 409
        response = client.get("/api/datasets/queued/catalog")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

        # Rejected input never creates a dataset.
        with pytest.raises(HTTPException) as error:
            client.portal.call(service.ingest_csv, "local", [CsvSource(source, "orders.txt")])
        assert error.value.status_code == 422
        response = client.post("/api/datasets/upload", files={"files": ("orders.txt", CSV)})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "http_422"
        assert _python_error(lambda: client.portal.call(service.ingest_csv, "local", [])) == 422
        assert _python_error(lambda: client.portal.call(service.connect_db, "local", "")) == 422
        response = client.post(
            "/api/datasets/connect", json={"kind": "postgres", "dsn": DSN_SHAPE, "tables": []}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "http_422"

        # HTTP schema validation rejects a request before the facade, with its own code.
        for response in (
            client.post("/api/datasets/upload"),
            client.post("/api/datasets/connect", json={"kind": "postgres", "dsn": ""}),
            client.post("/api/datasets/connect", json={"kind": "mysql", "dsn": DSN_SHAPE}),
        ):
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_request"

        # ClickHouse (#61) validates its DSN like PostgreSQL; other kinds stay unimplemented.
        with pytest.raises(HTTPException) as error:
            client.portal.call(
                partial(service.connect_db, "local", "clickhouse://ro@ch/db", kind="clickhouse")
            )
        assert error.value.status_code == 422
        response = client.post(
            "/api/datasets/connect", json={"kind": "clickhouse", "dsn": "clickhouse://ro@ch/db"}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "http_422"
        assert "clickhouse://" not in response.text
        mysql = partial(service.connect_db, "local", "mysql://ro:pw@db/x", kind="mysql")
        assert _python_error(lambda: client.portal.call(mysql)) == 501
        listed = {item["dataset_id"] for item in client.get("/api/datasets").json()}
        assert listed == {http_id, python_id, "queued"}


def test_processing_error_code_is_in_detail_while_sse_carries_only_the_message(
    configured, tmp_path: Path
) -> None:
    settings, _ = configured
    empty = tmp_path / "bad.csv"
    empty.write_bytes(b"")
    with _client(settings) as client:
        service = client.app.state.dataset_service
        http_id = client.post("/api/datasets/upload", files={"files": ("bad.csv", b"")}).json()[
            "dataset_id"
        ]
        python_id = client.portal.call(
            service.ingest_csv, "local", [CsvSource(empty, "bad.csv")]
        ).dataset_id
        for dataset_id in (http_id, python_id):
            event, data = _terminal(client, dataset_id)
            detail = client.get(f"/api/datasets/{dataset_id}").json()
            assert detail == service.get_detail("local", dataset_id).model_dump(mode="json")
            assert detail["status"] == "error"
            assert detail["error"]["code"] == "dataset_processing_failed"
            # The stream has no processing error code; clients read it from the detail.
            assert event == "error"
            assert data == {"message": detail["error"]["message"], "detail": None, "code": None}


@pytest.mark.skipif(not DSN, reason="DJEDAI_TEST_PG_DSN is not configured")
def test_postgres_python_and_http_entry_points_share_ids_catalogs_and_errors(
    configured,
) -> None:
    settings, _ = configured
    with _client(settings) as client:
        service = client.app.state.dataset_service
        http = client.post(
            "/api/datasets/connect", json={"kind": "postgres", "dsn": DSN, "tables": ["stores"]}
        )
        assert http.status_code == 202
        assert DSN not in http.text
        accepted = client.portal.call(service.connect_db, "local", DSN, ["stores"])
        http_id, python_id = http.json()["dataset_id"], accepted.dataset_id
        _wait_ready(client, http_id)
        _wait_ready(client, python_id)

        for dataset_id in (http_id, python_id):
            catalog = service.get_catalog("local", dataset_id).model_dump(mode="json")
            assert catalog["source"]["kind"] == "postgres"
            assert client.get(f"/api/datasets/{dataset_id}/catalog").json() == catalog
            assert DSN not in client.get(f"/api/datasets/{dataset_id}").text
        _same_catalog(
            service.get_catalog("local", python_id).model_dump(mode="json"),
            client.get(f"/api/datasets/{http_id}/catalog").json(),
        )

        other = _bearer(client, "other")
        for dataset_id in (http_id, python_id):
            assert _python_error(partial(service.get_context, "other", dataset_id)) == 404
            response = client.get(f"/api/datasets/{dataset_id}", headers=other)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"

        with pytest.raises(HTTPException) as error:
            client.portal.call(service.connect_db, "local", "not a dsn")
        assert error.value.status_code == 422
        response = client.post(
            "/api/datasets/connect", json={"kind": "postgres", "dsn": "not a dsn"}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "http_422"
