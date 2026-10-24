"""Follow-up сценарии агента: summary, focus_result_ids и ссылки на прошлые блоки."""

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
from djedai.contracts.chart import ChartSpec, XAxis, YMeasure
from djedai.contracts.tool_result import ChartHint, Fact, ToolResult, ToolResultData


def _result(result_id: str, fact: str) -> ToolResult:
    return ToolResult(
        result_id=result_id,
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["category", "value"], rows=[["A", 10]]),
        facts=[Fact(text=fact, kind="summary")],
        chart_hint=ChartHint(type="bar", x="category", y=["value"]),
    )


def test_understand_receives_summary_recent_turns_and_focused_results() -> None:
    graph = build_agent_graph()
    client = MagicMock()
    client.generate_typed_response.side_effect = [
        ModelResponse(status="success", data=Understanding(intent="data_question")),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="Готово")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]
    config = {"configurable": {"thread_id": "memory_prompt", "client": client}}
    state = {
        "chat_id": "c_memory",
        "dataset_id": "ds_memory",
        "messages": [
            Message(role="user", content="Посчитай выручку за январь"),
            Message(role="assistant", content="Выручка составила 1200"),
            Message(role="user", content="А за тот же период по регионам?"),
        ],
        "history_summary": "Ранее обсуждали выручку за январь.",
        "focus_result_ids": ["r_first", "r_second"],
        "history_results": [_result("r_first", "Первый факт"), _result("r_second", "Второй факт")],
        "previous_blocks": [
            Block(type="text", markdown="Первый пункт"),
            Block(type="table", result_id="r_second"),
        ],
    }

    result = graph.invoke(state, config=config)

    system_prompt, question, response_model = client.generate_typed_response.call_args_list[0].args
    assert response_model is Understanding
    assert "Ранее обсуждали выручку за январь." in system_prompt
    assert "Пользователь: Посчитай выручку за январь" in system_prompt
    assert "Ассистент: Выручка составила 1200" in system_prompt
    assert "1. result_id=r_first" in system_prompt
    assert "2. result_id=r_second" in system_prompt
    assert "2. table: result_id=r_second" in system_prompt
    assert question == "А за тот же период по регионам?"
    plan_prompt = client.generate_typed_response.call_args_list[1].args[0]
    assert "Ранее обсуждали выручку за январь." in plan_prompt
    assert "1. result_id=r_first" in plan_prompt
    # Без новых расчётов и ссылок разговор остаётся на прежнем фокусе.
    assert result["focus_result_ids"] == ["r_first", "r_second"]


def test_focus_tracks_a_calculation_and_survives_a_view_change() -> None:
    graph = build_agent_graph()
    client = MagicMock()
    chart = ChartSpec(
        chart_id="ch_line",
        result_id="r_calc",
        type="line",
        title="Динамика",
        x=XAxis(field="category"),
        y=[YMeasure(field="value")],
    )
    client.generate_typed_response.side_effect = [
        # Первый ход: расчёт.
        ModelResponse(status="success", data=Understanding(intent="data_question")),
        ModelResponse(
            status="success",
            data=PlanResponse(plan=[PlannedCall(tool="run_sql", args={"sql": "SELECT 1"})]),
        ),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="1200")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
        # Второй ход: меняем только представление.
        ModelResponse(
            status="success",
            data=Understanding(intent="modify_previous", references=["r_calc"]),
        ),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(status="success", data=VisualizeResponse(charts=[chart])),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="График")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]
    calls = {"count": 0}

    def dispatcher(_tool: str, _args: dict) -> ToolResult:
        calls["count"] += 1
        return _result("r_calc", "Выручка 1200")

    config = {
        "configurable": {
            "thread_id": "memory_focus",
            "client": client,
            "tool_dispatcher": dispatcher,
        }
    }
    first = graph.invoke(
        {
            "chat_id": "c_focus",
            "dataset_id": "ds_focus",
            "messages": [Message(role="user", content="Посчитай выручку")],
        },
        config=config,
    )
    second = graph.invoke(
        {
            "chat_id": "c_focus",
            "dataset_id": "ds_focus",
            "messages": [
                Message(role="user", content="Посчитай выручку"),
                Message(role="assistant", content="Выручка 1200"),
                Message(role="user", content="Покажи это линейным графиком"),
            ],
        },
        config=config,
    )

    assert first["focus_result_ids"] == ["r_calc"]
    assert calls["count"] == 1
    assert second["results"] == []
    assert second["focus_result_ids"] == ["r_calc"]
    assert second["charts"][0].result_id == "r_calc"


def test_details_about_the_second_item_reuse_its_saved_result() -> None:
    graph = build_agent_graph()
    client = MagicMock()
    client.generate_typed_response.side_effect = [
        ModelResponse(
            status="success",
            data=Understanding(intent="modify_previous", references=["r_second"]),
        ),
        ModelResponse(status="success", data=PlanResponse(plan=[])),
        ModelResponse(status="success", data=VisualizeResponse(charts=[])),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="Подробности")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]
    config = {"configurable": {"thread_id": "memory_details", "client": client}}
    result = graph.invoke(
        {
            "chat_id": "c_details",
            "dataset_id": "ds_details",
            "messages": [
                Message(role="user", content="Сравни два сегмента"),
                Message(role="assistant", content="Сравнение готово"),
                Message(role="user", content="Подробнее про второй пункт"),
            ],
            "focus_result_ids": ["r_first", "r_second"],
            "history_results": [
                _result("r_first", "Первый факт"),
                _result("r_second", "Второй факт"),
            ],
            "previous_blocks": [
                Block(type="table", result_id="r_first"),
                Block(type="table", result_id="r_second"),
            ],
        },
        config=config,
    )

    compose_prompt = client.generate_typed_response.call_args_list[3].args[0]
    assert "Второй факт" in compose_prompt
    assert "Первый факт" not in compose_prompt
    assert result["results"] == []
    assert result["focus_result_ids"] == ["r_second"]
