"""CP1 runtime binding, result persistence, access and analytical connection lifetime.

Only model responses are controlled. HTTP dependencies, the real graph, dataset
catalog loading, registry dispatch, SQLite and Parquet all remain in production.
"""

from __future__ import annotations

import json
import re
import runpy
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import Request
from fastapi.testclient import TestClient
from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from djedai.agent.client import BIModelClient, ModelResponse
from djedai.agent.prompts import load_prompt
from djedai.backend import chat_service
from djedai.backend.app import create_app
from djedai.backend.auth import hash_password
from djedai.backend.config import Settings, get_settings
from djedai.backend.db.models import ChatModel, DatasetModel, ToolResultModel, UserModel
from djedai.backend.db.session import create_db_engine
from djedai.backend.local_user import bootstrap_local_user
from djedai.contracts.agent import ComposeResponse, VerifyResponse
from djedai.contracts.tool_result import ToolResultData

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "test-runtime-password"


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


class ControlledModel:
    """Produce a typed plan from the catalog supplied by the real runtime."""

    def __init__(self, *, sql: str | None = None, fail: bool = False) -> None:
        self.sql = sql
        self.fail = fail
        self.calls = []

    def generate_typed_response(self, system_prompt, user_prompt, response_model, **kwargs):
        self.calls.append((response_model.__name__, system_prompt, user_prompt))
        if self.fail:
            raise RuntimeError("Controlled model transport failure")
        if response_model.__name__ == "Understanding":
            value = {"intent": "data_question", "resolved_question": user_prompt}
        elif response_model.__name__ == "PlanResponse":
            table = re.search(r"^Таблица (\w+)", system_prompt, re.MULTILINE).group(1)
            sql = self.sql or f'SELECT COUNT(*) AS n, SUM(amount) AS total FROM "{table}"'
            value = {"plan": [{"tool": "run_sql", "args": {"sql": sql}}]}
        elif response_model.__name__ == "ComposeResponse":
            value = {"answer_blocks": [{"type": "text", "markdown": "Расчёт завершён."}]}
        elif response_model.__name__ == "VerifyResponse":
            value = {
                "is_valid": True,
                "issues": [],
                "corrected_markdown": "Расчёт завершён.",
            }
        else:
            raise AssertionError(f"Unexpected response contract: {response_model}")
        return ModelResponse(status="success", data=response_model.model_validate(value))


@pytest.mark.parametrize("question", ["Сколько строк и какова сумма?", "Ответь COUNT=...;SUM=..."])
def test_browser_model_supports_successful_numeric_verification(question: str) -> None:
    """The HTTP fixture must verify successfully, rather than rely on graph fail-open."""
    model_app = runpy.run_path(str(ROOT / "tests/e2e/serve_cp1.py"))["model_app"]
    facts = json.dumps(
        [
            {
                "tool": "run_sql",
                "data": ToolResultData(
                    columns=["n", "total"], rows=[[2, 30]], row_count=1
                ).model_dump(mode="json"),
            }
        ]
    )
    requested_schemas = []
    with TestClient(model_app(), raise_server_exceptions=False) as server:

        def complete(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            requested_schemas.append(payload["response_format"]["json_schema"]["name"])
            response = server.post("/v1/chat/completions", json=payload)
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"content-type": response.headers["content-type"]},
                request=request,
            )

        model = BIModelClient(
            api_key="cp1-test-only",
            base_url="http://cp1-model.test/v1",
            model_name="cp1-controlled-model",
        )
        model.client.close()
        model.client = OpenAI(
            api_key="cp1-test-only",
            base_url="http://cp1-model.test/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(complete)),
        )
        try:
            composed = model.generate_typed_response(
                load_prompt("compose").format(catalog="", facts=facts), question, ComposeResponse
            )
            assert composed.status == "success" and composed.data is not None
            draft = composed.data.answer_blocks[0].markdown
            assert draft
            verified = model.generate_typed_response(
                load_prompt("verify").format(catalog="", facts=facts, draft=draft),
                "Verify the numbers and formats.",
                VerifyResponse,
            )
            assert verified.status == "success" and verified.data is not None
            assert verified.data.is_valid and verified.data.issues == []
            assert verified.data.corrected_markdown == draft
            assert requested_schemas == ["ComposeResponse", "VerifyResponse"]
        finally:
            model.client.close()


