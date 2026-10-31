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
from djedai.contracts.tool_result import ToolResult, ToolResultData


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
    assert result["verification"].status == "corrected"


def _verification_config(responses, *, thread_id: str):
    client = MagicMock()
    client.generate_typed_response.side_effect = responses

    def dispatcher(tool: str, args: dict) -> ToolResult:
        return ToolResult(
            result_id="r_verify",
            status="ok",
            tool=tool,
            params=args,
            facts=[{"text": "Revenue: 1000 RUB"}],
        )

    return {
        "configurable": {
            "thread_id": thread_id,
            "client": client,
            "tool_dispatcher": dispatcher,
        }
    }


def _understand_and_plan():
    return [
        ModelResponse(status="success", data=Understanding(intent="data_question")),
        ModelResponse(
            status="success",
            data=PlanResponse(
                plan=[PlannedCall(tool="run_sql", args={"sql": "SELECT 1000 AS revenue"})]
            ),
        ),
    ]


def test_llm_cannot_confirm_a_number_missing_from_facts():
    """Даже is_valid=True не отменяет детерминированную проверку черновика."""
    graph = build_agent_graph()
    config = _verification_config(
        [
            *_understand_and_plan(),
            ModelResponse(
                status="success",
                data=ComposeResponse(
                    answer_blocks=[Block(type="text", markdown="Revenue: 2000 RUB")]
                ),
            ),
            ModelResponse(
                status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")
            ),
            ModelResponse(
                status="success",
                data=ComposeResponse(
                    answer_blocks=[Block(type="text", markdown="Revenue: 1000 RUB")]
                ),
            ),
            ModelResponse(
                status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")
            ),
        ],
        thread_id="test_verify_llm_cannot_override",
    )
    result = graph.invoke(
        {"chat_id": "c1", "messages": [Message(role="user", content="Show revenue")]},
        config=config,
    )

    assert result["compose_retries"] == 2
    assert [block.markdown for block in result["answer_blocks"]] == ["Revenue: 1000 RUB"]
    assert result["verification"].status == "verified"
    assert any(
        assertion.raw_text == "2000" for assertion in result["verification"].rejected_assertions
    )


def test_query_threshold_reaches_deterministic_verification():
    """Порог из вопроса и SQL не считается выдуманным числом в ответе."""
    graph = build_agent_graph()
    sql = "SELECT COUNT(*) AS n FROM orders WHERE duration_sec > 3600"
    client = MagicMock()
    client.generate_typed_response.side_effect = [
        ModelResponse(status="success", data=Understanding(intent="data_question")),
        ModelResponse(
            status="success",
            data=PlanResponse(plan=[PlannedCall(tool="run_sql", args={"sql": sql})]),
        ),
        ModelResponse(
            status="success",
            data=ComposeResponse(
                answer_blocks=[Block(type="text", markdown="Порог 3600 секунд; строк 3")]
            ),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]

    def dispatcher(tool: str, args: dict) -> ToolResult:
        return ToolResult(
            result_id="r_threshold",
            status="ok",
            tool=tool,
            sql=sql,
            params=args,
            data=ToolResultData(columns=["n"], rows=[[3]], row_count=1),
            facts=[{"text": "Количество строк: 3"}],
        )

    config = {
        "configurable": {
            "thread_id": "test_verify_query_constant",
            "client": client,
            "tool_dispatcher": dispatcher,
        }
    }
    result = graph.invoke(
        {
            "chat_id": "c1",
            "messages": [
                Message(role="user", content="Сколько заказов длится дольше 3600 секунд?")
            ],
        },
        config=config,
    )

    assert result["compose_retries"] == 1
    assert result["verification"].status == "verified"
    threshold = next(
        assertion for assertion in result["verification"].assertions if assertion.raw_text == "3600"
    )
    assert threshold.kind == "constant"
    assert threshold.status == "ignored"


def test_invalid_final_correction_is_replaced_by_an_explicit_error():
    """Исправление модели проходит ту же проверку; иначе ответ не публикуется."""
    graph = build_agent_graph()
    attempts = [
        ModelResponse(
            status="success",
            data=VerifyResponse(
                is_valid=False,
                issues=["2000 is not supported"],
                corrected_markdown="Revenue: 3000 RUB",
            ),
        )
        for _ in range(2)
    ]
    responses = [*_understand_and_plan()]
    for verify_response in attempts:
        responses.extend(
            [
                ModelResponse(
                    status="success",
                    data=ComposeResponse(
                        answer_blocks=[Block(type="text", markdown="Revenue: 2000 RUB")]
                    ),
                ),
                verify_response,
            ]
        )
    config = _verification_config(responses, thread_id="test_verify_invalid_correction")
    result = graph.invoke(
        {"chat_id": "c1", "messages": [Message(role="user", content="Show revenue")]},
        config=config,
    )

    assert result["compose_retries"] == 2
    assert result["answer_blocks"][-1].type == "error"
    assert "Не удалось подтвердить числа" in result["answer_blocks"][-1].markdown
    assert result["verification"].status == "failed"
    assert "invalid_correction" in {issue.code for issue in result["verification"].issues}
    assert result["verification"].rejected_assertions
    assert all("3000" not in block.markdown for block in result["answer_blocks"])
