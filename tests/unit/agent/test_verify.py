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
)
from djedai.contracts.tool_result import ToolResult


def test_compose_verify_loop_and_strip():
    """Проверяет цикл верификации:
    генерация -> ошибка -> 1 ретрай -> вырезание недостоверных чисел."""
    graph = build_agent_graph()
    mock_client = MagicMock()

    # Имитируем последовательность из 6 вызовов API за один прогон:
    # 1. understand -> 2. plan -> 3. compose (fail) ->
    # 4. verify (fail) -> 5. compose retry -> 6. verify (strip)
    mock_client.generate_typed_response.side_effect = [
        ModelResponse(status="success", data=Understanding(intent="data_question")),
        ModelResponse(
            status="success",
            data=PlanResponse(
                plan=[PlannedCall(tool="run_sql", args={"sql": "SELECT 1000 AS revenue"})]
            ),
        ),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="Revenue: 1000 RUB")]),
        ),
        ModelResponse(
            status="success",
            data=VerifyResponse(
                is_valid=False,
                issues=["1000 is not in facts"],
                corrected_markdown="Revenue: Unknown",
            ),
        ),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="Revenue: 1000 RUB")]),
        ),
        ModelResponse(
            status="success",
            data=VerifyResponse(
                is_valid=False,
                issues=["1000 is not in facts"],
                corrected_markdown="Revenue is strictly unknown.",
            ),
        ),
    ]

    # Verification only runs when there are facts to check, so the planned tool
    # must return a successful result containing the verified value.
    def mock_dispatcher(tool: str, args: dict) -> ToolResult:
        return ToolResult(
            result_id="r_verify",
            status="ok",
            tool=tool,
            params=args,
            facts=[{"text": "Revenue: 1000 RUB"}],
        )

    config = {
        "configurable": {
            "thread_id": "test_verify",
            "client": mock_client,
            "tool_dispatcher": mock_dispatcher,
        }
    }

    initial_state = {
        "chat_id": "c1",
        "messages": [Message(role="user", content="Show revenue")],
    }

    result = graph.invoke(initial_state, config=config)

    # Проверяем, что граф сделал ровно 1 ретрай (2 попытки генерации compose)
    assert result["compose_retries"] == 2
    # Проверяем, что ответ в итоге был заменен на исправленный (вырезанный) верификатором
    assert result["is_verified"] is True
    assert len(result["answer_blocks"]) == 1
    assert result["answer_blocks"][0].markdown == "Revenue is strictly unknown."
