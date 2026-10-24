"""Real auth/CSV/graph/dispatch/storage paths for the tools added in #66/#67."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from djedai.agent.client import ModelResponse
from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.backend.db.models import ToolResultModel
from djedai.backend.db.session import init_db

pytestmark = pytest.mark.integration
CASES = [
    ("time_aggregate", {"time_col": "ts", "freq": "month", "agg": "count"}),
    ("find_duplicates", {"columns": ["g", "amount"]}),
    ("detect_anomalies", {"measure": "amount", "method": "iqr"}),
]


class PlannedModel:
    def __init__(self, tool, args):
        self.tool, self.args = tool, args

    def generate_typed_response(self, system_prompt, user_prompt, response_model):
        if response_model.__name__ == "Understanding":
            value = {"intent": "data_question", "resolved_question": user_prompt}
        elif response_model.__name__ == "PlanResponse":
            value = {"plan": [{"tool": self.tool, "args": self.args}]}
        elif response_model.__name__ == "ComposeResponse":
            value = {"answer_blocks": [{"type": "text", "markdown": "Расчёт завершён."}]}
        elif response_model.__name__ == "VerifyResponse":
            value = {"is_valid": True, "issues": [], "corrected_markdown": "Расчёт завершён."}
        else:
            raise AssertionError(response_model.__name__)
        return ModelResponse(status="success", data=response_model.model_validate(value))


@pytest.fixture
def api(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            catalog_descriptions=False,
            app_database_url=f"sqlite:///{tmp_path / 'app.db'}",
            datasets_data_dir=str(tmp_path / "datasets"),
            results_data_dir=str(tmp_path / "results"),
        )
    )
    init_db(app.state.engine)
    with TestClient(app) as client:
        credentials = {"username": "analytics-owner", "password": "analytics-runtime-password"}
        assert client.post("/api/auth/register", json=credentials).status_code == 201
        client.headers["Authorization"] = (
            f"Bearer {client.post('/api/auth/login', json=credentials).json()['access_token']}"
        )
        rows = [
            f"{date(2026, 1, 1) + timedelta(days=i)},{1 + i % 2},{100 if i == 40 else 10 + i % 3}"
            for i in range(84)
        ]
        content = ("ts,g,amount\n" + "\n".join(rows)).encode()
        upload = client.post(
            "/api/datasets/upload", files={"files": ("metrics.csv", content, "text/csv")}
        )
        assert upload.status_code == 202, upload.text
        dataset_id = upload.json()["dataset_id"]
        assert "event: done" in client.get(f"/api/datasets/{dataset_id}/events").text
        catalog = client.get(f"/api/datasets/{dataset_id}/catalog").json()
        yield client, app, dataset_id, catalog["tables"][0]["table_id"]


@pytest.mark.parametrize("tool,params", CASES)
def test_tool_result_survives_the_protected_http_graph_and_storage(api, monkeypatch, tool, params):
    client, app, dataset, table = api
    model = PlannedModel(tool, {"table": table, **params})
    monkeypatch.setattr("djedai.backend.chat_service.get_agent_client", lambda: model)
    chat = client.post("/api/chats", json={"dataset_id": dataset}).json()["chat_id"]
    response = client.post(f"/api/chats/{chat}/messages", json={"text": "Выполни расчёт"})
    assert response.status_code == 200
    assert "event: done" in response.text, response.text
    with app.state.session_factory() as db:
        results = list(db.scalars(select(ToolResultModel).where(ToolResultModel.chat_id == chat)))
        assert len(results) == 1
        result_id = results[0].result_id
        assert results[0].status == "ok", results[0].error
        assert results[0].data_ref
    data = client.get(f"/api/results/{result_id}/data").json()
    assert data["rows"]
    if tool == "time_aggregate":
        assert sum(row[-1] for row in data["rows"]) == 84
    elif tool == "detect_anomalies":
        assert any(row[0] == 100 for row in data["rows"])
    assert client.get(f"/api/results/{result_id}").json()["tool"] == tool
    history = client.get(f"/api/chats/{chat}").json()["messages"]
    assert history[-1]["result_ids"] == [result_id]


@pytest.mark.parametrize("tool,params", CASES)
def test_nested_filter_rejection_is_persisted_without_private_data(api, monkeypatch, tool, params):
    client, app, dataset, table = api
    args = {"table": table, **params, "filter": "EXISTS (SELECT 1 FROM information_schema.tables)"}
    monkeypatch.setattr(
        "djedai.backend.chat_service.get_agent_client", lambda: PlannedModel(tool, args)
    )
    chat = client.post("/api/chats", json={"dataset_id": dataset}).json()["chat_id"]
    response = client.post(f"/api/chats/{chat}/messages", json={"text": "Выполни расчёт"})
    assert response.status_code == 200
    with app.state.session_factory() as db:
        results = list(db.scalars(select(ToolResultModel).where(ToolResultModel.chat_id == chat)))
        assert results
        assert all(
            r.status == "error" and r.error["code"] == "dataset_scope_violation" for r in results
        )
        assert all(r.data_ref is None for r in results)
