from unittest.mock import MagicMock

import pytest

from djedai.agent import graph as graph_nodes
from djedai.agent.client import ModelResponse
from djedai.agent.graph import MODEL_ERROR_MARKDOWN, build_agent_graph
from djedai.agent.prompts import load_prompt
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
from djedai.contracts.tool_result import ToolResult


def test_graph_live_client_branches():
    """Тестирует ветки графа, которые вызываются при наличии 'client' в config (имитация LLM)."""
    graph = build_agent_graph()
    mock_client = MagicMock()

    mock_client.generate_typed_response.side_effect = [
        ModelResponse(
            status="success",
            data=Understanding(intent="visualize"),
        ),
        ModelResponse(
            status="success",
            data=PlanResponse(plan=[PlannedCall(tool="describe_dataset", args={})]),
        ),
        ModelResponse(
            status="success",
            data=VisualizeResponse(charts=[]),
        ),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="Mocked response")]),
        ),
        ModelResponse(
            status="success",
            data=VerifyResponse(is_valid=True, corrected_markdown="Mocked response"),
        ),
    ]

    config = {"configurable": {"thread_id": "test_llm_branches", "client": mock_client}}

    # The mocked plan calls describe_dataset, so provide a dispatcher that succeeds
    # and returns facts. Otherwise the graph enters its error-retry loop and never
    # reaches the compose branch that uses the client.
    def mock_dispatcher(tool: str, args: dict) -> ToolResult:
        return ToolResult(
            result_id="r_1",
            status="ok",
            facts=[{"text": "Dataset described successfully"}],
        )

    config["configurable"]["tool_dispatcher"] = mock_dispatcher

    initial_state = {
        "chat_id": "c1",
        "dataset_id": "ds1",
        "messages": [Message(role="user", content="Show graph")],
    }

    # Прогоняем граф с замоканным "live" клиентом
    result = graph.invoke(initial_state, config=config)

    # Проверяем, что граф пошел по веткам клиента и использовал его ответы
    assert result["understanding"].intent == "visualize"
    assert len(result["plan"]) == 1
    assert result["plan"][0].tool == "describe_dataset"
    assert len(result["answer_blocks"]) == 1
    assert result["answer_blocks"][0].markdown == "Mocked response"


def test_missing_prompt_file():
    """Проверяет фоллбэк, если файл промпта не найден."""
    # Пытаемся загрузить несуществующий промпт
    content = load_prompt("non_existent_prompt_name")
    assert content == "Промпт не найден."


def test_graph_api_error_fallback():
    """Проверяет, что ошибка модели явно передается пользователю без подмены на успешный ответ."""
    graph = build_agent_graph()
    mock_client = MagicMock()

    mock_client.generate_typed_response.return_value = ModelResponse(
        status="error", error_type="SERVER_ERROR", message="401 Unauthorized"
    )

    config = {"configurable": {"thread_id": "test_error_fallback", "client": mock_client}}
    initial_state = {
        "chat_id": "c1",
        "messages": [Message(role="user", content="Show revenue")],
    }

    result = graph.invoke(initial_state, config=config)

    assert result["understanding"].intent == "ambiguous"
    assert len(result["answer_blocks"]) == 1
    assert result["answer_blocks"][0].type == "error"
    assert result["answer_blocks"][0].markdown == MODEL_ERROR_MARKDOWN
    assert "401 Unauthorized" not in result["answer_blocks"][0].markdown


@pytest.mark.parametrize("node", ["understand", "plan", "plan_dashboard", "compose", "verify"])
@pytest.mark.parametrize("error_type", ["SERVER_ERROR", "TIMEOUT", "INVALID_JSON"])
def test_terminal_model_error_code_is_preserved_by_each_node(node, error_type):
    client = MagicMock()
    client.generate_typed_response.return_value = ModelResponse(
        status="error", error_type=error_type
    )
    state = {
        "messages": [Message(role="user", content="Show job duration")],
        "results": [
            ToolResult(result_id="r1", status="ok", facts=[{"text": "Duration available"}])
        ],
        "answer_blocks": [Block(type="text", markdown="Duration available")],
    }
    update = getattr(graph_nodes, node)(state, {"configurable": {"client": client}})
    assert update["terminal_error"] == error_type
    assert any(block.type == "error" for block in update["answer_blocks"])


def test_terminal_error_is_reset_on_next_chat_turn():
    graph = build_agent_graph()
    client = MagicMock()
    client.generate_typed_response.return_value = ModelResponse(
        status="error", error_type="TIMEOUT"
    )
    config = {"configurable": {"thread_id": "recover_after_failure", "client": client}}
    first = graph.invoke({"messages": [Message(role="user", content="Show job duration")]}, config)
    assert first["terminal_error"] == "TIMEOUT"
    client.generate_typed_response.return_value = ModelResponse(
        status="success", data=Understanding(intent="off_topic")
    )
    second = graph.invoke({"messages": [Message(role="user", content="Tell me a joke")]}, config)
    assert second["terminal_error"] is None
    assert second["understanding"].intent == "off_topic"
