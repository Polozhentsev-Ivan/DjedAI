"""Граф дашборда: отдельная ветка, зависимости и сохранение превью (#78)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.contracts.agent import (
    DashboardPlanResponse,
    DashboardStepPlan,
    DashboardWidgetPlan,
    Message,
    Understanding,
)
from djedai.contracts.chart import ChartTemplate, XAxis, YMeasure
from djedai.contracts.dashboard import DashboardSnapshot
from djedai.contracts.tool_result import ToolError, ToolResult, ToolResultData


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

    def generate_typed_response(self, _system_prompt, _user_prompt, response_model, **kwargs):
        stage = kwargs["stage"]
        self.stages.append(stage)
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
            "plan_dashboard": [_dashboard_plan()],
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

    assert client.stages == ["understand", "plan_dashboard"]
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
            "plan_dashboard": [_dashboard_plan(), _dashboard_plan()],
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

    assert client.stages == ["understand", "plan_dashboard", "plan_dashboard"]
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
            "plan_dashboard": [_dashboard_plan(), _dashboard_plan()],
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

    assert client.stages == ["understand", "plan_dashboard", "plan_dashboard"]
    assert [widget.status for widget in saver.calls[0][2]] == ["ok", "ok", "ok"]


def test_dashboard_branch_reports_unavailable_saver():
    client = FakeClient(
        {
            "understand": [Understanding(intent="dashboard")],
            "plan_dashboard": [_dashboard_plan()],
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
