"""Сквозной сценарий дашборда: генерация, сохранение, перезапуск и refresh (#78)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from djedai.agent.client import ModelResponse
from djedai.agent.data_policy import DataTransferPolicy
from djedai.agent.reasoning import REASONING_STAGES
from djedai.backend import chat_service
from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.backend.db.models import ChatModel, ToolResultModel
from djedai.backend.db.session import init_db
from djedai.config import get_limits
from djedai.contracts.agent import (
    DashboardPlanResponse,
    DashboardStepPlan,
    DashboardWidgetPlan,
    Understanding,
)
from djedai.contracts.chart import ChartTemplate, XAxis, YMeasure

pytestmark = pytest.mark.integration
PASSWORD = "dashboard-chat-password"


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        name = ""
        data: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data.append(line.removeprefix("data:").lstrip())
        events.append((name, json.loads("\n".join(data))))
    return events


def chart(
    chart_id: str,
    title: str,
    *,
    kind: str = "bar",
    x: str | None = "category",
    y: tuple[str, ...] = ("revenue",),
) -> ChartTemplate:
    return ChartTemplate(
        chart_id=chart_id,
        type=kind,
        title=title,
        x=XAxis(field=x) if x is not None else None,
        y=[YMeasure(field=field) for field in y],
    )


def dashboard_plan(sales: str, products: str) -> DashboardPlanResponse:
    prep_sql = (
        f"SELECT s.sale_id, s.product_id, s.amount, p.category "
        f"FROM {sales} s JOIN {products} p ON s.product_id = p.product_id"
    )
    revenue_sql = (
        "SELECT category, SUM(amount) AS revenue FROM source_result "
        "GROUP BY category ORDER BY category"
    )
    total_sql = f"SELECT SUM(amount) AS revenue FROM {sales}"
    orders_sql = (
        f"SELECT p.category, COUNT(*) AS orders FROM {sales} s "
        f"JOIN {products} p ON s.product_id = p.product_id "
        "GROUP BY p.category ORDER BY p.category"
    )
    return DashboardPlanResponse(
        title="Продажи",
        description="Обзор продаж по категориям",
        widgets=[
            DashboardWidgetPlan(
                title="Выручка по категориям",
                chart=chart("ch_revenue", "Выручка по категориям"),
                caption="Предварительный JOIN и агрегация",
                steps=[
                    DashboardStepPlan(step_id="prep", tool="run_sql", args={"sql": prep_sql}),
                    DashboardStepPlan(
                        step_id="agg",
                        tool="run_sql",
                        args={"sql": revenue_sql},
                        source_step_id="prep",
                    ),
                ],
                output_step_id="agg",
            ),
            DashboardWidgetPlan(
                title="Всего",
                chart=chart("ch_total", "Всего", kind="kpi", x=None),
                caption="Общая выручка",
                steps=[DashboardStepPlan(step_id="only", tool="run_sql", args={"sql": total_sql})],
            ),
            DashboardWidgetPlan(
                title="Заказы по категориям",
                chart=chart("ch_orders", "Заказы по категориям", y=("orders",)),
                caption="Число продаж",
                steps=[DashboardStepPlan(step_id="only", tool="run_sql", args={"sql": orders_sql})],
            ),
        ],
    )


class DashboardPlanner:
    def __init__(self, plan: DashboardPlanResponse) -> None:
        self.plan = plan
        self.stages: list[str] = []
        self.max_output_tokens: list[int | None] = []

    def generate_typed_response(self, _system_prompt, user_prompt, response_model, **kwargs):
        stage = kwargs["stage"]
        assert stage in REASONING_STAGES, f"Unknown reasoning stage: {stage}"
        self.stages.append(stage)
        self.max_output_tokens.append(kwargs.get("max_output_tokens"))
        if stage == "understand":
            return ModelResponse(
                status="success",
                data=Understanding(intent="dashboard", resolved_question=user_prompt),
            )
        if stage == "plan" and response_model is DashboardPlanResponse:
            return ModelResponse(status="success", data=self.plan)
        raise AssertionError(f"Unexpected model stage: {stage}")


@pytest.fixture
def dashboard_chat_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        _env_file=None,
        cp1_local_mode=False,
        catalog_descriptions=False,
        app_database_url=f"sqlite:///{tmp_path / 'app.db'}",
        datasets_data_dir=str(tmp_path / "datasets"),
        results_data_dir=str(tmp_path / "results"),
        diagnostics_dir=str(tmp_path / "diagnostics"),
        auth_rate_limit_requests=1000,
    )
    app = create_app(settings)
    init_db(app.state.engine)
    with TestClient(app) as client:
        credentials = {"username": "dashboard-owner", "password": PASSWORD}
        assert client.post("/api/auth/register", json=credentials).status_code == 201
        logged_in = client.post("/api/auth/login", json=credentials)
        assert logged_in.status_code == 200, logged_in.text
        token = logged_in.json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        user_id = client.get("/api/auth/me").json()["user_id"]

        response = client.post(
            "/api/datasets/upload",
            files=[
                (
                    "files",
                    (
                        "sales.csv",
                        b"sale_id,product_id,amount\n1,1,100\n2,1,200\n3,2,50\n",
                        "text/csv",
                    ),
                ),
                (
                    "files",
                    (
                        "products.csv",
                        b"product_id,category\n1,electronics\n2,books\n",
                        "text/csv",
                    ),
                ),
            ],
        )
        assert response.status_code == 202, response.text
        dataset_id = response.json()["dataset_id"]
        events = client.get(f"/api/datasets/{dataset_id}/events")
        assert "event: done" in events.text, events.text
        assert "event: error" not in events.text, events.text
        catalog = client.get(f"/api/datasets/{dataset_id}/catalog").json()
        tables = {table["original_name"]: table for table in catalog["tables"]}
        sales = tables["sales.csv"]
        products = tables["products.csv"]

        yield (
            client,
            app.state.session_factory,
            settings,
            token,
            user_id,
            dataset_id,
            sales,
            products,
        )


def _upload_data(client: TestClient, dataset_id: str, sales: dict, products: dict):
    return dashboard_plan(sales["table_id"], products["table_id"])


def _create_chat(client: TestClient, dataset_id: str) -> str:
    response = client.post("/api/chats", json={"dataset_id": dataset_id})
    assert response.status_code == 201, response.text
    return response.json()["chat_id"]


def _rows(client: TestClient, result_id: str) -> list[list[Any]]:
    response = client.get(f"/api/results/{result_id}/data")
    assert response.status_code == 200, response.text
    return response.json()["rows"]


def _widget(dashboard: dict[str, Any], title: str) -> dict[str, Any]:
    return next(widget for widget in dashboard["widgets"] if widget["chart"].get("title") == title)


@contextmanager
def _new_client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_agent_dashboard_is_saved_and_refreshes_without_old_results(
    dashboard_chat_env, monkeypatch: pytest.MonkeyPatch
):
    client, factory, settings, token, user_id, dataset_id, sales, products = dashboard_chat_env
    plan = _upload_data(client, dataset_id, sales, products)
    planner = DashboardPlanner(plan)
    monkeypatch.setattr(chat_service, "get_agent_client", lambda: planner)
    chat_id = _create_chat(client, dataset_id)

    response = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"text": "Собери дашборд продаж по категориям"},
    )

    assert response.status_code == 200, response.text
    assert planner.stages == ["understand", "plan"]
    assert planner.max_output_tokens == [None, get_limits().dashboard_max_output_tokens]
    events = parse_sse(response.text)
    assert [name for name, _ in events][-1] == "done"
    assert not [data for name, data in events if name == "error"], response.text
    blocks = [data for name, data in events if name == "block"]
    assert [block["type"] for block in blocks] == [
        "text",
        "chart",
        "chart",
        "chart",
        "dashboard_link",
    ]
    dashboard_id = blocks[-1]["dashboard_id"]
    assert blocks[-1]["title"] == "Продажи"

    dashboard = client.get(f"/api/dashboards/{dashboard_id}").json()
    assert dashboard["title"] == "Продажи"
    assert dashboard["description"] == "Обзор продаж по категориям"
    assert dashboard["source_chat_id"] == chat_id
    assert len(dashboard["widgets"]) == 3
    assert all(widget["status"] == "ok" for widget in dashboard["widgets"])

    revenue_widget = _widget(dashboard, "Выручка по категориям")
    assert revenue_widget["tool_call"] is None
    recipe = revenue_widget["recipe"]
    assert recipe["output_step_id"] == "step_2"
    assert [step["step_id"] for step in recipe["steps"]] == ["step_1", "step_2"]
    assert recipe["steps"][0]["source_step_id"] is None
    assert recipe["steps"][1]["source_step_id"] == "step_1"
    assert "JOIN" in recipe["steps"][0]["call"]["params"]["sql"].upper()
    assert "source_result" in recipe["steps"][1]["call"]["params"]["sql"]
    assert "source_result_id" not in recipe["steps"][1]["call"]["params"]
    assert revenue_widget["chart"]["result_id"] == revenue_widget["result_id"]

    total_widget = _widget(dashboard, "Всего")
    orders_widget = _widget(dashboard, "Заказы по категориям")
    assert total_widget["recipe"] is None and total_widget["tool_call"] is not None
    assert orders_widget["recipe"] is None and orders_widget["tool_call"] is not None

    assert _rows(client, revenue_widget["result_id"]) == [
        ["books", 50.0],
        ["electronics", 300.0],
    ]
    assert _rows(client, total_widget["result_id"]) == [[350.0]]
    assert _rows(client, orders_widget["result_id"]) == [
        ["books", 1],
        ["electronics", 2],
    ]

    # Промежуточный результат JOIN не входит в снимок виджета, но сохранён
    # как родитель выходного результата и должен быть воспроизведён рецептом.
    with factory() as db:
        output = db.get(ToolResultModel, revenue_widget["result_id"])
        assert output is not None and output.source_result_id is not None
        intermediate_id = output.source_result_id
        intermediate = db.get(ToolResultModel, intermediate_id)
        assert intermediate is not None
        assert "JOIN" in (intermediate.params or {}).get("sql", "").upper()
    old_result_ids = {
        revenue_widget["result_id"],
        total_widget["result_id"],
        orders_widget["result_id"],
        intermediate_id,
    }

    # Перезапуск приложения: ссылка и снимок открываются без пересчёта.
    with _new_client(settings) as restarted:
        restarted.headers["Authorization"] = f"Bearer {token}"
        reopened = restarted.get(f"/api/dashboards/{dashboard_id}")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["widgets"] == dashboard["widgets"]
        assert _rows(restarted, revenue_widget["result_id"]) == [
            ["books", 50.0],
            ["electronics", 300.0],
        ]

        # Меняем исходные данные и удаляем все результаты первого расчёта,
        # включая промежуточный JOIN: refresh обязан пересчитать рецепт.
        context = restarted.app.state.dataset_service.get_context(user_id, dataset_id)
        with duckdb.connect(str(context.duckdb_path)) as connection:
            connection.execute(
                f'INSERT INTO "{sales["raw_table"]}" (sale_id, product_id, amount) '
                "VALUES ('4', '2', '70')"
            )

        with restarted.app.state.session_factory() as db:
            for result_id in old_result_ids:
                record = db.get(ToolResultModel, result_id)
                assert record is not None
                db.delete(record)
            db.commit()
        for result_id in old_result_ids:
            (Path(settings.results_data_dir) / f"{result_id}.parquet").unlink(missing_ok=True)

        refreshed_response = restarted.post(f"/api/dashboards/{dashboard_id}/refresh")
        assert refreshed_response.status_code == 200, refreshed_response.text
        refreshed = refreshed_response.json()
        assert refreshed["refreshed_at"] is not None
        assert len(refreshed["widgets"]) == 3
        assert all(widget["status"] == "ok" for widget in refreshed["widgets"])

        refreshed_revenue = _widget(refreshed, "Выручка по категориям")
        refreshed_total = _widget(refreshed, "Всего")
        refreshed_orders = _widget(refreshed, "Заказы по категориям")
        new_result_ids = {
            refreshed_revenue["result_id"],
            refreshed_total["result_id"],
            refreshed_orders["result_id"],
        }
        assert new_result_ids.isdisjoint(old_result_ids)
        assert all(result_id is not None for result_id in new_result_ids)

        assert _rows(restarted, refreshed_revenue["result_id"]) == [
            ["books", 120.0],
            ["electronics", 300.0],
        ]
        assert _rows(restarted, refreshed_total["result_id"]) == [[420.0]]
        assert _rows(restarted, refreshed_orders["result_id"]) == [
            ["books", 2],
            ["electronics", 2],
        ]

        # Новый выходной результат зависимого виджета ссылается на новый
        # промежуточный результат, а не на удалённый старый.
        with restarted.app.state.session_factory() as db:
            output = db.get(ToolResultModel, refreshed_revenue["result_id"])
            assert output is not None and output.source_result_id is not None
            assert output.source_result_id != intermediate_id
            parent = db.get(ToolResultModel, output.source_result_id)
            assert parent is not None
            assert "JOIN" in (parent.params or {}).get("sql", "").upper()
            assert db.get(ToolResultModel, intermediate_id) is None
            for result_id in old_result_ids:
                assert db.get(ToolResultModel, result_id) is None

        # Повторное открытие после refresh возвращает уже новый снимок.
        persisted = restarted.get(f"/api/dashboards/{dashboard_id}").json()
        assert persisted["refreshed_at"] == refreshed["refreshed_at"]
        assert _widget(persisted, "Всего")["result_id"] == refreshed_total["result_id"]

        # Продолжение диалога: "открой созданную панель и обнови её" должно
        # переиспользовать сохранённый dashboard_id, а не создавать новый дашборд.
        class ChatRefreshPlanner:
            def __init__(self) -> None:
                self.stages: list[str] = []

            def generate_typed_response(
                self, _system_prompt, _user_prompt, response_model, **kwargs
            ):
                stage = kwargs["stage"]
                self.stages.append(stage)
                if stage == "understand":
                    return ModelResponse(
                        status="success",
                        data=Understanding(intent="dashboard", dashboard_id=dashboard_id),
                    )
                raise AssertionError(f"Unexpected model stage: {stage}")

        chat_planner = ChatRefreshPlanner()
        monkeypatch.setattr(chat_service, "get_agent_client", lambda: chat_planner)
        chat_refresh = restarted.post(
            f"/api/chats/{chat_id}/messages",
            json={"text": "Открой созданную панель и обнови все её виджеты"},
        )
        assert chat_refresh.status_code == 200, chat_refresh.text
        events = parse_sse(chat_refresh.text)
        assert not [data for name, data in events if name == "error"], chat_refresh.text
        blocks = [data for name, data in events if name == "block"]
        assert chat_planner.stages == ["understand"]
        assert blocks[-1]["type"] == "dashboard_link"
        assert blocks[-1]["dashboard_id"] == dashboard_id
        after_chat_refresh = restarted.get(f"/api/dashboards/{dashboard_id}").json()
        chat_refresh_result_ids = {widget["result_id"] for widget in after_chat_refresh["widgets"]}
        assert chat_refresh_result_ids.isdisjoint(new_result_ids)
        assert all(result_id is not None for result_id in chat_refresh_result_ids)

        with restarted.app.state.session_factory() as db:
            assert set(db.get(ChatModel, chat_id).focus_result_ids) == chat_refresh_result_ids

    # A new process must restore the refreshed focus and answer a text-only
    # continuation without a new SQL call, using the updated 420 total.
    class FollowupPlanner:
        data_policy = DataTransferPolicy.sharing()

        def __init__(self) -> None:
            self.stages: list[str] = []
            self.prompts: dict[str, str] = {}

        def generate_typed_response(self, _system_prompt, user_prompt, response_model, **kwargs):
            stage = kwargs["stage"]
            self.stages.append(stage)
            self.prompts[stage] = _system_prompt + "\n" + user_prompt
            data = {
                "understand": {"intent": "data_question"},
                "plan": {"plan": []},
                "compose": {"answer_blocks": [{"type": "text", "markdown": "Всего 420."}]},
                "verify": {"is_valid": True, "corrected_markdown": ""},
            }[stage]
            return ModelResponse(status="success", data=response_model.model_validate(data))

    followup = FollowupPlanner()
    monkeypatch.setattr(chat_service, "get_agent_client", lambda: followup)
    with _new_client(settings) as continued:
        continued.headers["Authorization"] = f"Bearer {token}"
        response = continued.post(
            f"/api/chats/{chat_id}/messages", json={"text": "Напомни общую выручку этой панели"}
        )
        events = parse_sse(response.text)
        assert [name for name, _ in events][-1] == "done", response.text
        assert not [name for name, _ in events if name in ("error", "tool_call")], response.text
        assert followup.stages == ["understand", "plan", "compose", "verify"]
        for stage in ("understand", "plan", "compose"):
            for result_id in chat_refresh_result_ids:
                assert result_id in followup.prompts[stage], followup.prompts[stage]
            assert "420" in followup.prompts[stage], followup.prompts[stage]
        assert any(
            name == "block" and data.get("markdown") == "Всего 420." for name, data in events
        )
        with continued.app.state.session_factory() as db:
            assert set(db.get(ChatModel, chat_id).focus_result_ids) == chat_refresh_result_ids

        # An ID from another dataset must be rejected before refresh mutates its
        # snapshot or saves tool results under an unrelated chat.
        other_upload = continued.post(
            "/api/datasets/upload",
            files=[("files", ("other.csv", b"value\n1\n", "text/csv"))],
        )
        assert other_upload.status_code == 202, other_upload.text
        other_dataset = other_upload.json()["dataset_id"]
        assert "event: done" in continued.get(f"/api/datasets/{other_dataset}/events").text
        other_chat = _create_chat(continued, other_dataset)
        before = continued.get(f"/api/dashboards/{dashboard_id}").json()
        monkeypatch.setattr(chat_service, "get_agent_client", lambda: ChatRefreshPlanner())
        rejected = continued.post(
            f"/api/chats/{other_chat}/messages", json={"text": "Обнови прежнюю панель"}
        )
        assert any(name == "error" for name, _ in parse_sse(rejected.text)), rejected.text
        assert continued.get(f"/api/dashboards/{dashboard_id}").json() == before
        with continued.app.state.session_factory() as db:
            assert db.query(ToolResultModel).filter_by(chat_id=other_chat).count() == 0
