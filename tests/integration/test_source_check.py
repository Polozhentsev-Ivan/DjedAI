"""#84: POST /api/datasets/connect/check lists tables of a source before connecting.

The fake ClickHouse server always runs. DJEDAI_TEST_PG_DSN / DJEDAI_TEST_CH_URL add real
servers (scripts/test_postgres_isolation.py and scripts/test_clickhouse_isolation.py).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.data.clickhouse import parse_dsn as parse_clickhouse_dsn
from djedai.data.postgres import parse_dsn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from fake_clickhouse import FakeClickHouse  # noqa: E402

pytestmark = pytest.mark.integration
PG_DSN = os.environ.get("DJEDAI_TEST_PG_DSN")
CH_URL = os.environ.get("DJEDAI_TEST_CH_URL")
DEMO_TABLES = ["campaigns", "products", "sales", "stores"]


def settings(tmp_path: Path, *, key: bool = True) -> Settings:
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", url)
    command.upgrade(migration, "head")
    return Settings(
        _env_file=None,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
        results_data_dir=str(tmp_path / "results"),
        source_encryption_key=Fernet.generate_key().decode() if key else None,
        cp1_local_mode=False,
        auth_rate_limit_requests=20,
    )


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path))) as client:
        credentials = {"username": "alice", "password": "test-only-account-password"}
        assert client.post("/api/auth/register", json=credentials).status_code == 201
        token = client.post("/api/auth/login", json=credentials).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def check(client, kind: str, dsn: str):
    return client.post("/api/datasets/connect/check", json={"kind": kind, "dsn": dsn})


def test_clickhouse_tables_are_listed_without_storing_or_echoing_the_source(client) -> None:
    with FakeClickHouse().load_demo() as fake:
        response = check(client, "clickhouse", fake.url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "clickhouse" and body["truncated"] is False
    assert body["source"].startswith("djedai_reader@127.0.0.1:")
    assert [table["name"] for table in body["tables"]] == DEMO_TABLES
    assert {t["name"]: t["row_count"] for t in body["tables"]}["sales"] == 4376
    assert fake.password not in response.text and "reader-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/datasets").json() == []
    assert not any("FORMAT Parquet" in call["sql"] for call in fake.calls)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda url: url.replace("reader-secret%21", "wrong"), "Неверный пользователь или пароль"),
        (lambda url: url.rsplit(":", 1)[0] + ":1/djedai_demo", "Сервер базы данных недоступен"),
    ],
)
def test_clickhouse_failures_are_safe_categories(client, change, message) -> None:
    with FakeClickHouse().load_demo() as fake:
        response = check(client, "clickhouse", change(fake.url) + "?connect_timeout=1")
    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith(message)
    assert "wrong" not in response.text and "127.0.0.1" not in response.text


def test_writable_clickhouse_user_is_rejected(client) -> None:
    with FakeClickHouse(readonly="0").load_demo() as fake:
        fake.honour_readonly = False
        response = check(client, "clickhouse", fake.url)
    assert response.status_code == 422
    assert "только для чтения" in response.json()["error"]["message"]


def test_unreachable_postgres_and_bad_dsn(client) -> None:
    dsn = "postgresql://private-user:private-password@127.0.0.1:1/private-db?connect_timeout=1"
    response = check(client, "postgres", dsn)
    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("Сервер базы данных недоступен")
    for secret in ("private-user", "private-password", "private-db"):
        assert secret not in response.text
    for kind, bad in (("postgres", "password=x"), ("clickhouse", "clickhouse://ro@ch/db")):
        response = check(client, kind, bad)
        assert response.status_code == 422
        assert "Проверьте строку подключения" in response.json()["error"]["message"]
        assert bad not in response.text


def test_check_requires_session_and_encryption_key(client, tmp_path: Path) -> None:
    anonymous = client.post(
        "/api/datasets/connect/check",
        json={"kind": "clickhouse", "dsn": "http://u:p@h/db"},
        headers={"Authorization": ""},
    )
    assert anonymous.status_code == 401
    with TestClient(create_app(settings(tmp_path / "no-key", key=False))) as other:
        credentials = {"username": "bob", "password": "test-only-account-password"}
        assert other.post("/api/auth/register", json=credentials).status_code == 201
        token = other.post("/api/auth/login", json=credentials).json()["access_token"]
        other.headers["Authorization"] = f"Bearer {token}"
        assert check(other, "clickhouse", "http://u:p@h/db").status_code == 503


@pytest.mark.skipif(not PG_DSN, reason="DJEDAI_TEST_PG_DSN is not configured")
def test_live_postgres_tables(client) -> None:
    response = check(client, "postgres", PG_DSN)
    assert response.status_code == 200, response.text
    names = [table["name"] for table in response.json()["tables"]]
    assert set(DEMO_TABLES) <= set(names)
    assert parse_dsn(PG_DSN).password not in response.text
    # Имена из проверки принимает подключение
    accepted = client.post(
        "/api/datasets/connect", json={"kind": "postgres", "dsn": PG_DSN, "tables": names[:1]}
    )
    assert accepted.status_code == 202
    events = client.get(f"/api/datasets/{accepted.json()['dataset_id']}/events")
    assert "event: done\n" in events.text


@pytest.mark.skipif(not CH_URL, reason="DJEDAI_TEST_CH_URL is not configured")
def test_live_clickhouse_tables(client) -> None:
    response = check(client, "clickhouse", CH_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [table["name"] for table in body["tables"]] == DEMO_TABLES
    assert {t["name"]: t["row_count"] for t in body["tables"]}["sales"] == 4376
    assert parse_clickhouse_dsn(CH_URL).password not in json.dumps(body)
