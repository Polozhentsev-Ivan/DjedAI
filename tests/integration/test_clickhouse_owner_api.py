"""Real auth/HTTP/runtime ClickHouse isolation with two independent sources (#61, #103).

Two fake servers with different store counts always run. With DJEDAI_TEST_CH_URL and
DJEDAI_TEST_CH_OTHER_URL (prepared by scripts/test_clickhouse_isolation.py) the same
scenario repeats on a real ClickHouse. Only model responses are controlled.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from djedai.agent.client import ModelResponse
from djedai.backend import chat_service
from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.data.clickhouse import parse_dsn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from fake_clickhouse import FakeClickHouse  # noqa: E402

FIRST_URL = os.environ.get("DJEDAI_TEST_CH_URL")
OTHER_URL = os.environ.get("DJEDAI_TEST_CH_OTHER_URL")
pytestmark = pytest.mark.integration
SQL = {"value": "SELECT COUNT(*) AS n FROM stores"}


class CountModel:
    """A deterministic test plan; no language model server or weights are used."""

    def generate_typed_response(self, system_prompt, user_prompt, response_model):
        if response_model.__name__ == "Understanding":
            value = {"intent": "data_question", "resolved_question": user_prompt}
        elif response_model.__name__ == "PlanResponse":
            value = {"plan": [{"tool": "run_sql", "args": {"sql": SQL["value"]}}]}
        elif response_model.__name__ == "ComposeResponse":
            value = {"answer_blocks": [{"type": "text", "markdown": "Расчёт завершён."}]}
        elif response_model.__name__ == "VerifyResponse":
            value = {"is_valid": True, "issues": [], "corrected_markdown": "Расчёт завершён."}
        else:
            raise AssertionError("Unexpected model response contract")
        return ModelResponse(status="success", data=response_model.model_validate(value))


@pytest.fixture(
    params=[
        "fake",
        pytest.param(
            "live",
            marks=pytest.mark.skipif(
                not FIRST_URL or not OTHER_URL,
                reason="Use scripts/test_clickhouse_isolation.py to prepare two live sources",
            ),
        ),
    ]
)
def sources(request):
    if request.param == "live":
        yield FIRST_URL, OTHER_URL
        return
    with ExitStack() as stack:
        first = stack.enter_context(FakeClickHouse().load_demo())
        other = stack.enter_context(FakeClickHouse(password="other-secret").load_demo(stores=3))
        yield first.url, other.url


@pytest.fixture
def configured(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", url)
    command.upgrade(migration, "head")
    monkeypatch.setattr(chat_service, "get_agent_client", CountModel)
    monkeypatch.setitem(SQL, "value", "SELECT COUNT(*) AS n FROM stores")
    return Settings(
        _env_file=None,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
        results_data_dir=str(tmp_path / "results"),
        source_encryption_key=os.environ.get("SOURCE_ENCRYPTION_KEY")
        or Fernet.generate_key().decode(),
        cp1_local_mode=False,
        auth_rate_limit_requests=20,
    )


def account(client, username):
    credentials = {"username": username, "password": "test-only-account-password"}
    created = client.post("/api/auth/register", json=credentials)
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json=credentials)
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def connect(client, headers, dsn):
    accepted = client.post(
        "/api/datasets/connect",
        headers=headers,
        json={"kind": "clickhouse", "dsn": dsn, "tables": ["stores", "sales"]},
    )
    assert accepted.status_code == 202, accepted.text
    dataset_id = accepted.json()["dataset_id"]
    events = client.get(f"/api/datasets/{dataset_id}/events", headers=headers)
    assert events.status_code == 200
    assert "event: done\n" in events.text, events.text
    catalog = client.get(f"/api/datasets/{dataset_id}/catalog", headers=headers)
    assert catalog.status_code == 200, catalog.text
    return dataset_id, catalog.json()


def ask(client, headers, dataset_id):
    created = client.post("/api/chats", headers=headers, json={"dataset_id": dataset_id})
    assert created.status_code == 201, created.text
    chat_id = created.json()["chat_id"]
    stream = client.post(
        f"/api/chats/{chat_id}/messages", headers=headers, json={"text": "Сколько магазинов?"}
    )
    assert stream.status_code == 200, stream.text
    assert "event: done\n" in stream.text
    results = []
    for frame in stream.text.split("\n\n"):
        if frame.startswith("event: tool_result\n"):
            results.append(json.loads(frame.split("data: ", 1)[1]))
    assert results
    return chat_id, results


def rows(client, headers, results):
    assert len(results) == 1 and results[0]["ok"], results
    result = results[0]
    data = client.get(f"/api/results/{result['result_id']}/data", headers=headers)
    assert data.status_code == 200, data.text
    return data.json()["rows"]


def test_two_owners_connect_query_and_never_see_each_other(configured, sources) -> None:
    first_url, other_url = sources
    with TestClient(create_app(configured)) as client:
        alice, bob = account(client, "alice"), account(client, "bob")
        first_id, first_catalog = connect(client, alice, first_url)
        other_id, other_catalog = connect(client, bob, other_url)
        stores = {t["table_id"]: t["row_count"] for t in first_catalog["tables"]}["stores"]
        other_stores = {t["table_id"]: t["row_count"] for t in other_catalog["tables"]}["stores"]
        assert stores > other_stores >= 1
        assert first_catalog["source"] == other_catalog["source"] == {"kind": "clickhouse"}

        for headers, own, foreign in ((alice, first_id, other_id), (bob, other_id, first_id)):
            listed = client.get("/api/datasets", headers=headers).json()
            assert [item["dataset_id"] for item in listed] == [own]
            for path in ("", "/catalog", "/events"):
                assert (
                    client.get(f"/api/datasets/{foreign}{path}", headers=headers).status_code == 404
                )
            denied = client.post("/api/chats", headers=headers, json={"dataset_id": foreign})
            assert denied.status_code == 404
        secrets = (
            first_url,
            other_url,
            parse_dsn(first_url).password,
            parse_dsn(other_url).password,
        )
        exposed = json.dumps([first_catalog, other_catalog]) + json.dumps(
            [client.get("/api/datasets", headers=h).json() for h in (alice, bob)]
        )
        assert not any(secret in exposed for secret in secrets)

        first_chat, first_result = ask(client, alice, first_id)
        other_chat, other_result = ask(client, bob, other_id)
        assert rows(client, alice, first_result) == [[stores]]
        assert rows(client, bob, other_result) == [[other_stores]]
        for headers, foreign_chat, foreign_result in (
            (alice, other_chat, other_result),
            (bob, first_chat, first_result),
        ):
            assert client.get(f"/api/chats/{foreign_chat}", headers=headers).status_code == 404
            for path in ("", "/data"):
                url = f"/api/results/{foreign_result[0]['result_id']}{path}"
                assert client.get(url, headers=headers).status_code == 404

        # SQL агента не выходит за каталог своего датасета: ни «сырые» копии, ни файлы,
        # ни табличные функции не дают прочитать данные другого владельца.
        foreign_file = client.app.state.dataset_service.data_dir / first_id / "dataset.duckdb"
        for forbidden in (
            "SELECT COUNT(*) AS n FROM ch_djedai_demo_stores",
            f"SELECT COUNT(*) AS n FROM read_parquet('{foreign_file}')",
            f"SELECT COUNT(*) AS n FROM '{foreign_file}'.stores",
            "SELECT COUNT(*) AS n FROM url('http://127.0.0.1/', 'CSV')",
        ):
            SQL["value"] = forbidden
            assert not any(result["ok"] for result in ask(client, bob, other_id)[1]), forbidden

        assert client.get("/api/datasets").status_code == 401
        anonymous = client.post(
            "/api/datasets/connect", json={"kind": "clickhouse", "dsn": first_url}
        )
        assert anonymous.status_code == 401

    SQL["value"] = "SELECT COUNT(*) AS n FROM stores"
    with TestClient(create_app(configured)) as restarted:
        assert (
            restarted.get(f"/api/datasets/{first_id}/catalog", headers=alice).json()
            == first_catalog
        )
        assert rows(restarted, bob, ask(restarted, bob, other_id)[1]) == [[other_stores]]
        assert restarted.get(f"/api/datasets/{first_id}", headers=bob).status_code == 404
        assert restarted.post("/api/auth/logout", headers=alice).status_code == 204
        assert restarted.get(f"/api/datasets/{first_id}", headers=alice).status_code == 401
