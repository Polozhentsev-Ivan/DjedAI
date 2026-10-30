"""Ветка дашборда: лимиты плана, рецепты, графики и превью (#78)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from djedai.agent.client import ModelResponse
from djedai.agent.dashboard import (
    WidgetExecution,
    build_widget_snapshot,
    chart_for_result,
    dashboard_output_result_ids,
    dashboard_preview_blocks,
    normalize_dashboard_plan,
    widget_layout,
    widget_tool_call_or_recipe,
)
from djedai.agent.diagnostics import RunDiagnostics
from djedai.agent.graph import plan_dashboard, route_after_plan_dashboard
from djedai.contracts.agent import (
    DashboardPlanResponse,
    DashboardStepPlan,
    DashboardWidgetPlan,
)
from djedai.contracts.chart import ChartSpec, ChartTemplate, XAxis, YMeasure
from djedai.contracts.dashboard import DashboardSnapshot, WidgetRecipe
from djedai.contracts.tool_result import (
    ToolError,
    ToolResult,
    ToolResultColumn,
    ToolResultData,
)
from djedai.execution import TurnCancelled, TurnTimeout


@pytest.mark.parametrize("error", [TurnTimeout, TurnCancelled])
def test_save_dashboard_propagates_budget_abort(error):
    from djedai.agent.graph import save_dashboard

    plan = DashboardPlanResponse(
        title="Sales", widgets=[widget(str(i), [step(f"s{i}", "SELECT 1")]) for i in range(3)]
    )
    executions = [WidgetExecution(widget=w, results={}) for w in plan.widgets]

    def saver(*_args):
        raise error()

    with pytest.raises(error):
        save_dashboard(
            {"dashboard_plan": plan, "dashboard_widgets": executions},
            {"configurable": {"dashboard_saver": saver}},
        )


def chart(
    chart_id: str = "ch_1",
    *,
    kind: str = "bar",
    x: str | None = "category",
    y: tuple[str, ...] = ("revenue",),
) -> ChartTemplate:
    return ChartTemplate(
        chart_id=chart_id,
        type=kind,
        title="Виджет",
        x=XAxis(field=x) if x is not None else None,
        y=[YMeasure(field=field) for field in y],
    )


def step(
    step_id: str,
    sql: str,
    *,
    source_step_id: str | None = None,
    extra: dict | None = None,
) -> DashboardStepPlan:
    args = {"sql": sql}
    if extra:
        args.update(extra)
    return DashboardStepPlan(
        step_id=step_id,
        tool="run_sql",
        args=args,
        source_step_id=source_step_id,
    )


def widget(
    title: str,
    steps: list[DashboardStepPlan],
    *,
    chart_template: ChartTemplate | None = None,
    output_step_id: str | None = None,
) -> DashboardWidgetPlan:
    return DashboardWidgetPlan(
        title=title,
        chart=chart_template or chart(),
        caption=f"Вывод: {title}",
        steps=steps,
        output_step_id=output_step_id,
    )


def result(
    result_id: str,
    *,
    columns: list[str | ToolResultColumn] | None = None,
    rows: list[list[object]] | None = None,
    status: str = "ok",
    error: ToolError | None = None,
) -> ToolResult:
    data = (
        ToolResultData(columns=columns, rows=rows, row_count=len(rows))
        if columns is not None and rows is not None
        else None
    )
    return ToolResult(
        result_id=result_id,
        status=status,
        tool="run_sql",
        data=data,
        error=error,
    )


def test_normalize_dashboard_plan_renumbers_steps_and_chart_ids():
    plan = DashboardPlanResponse(
        title="Продажи",
        description="Обзор",
        widgets=[
            widget(
                "По категориям",
                [
                    step("prep", "SELECT * FROM sales"),
                    step(
                        "agg",
                        "SELECT category, SUM(amount) AS revenue "
                        "FROM source_result GROUP BY category",
                        source_step_id="prep",
                    ),
                ],
                chart_template=chart("duplicate"),
                output_step_id="agg",
            ),
            widget(
                "Всего",
                [step("only", "SELECT SUM(amount) AS revenue FROM sales")],
                chart_template=chart("duplicate", kind="kpi", x=None),
            ),
            widget(
                "Заказы",
                [step("only", "SELECT COUNT(*) AS orders FROM sales")],
                chart_template=chart("ch_orders"),
            ),
        ],
    )

    normalized = normalize_dashboard_plan(plan, max_tool_calls=20)

    first, second, third = normalized.widgets
    assert [step.step_id for step in first.steps] == ["step_1", "step_2"]
    assert first.steps[1].source_step_id == "step_1"
    assert first.output_step_id == "step_2"
    assert first.chart.chart_id == "duplicate"
    assert second.chart.chart_id == "ch_dash_2"
    assert third.chart.chart_id == "ch_orders"
    # Исходный план не мутируется: временные ID модели остаются в нём.
    assert [step.step_id for step in plan.widgets[0].steps] == ["prep", "agg"]


@pytest.mark.parametrize(
    ("widgets", "max_tool_calls", "message"),
    [
        (
            [
                widget("Один", [step("s", "SELECT 1")]),
                widget("Два", [step("s", "SELECT 2")]),
            ],
            20,
            "от 3 до 8 виджетов",
        ),
        (
            [
                DashboardWidgetPlan.model_construct(
                    title="Один",
                    chart=chart(),
                    caption=None,
                    steps=[step(f"s{i}", f"SELECT {i}") for i in range(6)],
                    output_step_id=None,
                ),
                widget("Два", [step("s", "SELECT 2")]),
                widget("Три", [step("s", "SELECT 3")]),
            ],
            20,
            "от 1 до 5 шагов",
        ),
        (
            [
                widget("Один", [step("a", "SELECT 1"), step("b", "SELECT 2")]),
                widget("Два", [step("a", "SELECT 1"), step("b", "SELECT 2")]),
                widget("Три", [step("a", "SELECT 1"), step("b", "SELECT 2")]),
            ],
            5,
            "лимит ветки",
        ),
    ],
)
def test_normalize_dashboard_plan_enforces_separate_limits(widgets, max_tool_calls, message):
    # Контракт уже ограничивает 3–8 виджетов и 1–5 шагов; ветка проверяет
    # те же границы ещё раз, потому что план мог быть собран не моделью.
    plan = DashboardPlanResponse.model_construct(title="Дашборд", description=None, widgets=widgets)

    with pytest.raises(ValueError, match=message):
        normalize_dashboard_plan(plan, max_tool_calls=max_tool_calls)


@pytest.mark.parametrize(
    ("widget_plan", "message"),
    [
        (
            widget(
                "Цикл",
                [
                    step("a", "SELECT 1", source_step_id="b"),
                    step("b", "SELECT 2", source_step_id="a"),
                ],
            ),
            "неизвестный или последующий",
        ),
        (
            widget("Дубли", [step("a", "SELECT 1"), step("a", "SELECT 2")]),
            "повторяется шаг",
        ),
        (
            widget(
                "Старый ID",
                [step("a", "SELECT 1", extra={"source_result_id": "r_old"})],
            ),
            "source_step_id",
        ),
        (
            widget("Выход", [step("a", "SELECT 1")], output_step_id="missing"),
            "output_step_id",
        ),
    ],
)
def test_normalize_dashboard_plan_rejects_broken_dependencies(widget_plan, message):
    plan = DashboardPlanResponse(
        title="Дашборд",
        widgets=[
            widget_plan,
            widget("Второй", [step("a", "SELECT 2")]),
            widget("Третий", [step("a", "SELECT 3")]),
        ],
    )

    with pytest.raises(ValueError, match=message):
        normalize_dashboard_plan(plan, max_tool_calls=20)


def plan_with_first_step(first: DashboardStepPlan) -> DashboardPlanResponse:
    return DashboardPlanResponse(
        title="Дашборд",
        widgets=[
            widget("Первый", [first]),
            widget("Второй", [step("a", "SELECT 2")]),
            widget("Третий", [step("a", "SELECT 3")]),
        ],
    )


def test_normalize_dashboard_plan_checks_step_args_against_tool_schema():
    empty = DashboardStepPlan(step_id="a", tool="run_sql", args={})

    with pytest.raises(ValueError, match="Шаг 1 виджета 1 .* run_sql: sql"):
        normalize_dashboard_plan(plan_with_first_step(empty), max_tool_calls=20)

    valid = normalize_dashboard_plan(plan_with_first_step(step("a", "SELECT 1")), max_tool_calls=20)
    assert valid.widgets[0].steps[0].args == {"sql": "SELECT 1"}


def test_rejected_step_args_reveal_only_names():
    secret = "SELECT secret_value FROM private_table"
    bad = DashboardStepPlan(
        step_id="a", tool="run_sql", args={"sql": ["x"], "query": secret, secret: 1}
    )
    events: list[dict] = []
    diagnostics = RunDiagnostics(
        events.append, owner_id="o", chat_id="c", dataset_id="d", user_message_id="m"
    )

    with diagnostics.stage("plan_dashboard"), pytest.raises(ValueError) as error:
        normalize_dashboard_plan(plan_with_first_step(bad), max_tool_calls=20)

    assert "secret_value" not in str(error.value)
    assert error.value.__cause__ is None
    event = next(item for item in events if item["event"] == "invalid_plan_step")
    assert event["tool"] == "run_sql"
    assert event["step_keys"] == ["args", "step_id", "tool"]
    assert event["arg_keys"] == ["<other>", "query", "sql"]
    assert "secret_value" not in json.dumps(events)


def test_plan_dashboard_ends_with_error_block_without_replanning():
    bad = DashboardStepPlan(step_id="a", tool="run_sql", args={})
    client = MagicMock()
    client.generate_typed_response.return_value = ModelResponse(
        status="success", data=plan_with_first_step(bad)
    )

    update = plan_dashboard({"messages": []}, {"configurable": {"client": client}})

    client.generate_typed_response.assert_called_once()
    assert update["dashboard_plan"] is None
    assert [block.type for block in update["answer_blocks"]] == ["error"]
    assert "run_sql: sql" in update["answer_blocks"][0].markdown
    assert route_after_plan_dashboard(update) == "end"


def test_widget_definition_is_a_call_or_a_recipe_without_temporary_ids():
    single = widget("Всего", [step("only", "SELECT SUM(amount) AS revenue FROM sales")])
    tool_call, recipe = widget_tool_call_or_recipe(single)
    assert recipe is None
    assert tool_call is not None
    assert tool_call.tool == "run_sql"
    assert tool_call.params == {"sql": "SELECT SUM(amount) AS revenue FROM sales"}

    dependent = widget(
        "По категориям",
        [
            step("prep", "SELECT category, amount FROM sales"),
            step(
                "agg",
                "SELECT category, SUM(amount) AS revenue FROM source_result GROUP BY category",
                source_step_id="prep",
            ),
        ],
        output_step_id="agg",
    )
    tool_call, recipe = widget_tool_call_or_recipe(dependent)

    assert tool_call is None
    assert isinstance(recipe, WidgetRecipe)
    assert [item.step_id for item in recipe.steps] == ["prep", "agg"]
    assert recipe.steps[0].source_step_id is None
    assert recipe.steps[1].source_step_id == "prep"
    assert recipe.output_step_id == "agg"
    assert "source_result_id" not in recipe.steps[1].call.params
    assert recipe.steps[1].call.params["sql"].endswith("FROM source_result GROUP BY category")


def test_chart_for_result_keeps_valid_spec_and_repairs_invalid_one():
    template = chart("ch_valid")
    valid = chart_for_result(
        template,
        result("r_valid", columns=["category", "revenue"], rows=[["books", 50.0]]),
    )
    assert isinstance(valid, ChartSpec)
    assert valid.result_id == "r_valid"
    assert valid.chart_id == "ch_valid"
    assert valid.type == "bar"
    assert valid.x is not None and valid.x.field == "category"
    assert [measure.field for measure in valid.y] == ["revenue"]

    broken = chart("ch_broken", x="missing", y=("unknown",))
    repaired = chart_for_result(
        broken,
        result(
            "r_repaired",
            columns=["category", "revenue"],
            rows=[["books", 50.0], ["electronics", 300.0]],
        ),
    )
    assert repaired.result_id == "r_repaired"
    assert repaired.type == "bar"
    assert repaired.x is not None and repaired.x.field == "category"
    assert [measure.field for measure in repaired.y] == ["revenue"]


def test_chart_for_result_uses_kpi_table_and_declared_numeric_types():
    kpi = chart_for_result(
        chart("ch_kpi", x=None, y=("missing",)),
        result("r_kpi", columns=["revenue"], rows=[[420.0]]),
    )
    assert kpi.type == "kpi"
    assert kpi.x is None
    assert [measure.field for measure in kpi.y] == ["revenue"]

    table = chart_for_result(
        chart("ch_table", x="category", y=("category",)),
        result("r_table", columns=["category"], rows=[["books"], ["electronics"]]),
    )
    assert table.type == "table"
    assert table.x is None
    assert table.y == []

    declared = chart_for_result(
        chart("ch_declared"),
        result(
            "r_declared",
            columns=[
                ToolResultColumn(name="category", type="VARCHAR"),
                ToolResultColumn(name="revenue", type="DOUBLE"),
            ],
            rows=[["books", 50.0]],
        ),
    )
    assert declared.type == "bar"
    assert declared.x is not None and declared.x.field == "category"
    assert [measure.field for measure in declared.y] == ["revenue"]


def test_build_widget_snapshot_stores_success_recipe_and_error_outcome():
    dependent = widget(
        "По категориям",
        [
            step("prep", "SELECT category, amount FROM sales"),
            step(
                "agg",
                "SELECT category, SUM(amount) AS revenue FROM source_result GROUP BY category",
                source_step_id="prep",
            ),
        ],
        output_step_id="agg",
    )
    parent = result(
        "r_prep",
        columns=["category", "amount"],
        rows=[["books", 50.0], ["electronics", 300.0]],
    )
    output = result(
        "r_agg",
        columns=["category", "revenue"],
        rows=[["books", 50.0], ["electronics", 300.0]],
    )
    execution = WidgetExecution(widget=dependent, results={"prep": parent, "agg": output})

    snapshot = build_widget_snapshot(execution, 0)

    assert snapshot.widget_id.startswith("w_")
    assert snapshot.status == "ok"
    assert snapshot.result_id == "r_agg"
    assert snapshot.tool_call is None
    assert snapshot.recipe is not None
    assert snapshot.recipe.steps[1].source_step_id == "prep"
    assert isinstance(snapshot.chart, ChartSpec)
    assert snapshot.chart.result_id == "r_agg"
    assert snapshot.layout == widget_layout(0)

    failed = WidgetExecution(
        widget=dependent,
        results={"prep": parent},
        error=ToolError(code="dependency_failed", message="Шаг не выполнен"),
    )
    failed_snapshot = build_widget_snapshot(failed, 1)
    assert failed_snapshot.status == "error"
    assert failed_snapshot.result_id is None
    assert failed_snapshot.error is not None
    assert failed_snapshot.error.code == "dependency_failed"
    assert failed_snapshot.layout == widget_layout(1)

    missing = build_widget_snapshot(WidgetExecution(widget=dependent), 0)
    assert missing.status == "error"
    assert missing.error is not None
    assert missing.error.code == "missing_output"

    output_error = result(
        "r_error",
        status="error",
        error=ToolError(code="sql_error", message="Ошибка SQL"),
    )
    error_snapshot = build_widget_snapshot(
        WidgetExecution(widget=dependent, results={"agg": output_error}), 0
    )
    assert error_snapshot.status == "error"
    assert error_snapshot.error is not None
    assert error_snapshot.error.code == "sql_error"


def test_preview_contains_charts_and_link_and_focus_skips_failed_widgets():
    first = build_widget_snapshot(
        WidgetExecution(
            widget=widget(
                "Всего",
                [step("only", "SELECT SUM(amount) AS revenue FROM sales")],
                chart_template=chart("ch_total", kind="kpi", x=None),
            ),
            results={
                "only": result("r_total", columns=["revenue"], rows=[[420.0]]),
            },
        ),
        0,
    )
    second = build_widget_snapshot(
        WidgetExecution(
            widget=widget(
                "Ошибка",
                [step("only", "SELECT * FROM missing")],
            ),
            results={
                "only": result(
                    "r_missing",
                    status="error",
                    error=ToolError(code="sql_error", message="Нет таблицы"),
                )
            },
        ),
        1,
    )
    third = build_widget_snapshot(
        WidgetExecution(
            widget=widget(
                "По категориям",
                [step("only", "SELECT category, revenue FROM data")],
            ),
            results={
                "only": result(
                    "r_categories",
                    columns=["category", "revenue"],
                    rows=[["books", 120.0]],
                )
            },
        ),
        2,
    )
    snapshot = DashboardSnapshot(
        dashboard_id="d_test",
        dataset_id="ds_test",
        title="Продажи",
        description="Обзор",
        source_chat_id="chat_test",
        widgets=[first, second, third],
        created_at=datetime.now(UTC),
    )

    blocks = dashboard_preview_blocks(snapshot)

    assert [block.type for block in blocks] == [
        "text",
        "chart",
        "chart",
        "dashboard_link",
    ]
    assert blocks[0].markdown == "Дашборд «Продажи» сохранён. Виджетов: 3. Не удалось посчитать: 1."
    assert blocks[1].chart is not None
    assert blocks[1].chart["result_id"] == "r_total"
    assert blocks[2].chart is not None
    assert blocks[2].chart["result_id"] == "r_categories"
    assert blocks[3].dashboard_id == "d_test"
    assert blocks[3].title == "Продажи"
    assert dashboard_output_result_ids(snapshot) == ["r_total", "r_categories"]
