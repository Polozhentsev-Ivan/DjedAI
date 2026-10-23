from typing import get_args
from unittest.mock import MagicMock

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.contracts.agent import (
    ComposeResponse,
    Message,
    PlannedCall,
    PlanResponse,
    Understanding,
    VerifyResponse,
    VisualizeResponse,
)
from djedai.contracts.chart import ChartSpec, ChartType, XAxis, YMeasure
from djedai.contracts.tool_result import ChartHint, ToolResult, ToolResultData


def _saved_result() -> ToolResult:
    return ToolResult(
        result_id="r_1",
        status="ok",
        tool="value_counts",
        data=ToolResultData(columns=["category", "value"], rows=[["A", 10], ["B", 20]]),
        chart_hint=ChartHint(type="bar", x="category", y=["value"]),
    )


def _chart(chart_type: str, chart_id: str = "ch_123") -> ChartSpec:
    return ChartSpec(
        chart_id=chart_id,
        result_id="r_1",
        type=chart_type,
        title=f"Test {chart_type}",
        x=XAxis(field="category"),
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
        # No text block is composed, so verification has nothing to check and stays silent.
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