@pytest.fixture
def configured(tmp_path: Path, monkeypatch, account_password_hash: str):
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", url)
    command.upgrade(migration, "head")
    engine = create_db_engine(url)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "runtime-local", username="local")
    bootstrap_local_user(factory, "runtime-other", username="runtime-other")
    with factory() as db:
        for user_id in ("runtime-local", "runtime-other"):
            db.get(UserModel, user_id).password_hash = account_password_hash
        db.commit()
    # Explicit app settings must win over process-global defaults for result writes/reads.
    monkeypatch.setenv("RESULTS_DATA_DIR", str(tmp_path / "wrong-global-results"))
    get_settings.cache_clear()
    settings = Settings(
        _env_file=None,
        cp1_local_mode=False,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
        RESULTS_DATA_DIR=str(tmp_path / "app-specific-results"),
    )
    yield settings, factory
    engine.dispose()
    get_settings.cache_clear()


def _model(monkeypatch, **kwargs):
    model = ControlledModel(**kwargs)
    monkeypatch.setattr(chat_service, "get_agent_client", lambda: model)
    return model


def _upload(client, name="sales", values=(10, 20)):
    content = "amount\n" + "".join(f"{value}\n" for value in values)
    response = client.post(
        "/api/datasets/upload", files={"files": (f"{name}.csv", content.encode(), "text/csv")}
    )
    assert response.status_code == 202
    dataset_id = response.json()["dataset_id"]
    assert "event: done\n" in client.get(f"/api/datasets/{dataset_id}/events").text
    response = client.post("/api/chats", json={"dataset_id": dataset_id})
    assert response.status_code == 201
    return dataset_id, response.json()["chat_id"]


def _events(text):
    parsed = []
    for frame in text.split("\n\n"):
        lines = frame.splitlines()
        if not lines:
            continue
        name = next(
            line.removeprefix("event:").strip() for line in lines if line.startswith("event:")
        )
        data = "\n".join(
            line.removeprefix("data:").lstrip() for line in lines if line.startswith("data:")
        )
        parsed.append((name, json.loads(data)))
    return parsed


def _post_turn(client, chat_id):
    response = client.post(
        f"/api/chats/{chat_id}/messages", json={"text": "Сколько строк и какова сумма amount?"}
    )
    assert response.status_code == 200
    return _events(response.text)


def _assert_database_released(path: Path, table="sales", expected=(2, 30)):
    # DuckDB rejects this if an incompatible read-only connection is still open.
    with duckdb.connect(str(path), read_only=False) as connection:
        assert (
            connection.execute(f'SELECT COUNT(*), SUM(amount) FROM "{table}"').fetchone()
            == expected
        )


def test_production_runtime_binds_each_dataset_and_persists_results_across_restart(
    configured, monkeypatch
) -> None:
    settings, factory = configured
    model = _model(monkeypatch)
    saved = []
    with _client(settings) as client:
        for name, values, expected in (
            ("alpha", (10, 20), [2, 30]),
            ("beta", (3, 5, 7, 11), [4, 26]),
        ):
            dataset_id, chat_id = _upload(client, name, values)
            events = _post_turn(client, chat_id)
            assert events[-1][0] == "done"
            assert "error" not in [event for event, _ in events]
            result_events = [data for event, data in events if event == "tool_result"]
            assert len(result_events) == 1
            assert result_events[0]["ok"] is True
            result_id = result_events[0]["result_id"]
            expected_sql = f'SELECT COUNT(*) AS n, SUM(amount) AS total FROM "{name}"'
            metadata = client.get(f"/api/results/{result_id}").json()
            assert (metadata["result_id"], metadata["status"], metadata["tool"]) == (
                result_id,
                "ok",
                "run_sql",
            )
            assert metadata["sql"] == expected_sql
            assert metadata["params"] == {"sql": expected_sql}
            assert metadata["data"] is None
            assert metadata["data_ref"] == f"parquet://{result_id}.parquet"
            data = client.get(f"/api/results/{result_id}/data").json()
            assert data["rows"] == [expected]
            assert data["columns"] == ["n", "total"]
            assert data["row_count"] == 1
            with factory() as db:
                record = db.get(ToolResultModel, result_id)
                assert (record.owner_id, record.chat_id) == ("runtime-local", chat_id)
            parquet = Path(settings.results_data_dir) / f"{result_id}.parquet"
            assert parquet.is_file()
            with duckdb.connect() as connection:
                assert connection.execute(
                    "SELECT * FROM read_parquet(?)", [str(parquet)]
                ).fetchall() == [tuple(expected)]
            history = client.get(f"/api/chats/{chat_id}").json()
            assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
            assert history["messages"][-1]["result_ids"] == [result_id]
            assert history["messages"][-1]["message_id"] == events[-1][1]["message_id"]
            context = client.app.state.dataset_service.get_context("runtime-local", dataset_id)
            _assert_database_released(context.duckdb_path, name, tuple(expected))
            saved.append((chat_id, result_id, metadata, data, history))
        assert not (Path(settings.results_data_dir).parent / "wrong-global-results").exists()
        plans = [system for schema, system, _ in model.calls if schema == "PlanResponse"]
        assert len(plans) == 2
        assert "Таблица alpha" in plans[0] and "Таблица beta" not in plans[0]
        assert "Таблица beta" in plans[1] and "Таблица alpha" not in plans[1]
        checks = [system for schema, system, _ in model.calls if schema == "VerifyResponse"]
        assert len(checks) == 2
        assert all("Drafted Response:\nРасчёт завершён." in system for system in checks)

    with _client(settings) as restarted:
        for chat_id, result_id, metadata, data, history in saved:
            assert restarted.get(f"/api/results/{result_id}").json() == metadata
            assert restarted.get(f"/api/results/{result_id}/data").json() == data
            assert restarted.get(f"/api/chats/{chat_id}").json() == history


