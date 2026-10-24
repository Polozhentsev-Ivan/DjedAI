"""Real auth/HTTP/runtime PostgreSQL isolation with two independent live sources.

Run through scripts/test_postgres_isolation.py for reproducible databases and keys.
Only model responses are controlled; HTTP auth, graph, tools, persistence and live
PostgreSQL connections use the production implementation.
"""

from __future__ import annotations

import json
import os
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
from djedai.data.postgres import parse_dsn

FIRST_DSN = os.environ.get("DJEDAI_TEST_PG_DSN")
OTHER_DSN = os.environ.get("DJEDAI_TEST_PG_OTHER_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not FIRST_DSN or not OTHER_DSN,
        reason="Use scripts/test_postgres_isolation.py to prepare two live PostgreSQL sources",
    ),
]
ROOT = Path(__file__).resolve().parents[2]


class CountModel:
    """A deterministic test plan; no language model server or weights are used."""

    def generate_typed_response(self, system_prompt, user_prompt, response_model):
        if response_model.__name__ == "Understanding":
            value = {"intent": "data_question", "resolved_question": user_prompt}
        elif response_model.__name__ == "PlanResponse":
            value = {
                "plan": [{"tool": "run_sql", "args": {"sql": "SELECT COUNT(*) AS n FROM stores"}}]
            }
        elif response_model.__name__ == "ComposeResponse":
            value = {"answer_blocks": [{"type": "text", "markdown": "Расчёт завершён."}]}
        elif response_model.__name__ == "VerifyResponse":
            value = {"is_valid": True, "issues": [], "corrected_markdown": "Расчёт завершён."}
        else:
            raise AssertionError("Unexpected model response contract")
        return ModelResponse(status="success", data=response_model.model_validate(value))


@pytest.fixture
def configured(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", url)
    command.upgrade(migration, "head")
    monkeypatch.setattr(chat_service, "get_agent_client", CountModel)
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
        json={"kind": "postgres", "dsn": dsn, "tables": ["stores"]},
    )
    assert accepted.status_code == 202, accepted.text
    dataset_id = accepted.json()["dataset_id"]
    events = client.get(f"/api/datasets/{dataset_id}/events", headers=headers)
    assert events.status_code == 200
    assert "event: done\n" in events.text
    assert "event: error\n" not in events.text
    catalog = client.get(f"/api/datasets/{dataset_id}/catalog", headers=headers)
    assert catalog.status_code == 200, catalog.text
    return dataset_id, catalog.json()


def ask_count(client, headers, dataset_id):
    created = client.post("/api/chats", headers=headers, json={"dataset_id": dataset_id})
    assert created.status_code == 201, created.text
    chat_id = created.json()["chat_id"]
    stream = client.post(
        f"/api/chats/{chat_id}/messages", headers=headers, json={"text": "Сколько магазинов?"}
    )
    assert stream.status_code == 200, stream.text
    assert "event: done\n" in stream.text
    assert "event: error\n" not in stream.text
    result_ids = []
    for frame in stream.text.split("\n\n"):
        if frame.startswith("event: tool_result\n"):
            data = json.loads(frame.split("data: ", 1)[1])
            assert data["ok"]
            result_ids.append(data["result_id"])
    assert len(result_ids) == 1
    result = client.get(f"/api/results/{result_ids[0]}/data", headers=headers)
    assert result.status_code == 200, result.text
    return chat_id, result_ids[0], result.json()["rows"]


def test_auth_connect_two_sources_chat_and_results_stay_owned_after_restart(configured) -> None:
    with TestClient(create_app(configured)) as client:
        alice, bob = account(client, "alice"), account(client, "bob")
        first_id, first_catalog = connect(client, alice, FIRST_DSN)
        other_id, other_catalog = connect(client, bob, OTHER_DSN)
        first_count = first_catalog["tables"][0]["row_count"]
        other_count = other_catalog["tables"][0]["row_count"]
        assert first_count > other_count >= 1
        assert first_catalog["source"] == other_catalog["source"] == {"kind": "postgres"}
        for headers, own, foreign in ((alice, first_id, other_id), (bob, other_id, first_id)):
            assert [
                item["dataset_id"] for item in client.get("/api/datasets", headers=headers).json()
            ] == [own]
            for path in ("", "/catalog", "/events"):
                denied = client.get(f"/api/datasets/{foreign}{path}", headers=headers)
                assert denied.status_code == 404
            denied = client.post("/api/chats", headers=headers, json={"dataset_id": foreign})
            assert denied.status_code == 404
        for value in (FIRST_DSN, OTHER_DSN, parse_dsn(FIRST_DSN).password):
            assert value not in json.dumps([first_catalog, other_catalog])
        first_chat, first_result, first_rows = ask_count(client, alice, first_id)
        other_chat, other_result, other_rows = ask_count(client, bob, other_id)
        assert first_rows == [[first_count]]
        assert other_rows == [[other_count]]
        for headers, foreign_chat, foreign_result in (
            (alice, other_chat, other_result),
            (bob, first_chat, first_result),
        ):
            assert client.get(f"/api/chats/{foreign_chat}", headers=headers).status_code == 404
            assert client.get(f"/api/results/{foreign_result}", headers=headers).status_code == 404
            assert (
                client.get(f"/api/results/{foreign_result}/data", headers=headers).status_code
                == 404
            )
        assert client.get("/api/datasets").status_code == 401
        assert (
            client.post(
                "/api/datasets/connect", json={"kind": "postgres", "dsn": FIRST_DSN}
            ).status_code
            == 401
        )

    with TestClient(create_app(configured)) as restarted:
        assert (
            restarted.get(f"/api/datasets/{first_id}/catalog", headers=alice).json()
            == first_catalog
        )
        assert (
            restarted.get(f"/api/datasets/{other_id}/catalog", headers=bob).json() == other_catalog
        )
        assert ask_count(restarted, alice, first_id)[2] == [[first_count]]
        assert ask_count(restarted, bob, other_id)[2] == [[other_count]]
        assert restarted.get(f"/api/datasets/{first_id}", headers=bob).status_code == 404
        assert restarted.post("/api/auth/logout", headers=alice).status_code == 204
        assert restarted.get(f"/api/datasets/{first_id}", headers=alice).status_code == 401


def test_bad_external_source_has_safe_terminal_http_and_sse_errors(configured, caplog) -> None:
    dsn = "postgresql://private-user:private-password@127.0.0.1:1/private-db?connect_timeout=1"
    with TestClient(create_app(configured)) as client:
        owner = account(client, "alice")
        accepted = client.post(
            "/api/datasets/connect",
            headers=owner,
            json={"kind": "postgres", "dsn": dsn, "tables": ["stores"]},
        )
        assert accepted.status_code == 202, accepted.text
        dataset_id = accepted.json()["dataset_id"]
        events = client.get(f"/api/datasets/{dataset_id}/events", headers=owner)
        assert events.status_code == 200
        assert "event: error\n" in events.text
        assert "event: done\n" not in events.text
        detail = client.get(f"/api/datasets/{dataset_id}", headers=owner)
        assert detail.status_code == 200
        assert detail.json()["status"] == "error"
        assert detail.json()["catalog"] is None
        assert client.get(f"/api/datasets/{dataset_id}/catalog", headers=owner).status_code == 409
        for secret in (dsn, "private-user", "private-password", "private-db"):
            assert secret not in accepted.text + events.text + detail.text + caplog.text
    with TestClient(create_app(configured)) as restarted:
        assert restarted.get(f"/api/datasets/{dataset_id}", headers=owner).json() == detail.json()
