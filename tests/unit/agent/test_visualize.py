from typing import get_args
from unittest.mock import MagicMock

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.contracts.agent import (
    Block,
    ComposeResponse,
    Message,
    PlannedCall,
    PlanResponse,
    Understanding,
    VerifyResponse,
    VisualizeResponse,
)
from djedai.contracts.chart import ChartSpec, ChartType, XAxis, YMeasure
from djedai.contracts.tool_result import ChartHint, Fact, ToolError, ToolResult, ToolResultData


def _saved_result() -> ToolResult:
    return ToolResult(
        result_id="r_1",
        status="ok",
        tool="value_counts",
        data=ToolResultData(columns=["category", "value"], rows=[["A", 10], ["B", 20]]),
        chart_hint=ChartHint(type="bar", x="category", y=["value"]),
    )


def _chart(chart_type: str, chart_id: str = "ch_123") -> ChartSpec:
    # Scatter plots both coordinates on value axes, so its X field must be numeric.
    x = XAxis(field="value", kind="number") if chart_type == "scatter" else XAxis(field="category")
    return ChartSpec(
        chart_id=chart_id,
        result_id="r_1",
        type=chart_type,
        title=f"Test {chart_type}",
        x=x,
        y=[YMeasure(field="value")],
    )


def _run_modify(thread_id: str, response: VisualizeResponse, dispatcher=None):
    graph = build_agent_graph()
    mock_client = MagicMock()
    mock_client.generate_typed_response.side_effect = [
        ModelResponse(
            status="success",
            data=Understanding(intent="modify_previous", references=["r_1"]),
        ),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(status="success", data=response),
        ModelResponse(status="success", data=ComposeResponse(answer_blocks=[])),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]

    configurable = {"thread_id": thread_id, "client": mock_client}
    if dispatcher is not None:
        configurable["tool_dispatcher"] = dispatcher

    state = {
        "chat_id": "c1",
        "dataset_id": "ds_1",
        "messages": [Message(role="user", content="Show as a bar chart")],
        # Pre-existing historical results
        "results": [_saved_result()],
    }
    return graph.invoke(state, config={"configurable": configurable})


def test_visualize_modifies_previous():
    """Проверяет сценарий изменения графиков без повторного запуска инструмента."""
    graph = build_agent_graph()
    mock_client = MagicMock()

    chart_spec = ChartSpec(
        chart_id="ch_123",
        result_id="r_1",
        type="bar",
        title="Test Bar",
        x=XAxis(field="category"),
        y=[YMeasure(field="value")],
    )

    # 1. understand: modify_previous (referencing result r_1)
    # 2. plan: empty (because no new data is needed)
    # 3. visualize: generate ChartSpec
    # 4. compose: simple block
    # 5. verify: pass
    mock_client.generate_typed_response.side_effect = [
        ModelResponse(
            status="success",
            data=Understanding(intent="modify_previous", references=["r_1"]),
        ),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(status="success", data=VisualizeResponse(charts=[chart_spec])),
        ModelResponse(status="success", data=ComposeResponse(answer_blocks=[])),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]

    config = {"configurable": {"thread_id": "test_vis", "client": mock_client}}
    initial_state = {
        "chat_id": "c1",
        "dataset_id": "ds_1",
        "messages": [Message(role="user", content="Show as a bar chart")],
        # Pre-existing historical results
        "results": [
            ToolResult(
                result_id="r_1",
                status="ok",
                tool="value_counts",
                data=ToolResultData(columns=["category", "value"], rows=[["A", 10], ["B", 20]]),
                chart_hint=ChartHint(type="bar", x="category", y=["value"]),
            )
        ],
    }

    result = graph.invoke(initial_state, config=config)

    # Validate Visualize generation
    assert len(result["charts"]) == 1
    assert result["charts"][0].result_id == "r_1"
    assert result["charts"][0].type == "bar"

    # Validate Compose properly appended it
    assert len(result["answer_blocks"]) == 1
    assert result["answer_blocks"][0].type == "chart"
    assert result["answer_blocks"][0].chart["result_id"] == "r_1"


def test_visualize_change_type_reuses_saved_result_without_recalculation():
    """Смена типа графика не запускает инструменты и не создаёт данные заново."""
    calls = {"count": 0}

    def dispatcher(tool: str, args: dict) -> ToolResult:
        calls["count"] += 1
        raise AssertionError("recalculation must not happen when changing the view")

    result = _run_modify("test_vis_retype", VisualizeResponse(charts=[_chart("line")]), dispatcher)

    assert calls["count"] == 0
    assert result["results"] == []
    chart = result["charts"][0]
    assert chart.type == "line"
    assert chart.result_id == "r_1"
    assert chart.x is not None and chart.x.field == "category"
    assert [measure.field for measure in chart.y] == ["value"]
    assert chart.title


