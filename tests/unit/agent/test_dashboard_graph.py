"""Граф дашборда: отдельная ветка, зависимости и сохранение превью (#78)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from djedai.agent.client import ModelResponse
from djedai.agent.data_policy import REDACTED, DataTransferPolicy
from djedai.agent.graph import (
    build_agent_graph,
    execute_dashboard,
    route_after_understand,
    understand,
)
from djedai.agent.reasoning import REASONING_STAGES
from djedai.config import get_limits
from djedai.contracts.agent import (
    Block,
    DashboardPlanResponse,
    DashboardStepPlan,
    DashboardWidgetPlan,
    Message,
    Understanding,
)
from djedai.contracts.chart import ChartSpec, ChartTemplate, XAxis, YMeasure
from djedai.contracts.dashboard import (
    DashboardSnapshot,
    WidgetLayout,
    WidgetSnapshot,
    WidgetToolCall,
)
from djedai.contracts.tool_result import Fact, ToolError, ToolResult, ToolResultData


def _chart(
    chart_id: str,
    *,
    kind: str = "bar",
    x: str | None = "category",
    y: tuple[str, ...] = ("revenue",),
) -> ChartTemplate:
    return ChartTemplate(
        chart_id=chart_id,
        type=kind,
        title=f"График {chart_id}",
        x=XAxis(field=x) if x is not None else None,
        y=[YMeasure(field=field) for field in y],
    )


def _step(step_id: str, sql: str, source_step_id: str | None = None) -> DashboardStepPlan:
    return DashboardStepPlan(
        step_id=step_id,
        tool="run_sql",
        args={"sql": sql},
        source_step_id=source_step_id,
    )


def _dashboard_plan() -> DashboardPlanResponse:
    return DashboardPlanResponse(
        title="Продажи",
        description="Обзор продаж",
        widgets=[
            DashboardWidgetPlan(
                title="По категориям",
                chart=_chart("ch_category"),
                caption="Выручка по категориям",
                steps=[
                    _step(
                        "prep",
                        "SELECT s.sale_id, p.category, s.amount "
                        "FROM sales s JOIN products p ON s.product_id = p.product_id",
                    ),
                    _step(
                        "agg",
                        "SELECT category, SUM(amount) AS revenue "
                        "FROM source_result GROUP BY category ORDER BY category",
                        source_step_id="prep",
                    ),
                ],
                output_step_id="agg",
            ),
            DashboardWidgetPlan(
                title="Всего",
                chart=_chart("ch_total", kind="kpi", x=None, y=("revenue",)),
                caption="Общая выручка",
                steps=[_step("only", "SELECT SUM(amount) AS revenue FROM sales")],
            ),
            DashboardWidgetPlan(
                title="Заказы",
                chart=_chart("ch_orders", y=("orders",)),
                caption="Число продаж по категориям",
                steps=[
                    _step(
                        "only",
                        "SELECT p.category, COUNT(*) AS orders "
                        "FROM sales s JOIN products p ON s.product_id = p.product_id "
                        "GROUP BY p.category ORDER BY p.category",
                    )
                ],
            ),
        ],
    )


class FakeClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = {stage: list(items) for stage, items in responses.items()}
        self.stages: list[str] = []
        self.response_models: list[str] = []
        self.system_prompts: list[str] = []
        self.max_output_tokens: list[int | None] = []

    def generate_typed_response(self, system_prompt, _user_prompt, response_model, **kwargs):
        stage = kwargs["stage"]
        # Повторяем проверку BIModelClient.reasoning_for: неизвестная стадия
        # должна падать до планирования дашборда, а не в реальном клиенте.
        if stage not in REASONING_STAGES:
            raise ValueError(f"Неизвестная стадия reasoning: {stage!r}")
        self.stages.append(stage)
        self.response_models.append(response_model.__name__)
        self.system_prompts.append(system_prompt)
        self.max_output_tokens.append(kwargs.get("max_output_tokens"))
        return ModelResponse(status="success", data=self.responses[stage].pop(0))


class DashboardSaver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, list[Any]]] = []

    def __call__(self, title, description, widgets):
        self.calls.append((title, description, widgets))
        return DashboardSnapshot(
            dashboard_id="d_test",
            dataset_id="ds_test",
            title=title,
            description=description,
            source_chat_id="chat_test",
            widgets=widgets,
            created_at=datetime.now(UTC),
            refreshed_at=datetime.now(UTC),
        )


def _dispatcher(rows_by_sql: dict[str, tuple[list[str], list[list[object]]]]):
    calls: list[tuple[str, dict[str, Any], ToolResult | None]] = []

    def dispatch(tool: str, args: dict[str, Any], source_result: ToolResult | None = None):
        calls.append((tool, args, source_result))
        columns, rows = rows_by_sql[args["sql"]]
        return ToolResult(
            result_id=f"r_{len(calls)}",
            status="ok",
            tool=tool,
            params=args,
            data=ToolResultData(columns=columns, rows=rows, row_count=len(rows)),
        )

    dispatch.calls = calls  # type: ignore[attr-defined]
    return dispatch


def _rows():
    return {
        "SELECT s.sale_id, p.category, s.amount "
        "FROM sales s JOIN products p ON s.product_id = p.product_id": (
            ["sale_id", "category", "amount"],
            [[1, "electronics", 100.0], [2, "electronics", 200.0], [3, "books", 50.0]],
        ),
        "SELECT category, SUM(amount) AS revenue "
        "FROM source_result GROUP BY category ORDER BY category": (
            ["category", "revenue"],
            [["books", 50.0], ["electronics", 300.0]],
        ),
        "SELECT SUM(amount) AS revenue FROM sales": (["revenue"], [[350.0]]),
        "SELECT p.category, COUNT(*) AS orders "
        "FROM sales s JOIN products p ON s.product_id = p.product_id "
        "GROUP BY p.category ORDER BY p.category": (
            ["category", "orders"],
            [["books", 1], ["electronics", 2]],
        ),
    }


def test_dashboard_branch_plans_executes_and_saves_without_standard_plan():
    client = FakeClient(
        {
            "understand": [Understanding(intent="dashboard")],
            "plan": [_dashboard_plan()],
        }
    )
    dispatcher = _dispatcher(_rows())
    saver = DashboardSaver()

    result = build_agent_graph().invoke(
        {
            "chat_id": "chat_test",
            "dataset_id": "ds_test",
            "messages": [Message(role="user", content="Собери дашборд продаж")],
        },
        config={
            "configurable": {
                "thread_id": "chat_test",
                "client": client,
                "tool_dispatcher": dispatcher,
                "dashboard_saver": saver,
            }
        },
    )

    assert client.stages == ["understand", "plan"]
    assert client.response_models == ["Understanding", "DashboardPlanResponse"]
    assert client.max_output_tokens == [None, get_limits().dashboard_max_output_tokens]
    assert "run_sql" in client.system_prompts[-1]
    assert "compute_metric" in client.system_prompts[-1]
    assert "detect_anomalies" not in client.system_prompts[-1]
    assert result.get("plan", []) == []
    assert result["dashboard_plan"] is not None
    assert len(result["dashboard_plan"].widgets) == 3
    assert result["dashboard_plan"].widgets[0].steps[1].source_step_id == "step_1"
    assert len(result["dashboard_widgets"]) == 3

    assert len(saver.calls) == 1
    title, description, widgets = saver.calls[0]
    assert title == "Продажи"
    assert description == "Обзор продаж"
    assert [widget.status for widget in widgets] == ["ok", "ok", "ok"]
    assert widgets[0].tool_call is None
    assert widgets[0].recipe is not None
    assert widgets[0].recipe.steps[1].source_step_id == "step_1"
    assert "source_result_id" not in widgets[0].recipe.steps[1].call.params
    assert widgets[1].tool_call is not None
    assert widgets[1].recipe is None

    assert [block.type for block in result["answer_blocks"]] == [
        "text",
        "chart",
        "chart",
        "chart",
        "dashboard_link",
    ]
    assert result["answer_blocks"][-1].dashboard_id == "d_test"
    assert result["dashboard_snapshot"] is not None
    assert result["focus_result_ids"] == ["r_2", "r_3", "r_4"]

    calls = dispatcher.calls  # type: ignore[attr-defined]
    assert len(calls) == 4
    assert calls[0][2] is None
    assert calls[1][2] is not None
    assert calls[1][2].result_id == "r_1"
    assert calls[1][1]["sql"].endswith("FROM source_result GROUP BY category ORDER BY category")
    assert [item.result_id for item in result["results"]] == ["r_1", "r_2", "r_3", "r_4"]


def test_dashboard_branch_retries_the_whole_plan_when_all_widgets_fail():
    client = FakeClient(
        {
            "understand": [Understanding(intent="dashboard")],
            "plan": [_dashboard_plan(), _dashboard_plan()],
        }
    )
    rows = _rows()
    attempts = {"count": 0}

    def dispatch(tool: str, args: dict[str, Any], source_result: ToolResult | None = None):
        attempts["count"] += 1
        if attempts["count"] <= 3:
            return ToolResult(
                result_id=f"r_fail_{attempts['count']}",
                status="error",
                tool=tool,
                params=args,
                error=ToolError(code="sql_error", message="Временная ошибка"),
            )
        columns, values = rows[args["sql"]]
        return ToolResult(
            result_id=f"r_ok_{attempts['count']}",
            status="ok",
            tool=tool,
            params=args,
            data=ToolResultData(columns=columns, rows=values, row_count=len(values)),
        )

    saver = DashboardSaver()

    result = build_agent_graph().invoke(
        {
            "chat_id": "chat_test",
            "dataset_id": "ds_test",
            "messages": [Message(role="user", content="Собери дашборд продаж")],
        },
        config={
            "configurable": {
                "thread_id": "chat_test",
                "client": client,
                "tool_dispatcher": dispatch,
                "dashboard_saver": saver,
            }
        },
    )

    assert client.stages == ["understand", "plan", "plan"]
    assert result["dashboard_retries"] == 2
    assert len(saver.calls) == 1
    assert [widget.status for widget in saver.calls[0][2]] == ["ok", "ok", "ok"]
    assert [block.type for block in result["answer_blocks"]] == [
        "text",
        "chart",
        "chart",
        "chart",
        "dashboard_link",
    ]


def test_dashboard_branch_retries_a_partial_failure_before_saving():
    client = FakeClient(
        {
            "understand": [Understanding(intent="dashboard")],
            "plan": [_dashboard_plan(), _dashboard_plan()],
        }
    )
    rows = _rows()
    attempts = {"count": 0}

    def dispatch(tool: str, args: dict[str, Any], source_result: ToolResult | None = None):
        attempts["count"] += 1
        # Первый виджет падает на первом шаге; остальные виджеты считаются.
        if attempts["count"] == 1:
            return ToolResult(
                result_id="r_partial_fail",
                status="error",
                tool=tool,
                params=args,
                error=ToolError(code="sql_error", message="Ошибка JOIN"),
            )
        columns, values = rows[args["sql"]]
        return ToolResult(
            result_id=f"r_partial_ok_{attempts['count']}",
            status="ok",
            tool=tool,
            params=args,
            data=ToolResultData(columns=columns, rows=values, row_count=len(values)),
        )

    saver = DashboardSaver()

    build_agent_graph().invoke(
        {
            "chat_id": "chat_test",
            "dataset_id": "ds_test",
            "messages": [Message(role="user", content="Собери дашборд продаж")],
        },
        config={
            "configurable": {
                "thread_id": "chat_test",
                "client": client,
                "tool_dispatcher": dispatch,
                "dashboard_saver": saver,
            }
        },
    )

    assert client.stages == ["understand", "plan", "plan"]
    assert [widget.status for widget in saver.calls[0][2]] == ["ok", "ok", "ok"]


def test_dashboard_branch_reports_unavailable_saver():
    client = FakeClient(
        {
            "understand": [Understanding(intent="dashboard")],
            "plan": [_dashboard_plan()],
        }
    )

    result = build_agent_graph().invoke(
        {
            "chat_id": "chat_test",
            "dataset_id": "ds_test",
            "messages": [Message(role="user", content="Собери дашборд продаж")],
        },
        config={
            "configurable": {
                "thread_id": "chat_test",
                "client": client,
                "tool_dispatcher": _dispatcher(_rows()),
            }
        },
    )

    assert result["dashboard_snapshot"] is None
    assert result["answer_blocks"][0].type == "error"
    assert "Сохранение дашбордов" in result["answer_blocks"][0].markdown


def _saved_snapshot(dashboard_id: str = "d_prev") -> DashboardSnapshot:
    widgets = []
    for index, kind in enumerate(("kpi", "bar", "pie"), start=1):
        result_id = f"r_refreshed_{index}"
        widgets.append(
            WidgetSnapshot(
                widget_id=f"w_{index}",
                chart=ChartSpec(
                    chart_id=f"ch_{index}",
                    type=kind,
                    title=f"Виджет {index}",
                    result_id=result_id,
                    x=None if kind == "kpi" else XAxis(field="category"),
                    y=[YMeasure(field="value")],
                ),
                layout=WidgetLayout(x=0, y=index, w=6, h=4),
                tool_call=WidgetToolCall(tool="run_sql", params={"sql": "SELECT 1"}),
                result_id=result_id,
                status="ok",
            )
        )
    return DashboardSnapshot(
        dashboard_id=dashboard_id,
        dataset_id="ds_test",
        title="Обзор фоновых заданий",
        widgets=widgets,
        created_at=datetime.now(UTC),
        refreshed_at=datetime.now(UTC),
    )


def test_saved_dashboard_is_refreshed_by_id_without_creating_a_new_one():
    client = FakeClient({"understand": [Understanding(intent="dashboard", dashboard_id="d_prev")]})
    calls: list[str] = []

    def refresher(dashboard_id: str) -> DashboardSnapshot:
        calls.append(dashboard_id)
        return _saved_snapshot(dashboard_id)

    result = build_agent_graph().invoke(
        {
            "chat_id": "chat_refresh",
            "dataset_id": "ds_test",
            "messages": [Message(role="user", content="Открой созданную панель и обнови её")],
            "previous_blocks": [
                Block(type="dashboard_link", dashboard_id="d_prev", title="Обзор фоновых заданий")
            ],
        },
        config={
            "configurable": {
                "thread_id": "chat_refresh",
                "client": client,
                "dashboard_refresher": refresher,
            }
        },
    )

    # The refresh branch must not plan a second dashboard.
    assert client.stages == ["understand"]
    assert calls == ["d_prev"]
    assert result["dashboard_snapshot"].dashboard_id == "d_prev"
    assert [block.type for block in result["answer_blocks"]] == [
        "text",
        "chart",
        "chart",
        "chart",
        "dashboard_link",
    ]
    assert "обновлён" in result["answer_blocks"][0].markdown
    assert result["answer_blocks"][-1].dashboard_id == "d_prev"
    assert result["focus_result_ids"] == ["r_refreshed_1", "r_refreshed_2", "r_refreshed_3"]


def test_dashboard_refresh_failure_is_explicit_and_does_not_reuse_old_snapshot():
    client = FakeClient(
        {"understand": [Understanding(intent="dashboard", dashboard_id="d_missing")]}
    )

    def refresher(_dashboard_id: str) -> DashboardSnapshot:
        raise ValueError("Дашборд не найден")

    result = build_agent_graph().invoke(
        {
            "chat_id": "chat_refresh_error",
            "dataset_id": "ds_test",
            "previous_blocks": [
                Block(type="dashboard_link", dashboard_id="d_missing", title="Обзор")
            ],
            "messages": [Message(role="user", content="Обнови панель")],
        },
        config={
            "configurable": {
                "thread_id": "chat_refresh_error",
                "client": client,
                "dashboard_refresher": refresher,
            }
        },
    )

    assert result.get("dashboard_snapshot") is None
    assert result["answer_blocks"][0].type == "error"
    assert "Не удалось обновить дашборд" in result["answer_blocks"][0].markdown


def test_dashboard_refresh_without_service_is_explicit():
    client = FakeClient({"understand": [Understanding(intent="dashboard", dashboard_id="d_prev")]})

    result = build_agent_graph().invoke(
        {
            "chat_id": "chat_refresh_unavailable",
            "dataset_id": "ds_test",
            "previous_blocks": [Block(type="dashboard_link", dashboard_id="d_prev", title="Обзор")],
            "messages": [Message(role="user", content="Обнови панель")],
        },
        config={"configurable": {"thread_id": "chat_refresh_unavailable", "client": client}},
    )

    assert result["answer_blocks"][0].type == "error"
    assert "недоступно" in result["answer_blocks"][0].markdown


SECRET_VALUE = "ordinaryCredential9!"


def _sensitive_dashboard_plan() -> DashboardPlanResponse:
    return DashboardPlanResponse(
        title="Секреты",
        description="Проверка зависимых виджетов",
        widgets=[
            DashboardWidgetPlan(
                title="Пароли",
                chart=_chart("ch_password", y=("count",)),
                steps=[
                    DashboardStepPlan(
                        step_id="prep",
                        tool="run_sql",
                        args={"sql": "SELECT password AS value FROM accounts"},
                    ),
                    DashboardStepPlan(
                        step_id="profile",
                        tool="describe_column",
                        args={"table": "source_result", "column": "value"},
                        source_step_id="prep",
                    ),
                ],
            ),
            DashboardWidgetPlan(
                title="Всего",
                chart=_chart("ch_total", kind="kpi", x=None, y=("total",)),
                steps=[
                    DashboardStepPlan(
                        step_id="only",
                        tool="run_sql",
                        args={"sql": "SELECT COUNT(*) AS total FROM accounts"},
                    )
                ],
            ),
            DashboardWidgetPlan(
                title="Категории",
                chart=_chart("ch_categories"),
                steps=[
                    DashboardStepPlan(
                        step_id="only",
                        tool="run_sql",
                        args={
                            "sql": (
                                "SELECT category, COUNT(*) AS count FROM accounts GROUP BY category"
                            )
                        },
                    )
                ],
            ),
        ],
    )


def _sensitive_dispatcher():
    calls: list[tuple[str, dict[str, Any], ToolResult | None]] = []

    def dispatch(tool: str, args: dict[str, Any], source_result: ToolResult | None = None):
        calls.append((tool, args, source_result))
        if tool == "run_sql":
            if args["sql"] == "SELECT password AS value FROM accounts":
                return ToolResult(
                    result_id="r_prep",
                    status="ok",
                    tool=tool,
                    sql=args["sql"],
                    params=args,
                    data=ToolResultData(columns=["value"], rows=[[SECRET_VALUE]], row_count=1),
                )
            return ToolResult(
                result_id=f"r_extra_{len(calls)}",
                status="ok",
                tool=tool,
                sql=args["sql"],
                params=args,
                data=ToolResultData(columns=["category", "count"], rows=[["a", 1]]),
            )
        assert source_result is not None
        return ToolResult(
            result_id="r_profile",
            status="ok",
            tool=tool,
            params=args,
            data=ToolResultData(columns=["value", "count"], rows=[[SECRET_VALUE, 2]], row_count=2),
            facts=[
                Fact(
                    text=f"Значение '{SECRET_VALUE}' встречается 2 раз.",
                    kind="freq",
                    values={"value": SECRET_VALUE, "count": 2},
                )
            ],
        )

    dispatch.calls = calls  # type: ignore[attr-defined]
    return dispatch


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_dashboard_dependency_inherits_parent_sensitive_columns(policy) -> None:
    """Нейтральный алиас родителя остаётся чувствительным в зависимом шаге."""
    dispatcher = _sensitive_dispatcher()

    result = execute_dashboard(
        {
            "chat_id": "chat_test",
            "dataset_id": "ds_test",
            "sensitive_columns": ("password",),
            "dashboard_plan": _sensitive_dashboard_plan(),
            "results": [],
        },
        {"configurable": {"tool_dispatcher": dispatcher, "data_policy": policy}},
    )

    by_id = {item.result_id: item for item in result["results"]}
    prep = by_id["r_prep"]
    profile = by_id["r_profile"]
    assert prep.sensitive_columns == ["value"]
    assert profile.source_result_id == "r_prep"
    assert profile.sensitive_columns == ["value"]
    # Секрет остаётся в локальных данных, но не в фактах для модели.
    assert profile.data is not None
    assert profile.data.rows == [[SECRET_VALUE, 2]]
    assert SECRET_VALUE not in str(profile.facts)
    assert REDACTED in str(profile.facts)

    calls = dispatcher.calls  # type: ignore[attr-defined]
    assert calls[1][2] is not None
    assert calls[1][2].sensitive_columns == ["value"]


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_history_restores_sensitive_columns_for_next_turn(policy) -> None:
    """Сохранённая чувствительность дочернего результата очищает контекст хода."""
    child = ToolResult(
        result_id="r_history_child",
        status="ok",
        tool="describe_column",
        params={"table": "source_result", "column": "value"},
        data=ToolResultData(columns=["value", "count"], rows=[[SECRET_VALUE, 2]], row_count=2),
        facts=[
            Fact(
                text=f"Значение '{SECRET_VALUE}' встречается 2 раз.",
                kind="freq",
                values={"value": SECRET_VALUE, "count": 2},
            )
        ],
        sensitive_columns=["value"],
    )
    client = FakeClient({"understand": [Understanding(intent="data_question")]})

    understand(
        {
            "chat_id": "chat_test",
            "dataset_id": "ds_test",
            "messages": [Message(role="user", content="Что было в первой колонке?")],
            "catalog_prompt": "Catalog",
            "history_results": [child],
            "focus_result_ids": [child.result_id],
            "sensitive_columns": ("password",),
        },
        {"configurable": {"client": client, "data_policy": policy}},
    )

    prompt = client.system_prompts[-1]
    assert SECRET_VALUE not in prompt
    assert REDACTED in prompt


def test_invented_dashboard_id_creates_a_dashboard_instead_of_refreshing():
    understanding = Understanding(intent="dashboard", dashboard_id="made_up")
    state = {"understanding": understanding, "previous_blocks": []}
    assert route_after_understand(state) == "plan_dashboard"
    known = Block(type="dashboard_link", dashboard_id="made_up", title="Обзор")
    assert route_after_understand({**state, "previous_blocks": [known]}) == "refresh_dashboard"