def test_fast_path_count_and_structure_skip_model_and_survive_reload(
    configured, monkeypatch
) -> None:
    settings, _ = configured
    model = _model(monkeypatch)
    with _client(settings) as client:
        _, chat_id = _upload(client, "fast_sales", (10, 20, 30))
        count_events = _events(
            client.post(
                f"/api/chats/{chat_id}/messages",
                json={"text": "Сколько строк в таблице fast_sales?"},
            ).text
        )
        assert count_events[-1][0] == "done"
        assert "error" not in [event for event, _ in count_events]
        result_id = next(
            data["result_id"] for event, data in count_events if event == "tool_result"
        )
        blocks = [data for event, data in count_events if event == "block"]
        assert blocks[0] == {"type": "text", "markdown": "Строк в таблице `fast_sales`: 3."}
        assert blocks[1]["type"] == "text"
        assert "**Как посчитано.**" in blocks[1]["markdown"]
        assert model.calls == []

        metadata = client.get(f"/api/results/{result_id}").json()
        assert metadata["sql"] == 'SELECT COUNT(*) AS row_count FROM "fast_sales"'
        assert client.get(f"/api/results/{result_id}/data").json()["rows"] == [[3]]
        history = client.get(f"/api/chats/{chat_id}").json()["messages"]
        assert history[-1]["result_ids"] == [result_id]

        structure_events = _events(
            client.post(
                f"/api/chats/{chat_id}/messages",
                json={"text": "Покажи структуру таблицы fast_sales"},
            ).text
        )
        assert structure_events[-1][0] == "done"
        assert "tool_result" not in [event for event, _ in structure_events]
        structure_blocks = [data for event, data in structure_events if event == "block"]
        assert "**Структура таблицы `fast_sales`**" in structure_blocks[0]["markdown"]
        assert "`amount`" in structure_blocks[0]["markdown"]
        assert model.calls == []

        _, second_chat = _upload(client, "other_sales", (1, 2, 3, 4))
        other_events = _events(
            client.post(
                f"/api/chats/{second_chat}/messages",
                json={"text": "Сколько строк в таблице other_sales?"},
            ).text
        )
        assert other_events[-1][0] == "done"
        other_blocks = [data for event, data in other_events if event == "block"]
        assert other_blocks[0] == {
            "type": "text",
            "markdown": "Строк в таблице `other_sales`: 4.",
        }
        assert model.calls == []

    with _client(settings, username="runtime-other") as foreign:
        for suffix in ("", "/data"):
            assert foreign.get(f"/api/results/{result_id}{suffix}").status_code == 404
        assert foreign.get(f"/api/chats/{chat_id}").status_code == 404

    with _client(settings) as restarted:
        assert restarted.get(f"/api/results/{result_id}/data").json()["rows"] == [[3]]
        reloaded = restarted.get(f"/api/chats/{chat_id}").json()["messages"]
        assert reloaded[-3]["result_ids"] == [result_id]
        assert "**Структура таблицы `fast_sales`**" in reloaded[-1]["blocks"][0]["markdown"]