def test_visualize_supports_every_agreed_chart_type():
    """Все согласованные типы графиков проходят через агента без потери полей."""
    types = get_args(ChartType)
    assert set(types) == {
        "bar",
        "line",
        "area",
        "scatter",
        "pie",
        "histogram",
        "heatmap",
        "box",
        "table",
        "kpi",
    }

    charts = [_chart(chart_type, chart_id=f"ch_{chart_type}") for chart_type in types]
    result = _run_modify("test_vis_types", VisualizeResponse(charts=charts))

    assert [chart.type for chart in result["charts"]] == list(types)
    assert {chart.result_id for chart in result["charts"]} == {"r_1"}
    assert len(result["answer_blocks"]) == len(types)


def test_visualize_reuses_result_from_previous_turn_in_the_same_chat():
    """Второй ход чата меняет тип графика по сохранённому результату первого хода."""
    graph = build_agent_graph()
    client = MagicMock()
    executed = {"count": 0}

    def dispatcher(tool: str, args: dict) -> ToolResult:
        executed["count"] += 1
        return _saved_result()

    client.generate_typed_response.side_effect = [
        ModelResponse(status="success", data=Understanding(intent="visualize")),
        ModelResponse(
            status="success",
            data=PlanResponse(plan=[PlannedCall(tool="value_counts", args={})]),
        ),
        ModelResponse(status="success", data=VisualizeResponse(charts=[_chart("bar")])),
        ModelResponse(status="success", data=ComposeResponse(answer_blocks=[])),
        # Верификатор теперь читает заголовки (Title) графиков, поэтому его нужно замокать:
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
        # --- Second turn starts here ---
        ModelResponse(
            status="success",
            data=Understanding(intent="modify_previous", references=["r_1"]),
        ),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(
            status="success",
            data=VisualizeResponse(charts=[_chart("line", chart_id="ch_line")]),
        ),
        ModelResponse(status="success", data=ComposeResponse(answer_blocks=[])),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]
    config = {
        "configurable": {
            "thread_id": "test_vis_two_turns",
            "client": client,
            "tool_dispatcher": dispatcher,
        }
    }

    first = graph.invoke(
        {
            "chat_id": "c1",
            "dataset_id": "ds_1",
            "messages": [Message(role="user", content="Show revenue by category")],
        },
        config=config,
    )
    assert first["charts"][0].type == "bar"

    second = graph.invoke(
        {
            "chat_id": "c1",
            "dataset_id": "ds_1",
            "messages": [
                Message(role="user", content="Show revenue by category"),
                Message(role="assistant", content="Here is the chart"),
                Message(role="user", content="Make it a line chart"),
            ],
        },
        config=config,
    )

    # The view was changed without a second calculation.
    assert executed["count"] == 1
    assert second["results"] == []
    assert second["charts"][0].type == "line"
    assert second["charts"][0].result_id == "r_1"
    assert second["answer_blocks"][0].chart["result_id"] == "r_1"


def test_visualize_drops_charts_referencing_unknown_results():
    """График по выдуманному result_id не попадает в ответ."""
    invented = _chart("bar", chart_id="ch_ghost").model_copy(update={"result_id": "r_ghost"})

    result = _run_modify(
        "test_vis_ghost", VisualizeResponse(charts=[invented, _chart("bar", chart_id="ch_ok")])
    )

    assert [chart.chart_id for chart in result["charts"]] == ["ch_ok"]

    assert all(block.chart["result_id"] == "r_1" for block in result["answer_blocks"])


def test_visualize_rejects_string_column_on_numeric_axis():
    """Категориальную колонку нельзя откладывать по числовой оси Y."""
    mixed = ChartSpec(
        chart_id="ch_mixed",
        result_id="r_1",
        type="bar",
        title="Сумма по категории",
        x=XAxis(field="value"),
        y=[YMeasure(field="category")],
    )

    result = _run_modify("test_vis_string_axis", VisualizeResponse(charts=[mixed]))

    assert result["charts"] == []
    assert result["answer_blocks"] == []


def test_visualize_rejects_scatter_with_categorical_x():
    """Scatter требует числовую ось X: строковую категорию рендерер не построит."""
    scatter = ChartSpec(
        chart_id="ch_scatter_text",
        result_id="r_1",
        type="scatter",
        title="Точки",
        x=XAxis(field="category"),
        y=[YMeasure(field="value")],
    )

    result = _run_modify("test_vis_scatter_text", VisualizeResponse(charts=[scatter]))

    assert result["charts"] == []
    assert result["answer_blocks"] == []


