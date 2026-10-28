"""Потери типизации в ответе, сохранённом результате и после перезапуска (B2-10)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.backend.app import create_app
from djedai.backend.auth import hash_password
from djedai.backend.chat_service import AgentRuntime, get_agent_runtime
from djedai.backend.config import Settings
from djedai.backend.db.models import DatasetModel, UserModel
from djedai.backend.db.session import create_db_engine
from djedai.backend.local_user import bootstrap_local_user
from djedai.contracts.agent import (
    Block,
    ComposeResponse,
    PlannedCall,
    PlanResponse,
    Understanding,
    VerifyResponse,
    VisualizeResponse,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "test-quality-password"
DATE_OPTION = "edges.d_ambiguous: день/месяц/год (%d/%m/%Y)"


def _alembic(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    return cfg


@pytest.fixture
def configured(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    command.upgrade(_alembic(url), "head")
    engine = create_db_engine(url)
    factory = sessionmaker(bind=engine)
    bootstrap_local_user(factory, "local", username="local")
    with factory() as db:
        db.get(UserModel, "local").password_hash = hash_password(PASSWORD)
        db.commit()
    yield Settings(
        _env_file=None,
        cp1_local_mode=False,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
        results_data_dir=str(tmp_path / "results"),
    )
    engine.dispose()


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        login = client.post("/api/auth/login", json={"username": "local", "password": PASSWORD})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        yield client


def _files() -> list[tuple[str, tuple[str, bytes, str]]]:
    # 103 заказа: 3 без суммы, 4 с нераспознанной суммой; даты, где день ≤ 12
    amounts = ["", "", ""] + ["abc", "n/a", "—", "x1"] + [f"{i},50" for i in range(8, 104)]
    orders = "order_id;amount\n" + "".join(f"o{i};{a}\n" for i, a in enumerate(amounts, 1))
    edges = "d_ambiguous;n\n" + "".join(f"{d:02}/{d % 12 + 1:02}/2026;{d}\n" for d in range(1, 13))
    return [
        ("files", ("orders.csv", orders.encode(), "text/csv")),
        ("files", ("edges.csv", edges.encode(), "text/csv")),
    ]


def ok(data):
    return ModelResponse(status="success", data=data)


def _turn(sql: str, text: str | None) -> list[ModelResponse]:
    responses = [
        ok(Understanding(intent="data_question")),
        ok(PlanResponse(plan=[PlannedCall(tool="run_sql", args={"sql": sql})])),
    ]
    if text is not None:
        responses += [
            ok(ComposeResponse(answer_blocks=[Block(type="text", markdown=text)])),
            ok(VerifyResponse(is_valid=True, corrected_markdown=text)),
        ]
    return responses


def _markdown(message: dict) -> str:
    return "\n".join(b.get("markdown") or b.get("question") or "" for b in message["blocks"])


def test_legacy_catalog_ambiguities_survive_restart(configured):
    with _client(configured) as client:
        upload = client.post(
            "/api/datasets/upload",
            files=[
                (
                    "files",
                    (
                        "legacy.csv",
                        b"share;event_date\n15%;25/03/2026\n20%;03/04/2026\n0,15;05/06/2026\n",
                        "text/csv",
                    ),
                )
            ],
        )
        assert upload.status_code == 202, upload.text
        dataset_id = upload.json()["dataset_id"]
        assert "event: done" in client.get(f"/api/datasets/{dataset_id}/events").text
        engine = create_db_engine(configured.app_database_url)
        try:
            with sessionmaker(bind=engine)() as db:
                model = db.get(DatasetModel, dataset_id)
                catalog = deepcopy(model.catalog)
                for entry in catalog["transformations"]:
                    for key in ("without_percent_sign", "ambiguous_values", "alternative_formats"):
                        entry["detail"].pop(key, None)
                model.catalog = catalog
                db.commit()
        finally:
            engine.dispose()

    model_client = MagicMock()
    model_client.generate_typed_response.side_effect = _turn(
        'SELECT AVG(share), MIN(event_date) FROM "legacy"', "Расчёт выполнен."
    )
    with _client(configured) as client:

        def runtime(request: Request):
            return replace(
                AgentRuntime(graph=build_agent_graph(), client=model_client),
                dataset_context=request.app.state.dataset_service.get_context,
                metric_service=request.app.state.metric_service,
                results_storage_dir=configured.results_data_dir,
            )

        client.app.dependency_overrides[get_agent_runtime] = runtime
        chat_id = client.post("/api/chats", json={"dataset_id": dataset_id}).json()["chat_id"]
        response = client.post(
            f"/api/chats/{chat_id}/messages", json={"text": "Средняя доля и первая дата?"}
        )
        assert "event: done" in response.text, response.text

    with _client(configured) as client:
        answer = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]
        assert "масштаб неизвестен" in _markdown(answer)
        assert "день и месяц" in _markdown(answer)
        (result_id,) = answer["result_ids"]
        result = client.get(f"/api/results/{result_id}").json()
        assert {
            issue["code"] for column in result["quality"]["columns"] for issue in column["issues"]
        } == {"percent_scale", "ambiguous_date"}
        # После перезапуска результат помнит ревизию и время своего чтения (B1-11).
        read = result["read"]
        assert (read["catalog_revision"], read["read_mode"], read["consistency"]) == (
            1,
            "snapshot",
            "immutable",
        )
        assert read["started_at"] <= read["finished_at"]


def test_losses_remain_visible_when_followup_reuses_saved_result(configured) -> None:
    client_model = MagicMock()
    client_model.generate_typed_response.side_effect = [
        *_turn("SELECT SUM(amount) FROM orders", "Сумма — 5 664."),
        ok(Understanding(intent="modify_previous")),
        ok(PlanResponse(plan=[])),
        ok(VisualizeResponse(charts=[])),
        ok(ComposeResponse(answer_blocks=[Block(type="text", markdown="Кратко: сумма — 5 664.")])),
        ok(VerifyResponse(is_valid=True, corrected_markdown="Кратко: сумма — 5 664.")),
    ]
    with _client(configured) as client:
        upload = client.post("/api/datasets/upload", files=_files())
        assert upload.status_code == 202, upload.text
        dataset_id = upload.json()["dataset_id"]
        assert "event: done\n" in client.get(f"/api/datasets/{dataset_id}/events").text

        def runtime(request: Request) -> AgentRuntime:
            return replace(
                AgentRuntime(graph=build_agent_graph(), client=client_model),
                dataset_context=request.app.state.dataset_service.get_context,
                metric_service=request.app.state.metric_service,
                results_storage_dir=configured.results_data_dir,
            )

        client.app.dependency_overrides[get_agent_runtime] = runtime
        chat_id = client.post("/api/chats", json={"dataset_id": dataset_id}).json()["chat_id"]
        send = f"/api/chats/{chat_id}/messages"
        first = client.post(send, json={"text": "Сумма заказов?"})
        assert "event: done" in first.text
        followup = client.post(send, json={"text": "Объясни тот же результат короче."})
        assert "event: done" in followup.text, followup.text
        assert "event: tool_call" not in followup.text
        assert "4 из 100" in followup.text
        answer = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]
        assert "Кратко: сумма — 5 664." in _markdown(answer)
        assert "**Ограничения данных.**" in _markdown(answer)
        assert "4 из 100" in _markdown(answer)


def test_losses_survive_restart_and_ambiguity_is_clarified(configured) -> None:
    client_model = MagicMock()
    client_model.generate_typed_response.side_effect = [
        *_turn("SELECT SUM(amount) FROM orders", "Сумма — 5 664."),
        *_turn("SELECT d_ambiguous, COUNT(*) FROM edges GROUP BY 1", None),
        *_turn(
            "SELECT strptime(d_ambiguous, '%d/%m/%Y') AS d, COUNT(*) FROM edges GROUP BY 1",
            "12 дат.",
        ),
        *_turn("SELECT d_ambiguous, COUNT(*) FROM edges GROUP BY 1", None),
    ]
    with _client(configured) as client:
        upload = client.post("/api/datasets/upload", files=_files())
        assert upload.status_code == 202, upload.text
        dataset_id = upload.json()["dataset_id"]
        assert "event: done\n" in client.get(f"/api/datasets/{dataset_id}/events").text

        def runtime(request: Request) -> AgentRuntime:
            return replace(
                AgentRuntime(graph=build_agent_graph(), client=client_model),
                dataset_context=request.app.state.dataset_service.get_context,
                metric_service=request.app.state.metric_service,
                results_storage_dir=configured.results_data_dir,
            )

        client.app.dependency_overrides[get_agent_runtime] = runtime
        chat_id = client.post("/api/chats", json={"dataset_id": dataset_id}).json()["chat_id"]
        send = f"/api/chats/{chat_id}/messages"
        assert "event: done" in client.post(send, json={"text": "Сумма заказов?"}).text

    # Перезапуск: новое приложение читает сохранённые ответ и результат.
    with _client(configured) as client:
        messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
        answer = messages[-1]
        assert "**Ограничения данных.**" in _markdown(answer)
        assert "4 из 100" in _markdown(answer)
        (result_id,) = answer["result_ids"]
        result = client.get(f"/api/results/{result_id}").json()
        (amount,) = result["quality"]["columns"]
        assert (amount["column"], amount["scope"]) == ("amount", "population")
        assert (amount["rows"], amount["source_empty"], amount["non_empty"]) == (103, 3, 100)
        assert amount["parse_failed"] == 4
        assert amount["issues"][0]["code"] == "parse_losses"
        assert any(f["kind"] == "data_quality" for f in result["facts"])

        client.app.dependency_overrides[get_agent_runtime] = runtime
        assert "event: done" in client.post(send, json={"text": "Сколько по датам?"}).text
        clarification = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]["blocks"][0]
        assert clarification["type"] == "clarification"
        assert DATE_OPTION in clarification["options"]

        assert "event: done" in client.post(send, json={"text": DATE_OPTION}).text
        last = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]
        assert "12 дат." in _markdown(last)
        assert "Выбор уточнён пользователем." in _markdown(last)
        understand_question = client_model.generate_typed_response.call_args_list[6].args[
            1
        ]  # understand третьего хода
        assert understand_question.startswith("Сколько по датам?")
        assert DATE_OPTION in understand_question

        # Выбор действует только на свой вопрос: новый вопрос по той же колонке уточняется.
        assert "event: done" in client.post(send, json={"text": "А по датам снова?"}).text
        again = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]["blocks"][0]
        assert again["type"] == "clarification"
        assert DATE_OPTION in again["options"]


@pytest.mark.parametrize("starting_revision", ["f166metr0001", "f191merge0001", "f168qual0001"])
def test_quality_migration_upgrades_and_downgrades(tmp_path: Path, starting_revision: str) -> None:
    url = f"sqlite:///{(tmp_path / 'q.db').as_posix()}"
    cfg = _alembic(url)
    assert ScriptDirectory.from_config(cfg).get_heads() == ["f169dash0001"]
    command.upgrade(cfg, starting_revision)
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    try:
        assert "quality" in {c["name"] for c in inspect(engine).get_columns("results")}
        assert {"summary", "focus_result_ids"} <= {
            c["name"] for c in inspect(engine).get_columns("chats")
        }
        assert "metric_definitions" in inspect(engine).get_table_names()
        command.downgrade(cfg, "f166metr0001")
        assert "quality" not in {c["name"] for c in inspect(engine).get_columns("results")}
    finally:
        engine.dispose()
