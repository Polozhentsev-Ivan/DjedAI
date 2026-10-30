"""Ветка дашборда: план 3–8 виджетов, их рецепты и снимки результатов (B3-8 / #78)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

from pydantic import ValidationError

from djedai.agent.diagnostics import invalid_plan_step
from djedai.analytics.registry import TOOLS_REGISTRY
from djedai.contracts.agent import (
    Block,
    DashboardPlanResponse,
    DashboardStepPlan,
    DashboardWidgetPlan,
)
from djedai.contracts.chart import ChartSpec, ChartTemplate, XAxis, YMeasure
from djedai.contracts.dashboard import (
    DashboardSnapshot,
    WidgetLayout,
    WidgetRecipe,
    WidgetRecipeStep,
    WidgetSnapshot,
    WidgetToolCall,
)
from djedai.contracts.tool_result import ToolError, ToolResult

DASHBOARD_MIN_WIDGETS = 3
DASHBOARD_MAX_WIDGETS = 8
DASHBOARD_MAX_WIDGET_STEPS = 5

_TOOL_PARAMETERS = {tool.name: tool.parameters_model for tool in TOOLS_REGISTRY}

_NUMERIC_TYPES = {
    "INT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "TINYINT",
    "SMALLINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
    "NUMERIC",
    "MONEY",
}


@dataclass
class WidgetExecution:
    """Выполнение одного виджета: результаты шагов и ошибка цепочки."""

    widget: DashboardWidgetPlan
    results: dict[str, ToolResult] = field(default_factory=dict)
    error: ToolError | None = None

    @property
    def output(self) -> ToolResult | None:
        step_id = self.widget.output_step_id or (
            self.widget.steps[-1].step_id if self.widget.steps else None
        )
        return self.results.get(step_id) if step_id else None

    @property
    def is_successful(self) -> bool:
        output = self.output
        return self.error is None and output is not None and output.status in ("ok", "empty")


def _check_step_args(step: DashboardStepPlan, *, widget_index: int, step_index: int) -> None:
    """Отклоняет шаг, который упал бы при выполнении; значения аргументов не попадают в текст."""
    parameters = _TOOL_PARAMETERS.get(step.tool)
    if parameters is None:
        return
    try:
        parameters.model_validate(step.args)
    except ValidationError as exc:
        invalid_plan_step(step.tool, step.model_fields_set, step.args)
        fields = sorted(
            {
                str(error["loc"][0])
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
                if error["loc"] and error["loc"][0] in parameters.model_fields
            }
        )
        detail = f": {', '.join(fields)}" if fields else ""
        raise ValueError(
            f"Шаг {step_index} виджета {widget_index} содержит неверные параметры "
            f"инструмента {step.tool}{detail}"
        ) from None


def normalize_dashboard_plan(
    plan: DashboardPlanResponse, *, max_tool_calls: int
) -> DashboardPlanResponse:
    """Проверяет лимиты ветки и заменяет временные ID шагов детерминированными.

    Модель может вернуть произвольные ``step_id``; рецепт хранит только ссылки
    между шагами. Поэтому перед выполнением шаги перенумеровываются, а зависимости
    и выходной шаг переводятся на новые ID. ``source_result_id`` в параметрах
    запрещён: зависимость выражается через ``source_step_id``.
    """
    if not DASHBOARD_MIN_WIDGETS <= len(plan.widgets) <= DASHBOARD_MAX_WIDGETS:
        raise ValueError(
            f"Дашборд должен содержать от {DASHBOARD_MIN_WIDGETS} до "
            f"{DASHBOARD_MAX_WIDGETS} виджетов"
        )

    total_steps = 0
    chart_ids: set[str] = set()
    widgets: list[DashboardWidgetPlan] = []

    for widget_index, widget in enumerate(plan.widgets, start=1):
        if not 1 <= len(widget.steps) <= DASHBOARD_MAX_WIDGET_STEPS:
            raise ValueError(
                f"Виджет «{widget.title}» должен содержать от 1 до "
                f"{DASHBOARD_MAX_WIDGET_STEPS} шагов"
            )

        step_ids: dict[str, str] = {}
        steps: list[DashboardStepPlan] = []
        for step_index, step in enumerate(widget.steps, start=1):
            if step.step_id in step_ids:
                raise ValueError(f"В виджете «{widget.title}» повторяется шаг {step.step_id}")
            if step.args.get("source_result_id") is not None:
                raise ValueError(
                    "Зависимость шага задаётся source_step_id, а не временным source_result_id"
                )
            if step.source_step_id is not None and step.source_step_id not in step_ids:
                raise ValueError(
                    f"Шаг {step.step_id} ссылается на неизвестный или последующий "
                    f"шаг {step.source_step_id}"
                )
            _check_step_args(step, widget_index=widget_index, step_index=step_index)
            new_id = f"step_{step_index}"
            steps.append(
                DashboardStepPlan(
                    step_id=new_id,
                    tool=step.tool,
                    args=dict(step.args),
                    source_step_id=(
                        step_ids[step.source_step_id] if step.source_step_id is not None else None
                    ),
                )
            )
            step_ids[step.step_id] = new_id

        output = widget.output_step_id or widget.steps[-1].step_id
        if output not in step_ids:
            raise ValueError(f"Виджет «{widget.title}» ссылается на несуществующий output_step_id")
        chart_id = widget.chart.chart_id
        if not chart_id or chart_id in chart_ids:
            chart_id = f"ch_dash_{widget_index}"
        while chart_id in chart_ids:
            chart_id = f"ch_dash_{widget_index}_{uuid4().hex[:4]}"
        chart_ids.add(chart_id)
        widgets.append(
            DashboardWidgetPlan(
                title=widget.title,
                chart=widget.chart.model_copy(update={"chart_id": chart_id}),
                caption=widget.caption,
                steps=steps,
                output_step_id=step_ids[output],
            )
        )
        total_steps += len(steps)

    if total_steps > max_tool_calls:
        raise ValueError(
            f"Дашборд содержит {total_steps} вызовов инструментов, а лимит ветки — {max_tool_calls}"
        )

    return DashboardPlanResponse(
        title=plan.title,
        description=plan.description,
        widgets=widgets,
    )


def widget_tool_call_or_recipe(
    widget: DashboardWidgetPlan,
) -> tuple[WidgetToolCall | None, WidgetRecipe | None]:
    """Возвращает ровно один способ перевыполнения: вызов или рецепт."""
    calls = [WidgetToolCall(tool=step.tool, params=dict(step.args)) for step in widget.steps]
    if len(widget.steps) == 1:
        return calls[0], None
    recipe = WidgetRecipe(
        steps=[
            WidgetRecipeStep(
                step_id=step.step_id,
                source_step_id=step.source_step_id,
                call=WidgetToolCall(tool=step.tool, params=dict(step.args)),
            )
            for step in widget.steps
        ],
        output_step_id=widget.output_step_id or widget.steps[-1].step_id,
    )
    return None, recipe


def widget_layout(index: int) -> WidgetLayout:
    """Раскладка по два виджета в ряд; фронтенд дополнительно адаптирует сетку."""
    return WidgetLayout(x=(index % 2) * 6, y=(index // 2) * 4, w=6, h=4)


def _column_name(column: object) -> str | None:
    if isinstance(column, str):
        return column
    name = getattr(column, "name", None)
    return name if isinstance(name, str) and name else None


def _column_is_numeric(result: ToolResult, index: int, column: object) -> bool:
    declared = getattr(column, "type", None)
    if isinstance(declared, str) and declared:
        return any(token in declared.upper() for token in _NUMERIC_TYPES)
    if result.data is None:
        return False
    for row in result.data.rows:
        if index >= len(row) or row[index] is None:
            continue
        value = row[index]
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    return False


def _fallback_chart(template: ChartTemplate, result: ToolResult) -> ChartSpec:
    """Строит безопасный график по колонкам результата, если спецификация не подошла."""
    columns = list(result.data.columns) if result.data is not None else []
    numeric: list[str] = []
    categories: list[str] = []
    for index, column in enumerate(columns):
        name = _column_name(column)
        if name is None:
            continue
        if _column_is_numeric(result, index, column):
            numeric.append(name)
        else:
            categories.append(name)

    base = template.model_dump(
        exclude={"x", "y", "type", "orientation", "stacked", "sort", "color"}
    )
    if result.data is not None and len(result.data.rows) == 1 and numeric:
        return ChartSpec(
            **base,
            type="kpi",
            y=[YMeasure(field=numeric[0])],
            result_id=result.result_id,
        )
    if categories and numeric:
        return ChartSpec(
            **base,
            type="bar",
            x=XAxis(field=categories[0]),
            y=[YMeasure(field=numeric[0])],
            result_id=result.result_id,
        )
    return ChartSpec(**base, type="table", result_id=result.result_id)


def chart_for_result(template: ChartTemplate, result: ToolResult) -> ChartSpec:
    """Проверяет график модели по фактическим колонкам и при необходимости чинит его."""
    # Lazy import: graph imports this module for the dashboard branch.
    from djedai.agent.graph import sanitize_charts

    candidate = ChartSpec(**template.model_dump(), result_id=result.result_id)
    valid = sanitize_charts([candidate], [result])
    return valid[0] if valid else _fallback_chart(template, result)


def build_widget_snapshot(execution: WidgetExecution, index: int) -> WidgetSnapshot:
    """Собирает сохраняемый снимок виджета из плана и результатов выполнения."""
    widget = execution.widget
    tool_call, recipe = widget_tool_call_or_recipe(widget)
    output = execution.output
    layout = widget_layout(index)

    if execution.error is not None or output is None or output.status == "error":
        error = execution.error or (
            output.error
            if output is not None and output.error is not None
            else ToolError(
                code="missing_output",
                message="Результат вычисления виджета не найден",
            )
        )
        return WidgetSnapshot(
            widget_id=f"w_{uuid4().hex[:12]}",
            tool_call=tool_call,
            recipe=recipe,
            chart=widget.chart,
            caption=widget.caption,
            layout=layout,
            result_id=None,
            status="error",
            error=error,
        )

    chart = chart_for_result(widget.chart, output)
    read = output.read
    return WidgetSnapshot(
        widget_id=f"w_{uuid4().hex[:12]}",
        tool_call=tool_call,
        recipe=recipe,
        chart=chart,
        caption=widget.caption,
        layout=layout,
        result_id=output.result_id,
        status=output.status,
        error=None,
        read_started_at=read.started_at if read is not None else None,
        read_finished_at=read.finished_at if read is not None else None,
    )


def dashboard_preview_blocks(snapshot: DashboardSnapshot) -> list[Block]:
    """Текстовое резюме, графики успешных виджетов и ссылка на дашборд."""
    failed = [widget for widget in snapshot.widgets if widget.status == "error"]
    text = f"Дашборд «{snapshot.title}» сохранён. Виджетов: {len(snapshot.widgets)}."
    if failed:
        text += f" Не удалось посчитать: {len(failed)}."
    blocks = [Block(type="text", markdown=text)]
    for widget in snapshot.widgets:
        if isinstance(widget.chart, ChartSpec):
            blocks.append(Block(type="chart", chart=widget.chart.model_dump(mode="json")))
    blocks.append(
        Block(
            type="dashboard_link",
            dashboard_id=snapshot.dashboard_id,
            title=snapshot.title,
        )
    )
    return blocks


def dashboard_output_result_ids(snapshot: DashboardSnapshot) -> list[str]:
    """Результаты, на которые должен смотреть следующий вопрос после дашборда."""
    return [
        widget.result_id
        for widget in snapshot.widgets
        if widget.result_id is not None and widget.status in ("ok", "empty")
    ]