def test_visualize_rejects_number_kind_on_categorical_column():
    """x.kind=number для строковой колонки даёт несовместимую спецификацию."""
    wrong_kind = ChartSpec(
        chart_id="ch_wrong_kind",
        result_id="r_1",
        type="bar",
        title="Числовая категория",
        x=XAxis(field="category", kind="number"),
        y=[YMeasure(field="value")],
    )

    result = _run_modify("test_vis_wrong_kind", VisualizeResponse(charts=[wrong_kind]))

    assert result["charts"] == []
    assert result["answer_blocks"] == []


def test_compose_uses_saved_facts_when_plan_is_empty():
    """Смена представления объясняет сохранённые данные без нового расчёта."""
    graph = build_agent_graph()
    client = MagicMock()
    client.generate_typed_response.side_effect = [
        ModelResponse(
            status="success",
            data=Understanding(intent="modify_previous", references=["r_1"]),
        ),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(status="success", data=VisualizeResponse(charts=[_chart("bar")])),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="A = 10, B = 20")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]

    result = graph.invoke(
        {
            "chat_id": "c1",
            "dataset_id": "ds_1",
            "messages": [Message(role="user", content="Смени вид и объясни те же суммы")],
            "results": [_saved_result()],
        },
        config={"configurable": {"thread_id": "test_compose_saved", "client": client}},
    )

    assert result["charts"][0].result_id == "r_1"
    assert result["answer_blocks"][-1].chart["result_id"] == "r_1"
    compose_prompt = client.generate_typed_response.call_args_list[3].args[0]
    assert "category" in compose_prompt
    assert "A" in compose_prompt
    assert "20" in compose_prompt


def test_compose_falls_back_to_saved_history_when_no_chart_is_built():
    """Без нового плана и без графика компоновщик всё равно получает факты."""
    graph = build_agent_graph()
    client = MagicMock()
    client.generate_typed_response.side_effect = [
        ModelResponse(
            status="success",
            data=Understanding(intent="modify_previous", references=["r_1"]),
        ),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(status="success", data=VisualizeResponse(charts=[])),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="A = 10, B = 20")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]

    graph.invoke(
        {
            "chat_id": "c1",
            "dataset_id": "ds_1",
            "messages": [Message(role="user", content="Объясни те же суммы")],
            "results": [_saved_result()],
        },
        config={"configurable": {"thread_id": "test_compose_history", "client": client}},
    )

    compose_prompt = client.generate_typed_response.call_args_list[3].args[0]
    assert "category" in compose_prompt
    assert "20" in compose_prompt


def test_partial_plan_rerun_keeps_successful_results_for_compose_and_verify():
    """Повторный план после сбоя не теряет уже успешный расчёт."""
    graph = build_agent_graph()
    client = MagicMock()
    count_calls = {"total": 0}

    def dispatcher(tool: str, args: dict) -> ToolResult:
        if tool == "sum_amount":
            return ToolResult(
                result_id="r_sum",
                status="ok",
                tool=tool,
                facts=[Fact(text="SUM(amount) = 30")],
            )
        count_calls["total"] += 1
        if count_calls["total"] == 1:
            return ToolResult(
                result_id="r_count_failed",
                status="error",
                tool=tool,
                error=ToolError(code="timeout", message="Запрос не выполнен"),
            )
        return ToolResult(
            result_id="r_count",
            status="ok",
            tool=tool,
            facts=[Fact(text="COUNT(*) = 2")],
        )

    client.generate_typed_response.side_effect = [
        ModelResponse(status="success", data=Understanding(intent="data_question")),
        ModelResponse(
            status="success",
            data=PlanResponse(
                plan=[
                    PlannedCall(tool="sum_amount", args={}),
                    PlannedCall(tool="count_rows", args={}),
                ]
            ),
        ),
        ModelResponse(
            status="success",
            data=PlanResponse(plan=[PlannedCall(tool="count_rows", args={})]),
        ),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="Сумма 30, строк 2")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]

    result = graph.invoke(
        {
            "chat_id": "c1",
            "dataset_id": "ds_1",
            "messages": [Message(role="user", content="Сумма и количество строк")],
        },
        config={
            "configurable": {
                "thread_id": "test_partial_rerun",
                "client": client,
                "tool_dispatcher": dispatcher,
            }
        },
    )

    assert [tool_result.result_id for tool_result in result["results"]] == [
        "r_sum",
        "r_count_failed",
        "r_count",
    ]

    calls = client.generate_typed_response.call_args_list
    compose_prompt = calls[3].args[0]
    verify_prompt = calls[4].args[0]
    for prompt in (compose_prompt, verify_prompt):
        assert "SUM(amount) = 30" in prompt
        assert "COUNT(*) = 2" in prompt