@pytest.mark.parametrize("failure", ["empty", "timeout", "timeout_after_rejection"])
def test_unverified_numbers_are_neither_published_nor_saved(
    configured, monkeypatch, failure
) -> None:
    settings, _ = configured

    class UnverifiedModel(ControlledModel):
        def __init__(self) -> None:
            super().__init__()
            self.compose_calls = 0
            self.verify_calls = 0

        def generate_typed_response(self, system_prompt, user_prompt, response_model, **kwargs):
            if failure == "empty" and response_model.__name__ == "PlanResponse":
                return ModelResponse(
                    status="success",
                    data=response_model.model_validate(
                        {
                            "plan": [
                                {
                                    "tool": "value_counts",
                                    "args": {
                                        "table": "sales",
                                        "column": "amount",
                                        "filter": "FALSE",
                                    },
                                }
                            ]
                        }
                    ),
                )
            if response_model is ComposeResponse:
                self.compose_calls += 1
                # Reach the model verifier before its explicit rejection; later drafts
                # exercise the deterministic unsupported-number retry as well.
                draft = (
                    "Количество строк: 2"
                    if failure == "timeout_after_rejection" and self.compose_calls == 1
                    else "Количество строк: 999"
                )
                return ModelResponse(
                    status="success",
                    data=ComposeResponse.model_validate(
                        {"answer_blocks": [{"type": "text", "markdown": draft}]}
                    ),
                )
            if response_model is VerifyResponse:
                self.verify_calls += 1
                if failure == "timeout_after_rejection" and self.verify_calls == 1:
                    return ModelResponse(
                        status="success",
                        data=VerifyResponse(
                            is_valid=False,
                            issues=["Не показана запрошенная сумма amount"],
                            corrected_markdown="Количество строк: 2",
                        ),
                    )
                return ModelResponse(status="error", error_type="TIMEOUT", message="Test timeout")
            return super().generate_typed_response(
                system_prompt, user_prompt, response_model, **kwargs
            )

    model = UnverifiedModel()
    monkeypatch.setattr(chat_service, "get_agent_client", lambda: model)
    with _client(settings) as client:
        _, chat_id = _upload(client)
        events = _post_turn(client, chat_id)
        names = [event for event, _ in events]
        blocks = [data for event, data in events if event == "block"]
        history = client.get(f"/api/chats/{chat_id}").json()["messages"]
        assert "999" not in json.dumps(blocks, ensure_ascii=False)
        assert "999" not in json.dumps(
            [message.get("blocks", []) for message in history], ensure_ascii=False
        )
        if failure == "empty":
            assert names[-1] == "done" and "error" not in names
            assert blocks == [{"type": "text", "markdown": "Нет данных для формирования ответа."}]
            assert [message["role"] for message in history] == ["user", "assistant"]
            assert history[-1]["blocks"] == blocks
            assert model.compose_calls == 1 and model.verify_calls == 0
        else:
            assert names[-1] == "error" and "done" not in names and "block" not in names
            assert [message["role"] for message in history] == ["user"]
            expected_calls = 2 if failure == "timeout_after_rejection" else 1
            assert model.verify_calls == expected_calls
            assert model.compose_calls == 2


def test_missing_foreign_and_unowned_resources_are_hidden(configured, monkeypatch) -> None:
    settings, factory = configured
    model = _model(monkeypatch)
    with factory() as db:
        db.add(
            DatasetModel(
                dataset_id="foreign-dataset",
                owner_id="runtime-other",
                name="Private",
                source_kind="csv_upload",
                status="ready",
            )
        )
        db.commit()
        db.add(
            ChatModel(
                chat_id="foreign-chat",
                owner_id="runtime-other",
                dataset_id="foreign-dataset",
                title="Private chat",
            )
        )
        db.add_all(
            [
                ToolResultModel(result_id="foreign-result", owner_id="runtime-other", status="ok"),
                ToolResultModel(result_id="unowned-result", owner_id=None, status="ok"),
            ]
        )
        db.commit()
    with _client(settings) as client:
        for result_id in ("missing-result", "foreign-result", "unowned-result"):
            for suffix in ("", "/data"):
                response = client.get(f"/api/results/{result_id}{suffix}")
                assert response.status_code == 404
                assert response.json()["error"]["code"] == "not_found"
        for chat_id in ("missing-chat", "foreign-chat"):
            assert client.get(f"/api/chats/{chat_id}").status_code == 404
            response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Private?"})
            assert response.status_code == 404
        for dataset_id in ("missing-dataset", "foreign-dataset"):
            assert client.get("/api/chats", params={"dataset_id": dataset_id}).status_code == 404
            assert client.post("/api/chats", json={"dataset_id": dataset_id}).status_code == 404
    assert model.calls == []


def test_missing_model_configuration_persists_failure_without_synthetic_answer(
    configured, monkeypatch
) -> None:
    settings, factory = configured
    for field in ("MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY"):
        monkeypatch.setenv(field, "")
    with _client(settings) as client:
        _, chat_id = _upload(client)
        response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Сколько строк?"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: error" in response.text and '"code":"model_unavailable"' in response.text
        assert "event: done" not in response.text
        saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
        assert len(saved) == 1 and saved[0]["role"] == "user"
        assert saved[0]["turn"]["code"] == "model_unavailable"
        with factory() as db:
            assert list(db.scalars(select(ToolResultModel))) == []


@pytest.mark.parametrize("ending", ["model_error", "generator_close", "cancel"])
def test_dataset_connection_closes_for_failed_and_interrupted_turns(
    configured, monkeypatch, ending
) -> None:
    settings, factory = configured
    _model(monkeypatch, fail=ending == "model_error")
    with _client(settings) as client:
        dataset_id, chat_id = _upload(client)
        context = client.app.state.dataset_service.get_context("runtime-local", dataset_id)
        runtime = chat_service.get_agent_runtime(Request({"type": "http", "app": client.app}))
        assert runtime.tool_dispatcher is runtime.catalog_loader is None
        cancelled = False
        with factory() as db:
            stream = chat_service.stream_chat_turn(
                db,
                chat_id,
                "Сколько строк и какова сумма amount?",
                runtime,
                is_cancelled=lambda: cancelled,
                owner_id="runtime-local",
            )
            if ending == "model_error":
                events = list(stream)
                assert events[-1].event == "error"
                assert "done" not in [event.event for event in events]
            else:
                assert next(stream).event == "status"
                if ending == "generator_close":
                    stream.close()
                else:
                    cancelled = True
                    assert list(stream) == []
        _assert_database_released(context.duckdb_path)
        assert all(
            message["role"] == "user"
            for message in client.get(f"/api/chats/{chat_id}").json()["messages"]
        )


@pytest.mark.parametrize("operation", ["write", "file", "network", "other_dataset"])
def test_bound_runtime_rejects_writes_external_reads_and_other_datasets(
    configured, monkeypatch, tmp_path: Path, operation
) -> None:
    settings, _ = configured
    secret = tmp_path / "private.csv"
    secret.write_text("secret\nprivate-value\n", encoding="utf-8")
    statements = {
        "write": "DROP TABLE sales",
        "file": f"SELECT * FROM read_csv('{secret.as_posix()}')",
        "network": "SELECT * FROM read_csv('http://127.0.0.1:9/private.csv')",
        "other_dataset": "SELECT * FROM only_other_dataset",
    }
    model = _model(monkeypatch, sql=statements[operation])
    with _client(settings) as client:
        _upload(client, "only_other_dataset", (99,))
        dataset_id, chat_id = _upload(client)
        events = _post_turn(client, chat_id)
        results = [data for event, data in events if event == "tool_result"]
        assert results and all(result["ok"] is False for result in results)
        for result in results:
            metadata = client.get(f"/api/results/{result['result_id']}").json()
            assert metadata["status"] == "error"
            assert metadata["params"] == {"sql": statements[operation]}
            assert metadata["data"] is metadata["data_ref"] is None
            if operation == "write":
                assert metadata["error"]["code"] == "forbidden_operation"
            else:
                assert metadata["error"]["code"] == "dataset_scope_violation"
        assert model.calls
        context = client.app.state.dataset_service.get_context("runtime-local", dataset_id)
        _assert_database_released(context.duckdb_path)
