from unittest.mock import MagicMock

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph, compose, verify
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
    """Безопасное исправление верификатора публикуется без повторного compose."""
    graph = build_agent_graph()
    mock_client = MagicMock()

    # Четырёх ответов достаточно: повторный compose не вызывается.
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

    # Безопасное исправление не надо выбрасывать ради нового compose.
    assert result["compose_retries"] == 1
    # Проверяем, что ответ в итоге был заменен на исправленный (вырезанный) верификатором
    assert result["is_verified"] is True
    assert len(result["answer_blocks"]) == 1
    assert result["answer_blocks"][0].markdown == "Revenue: Unknown"
    assert result["verification"].status == "corrected"
    assert mock_client.generate_typed_response.call_count == 4


def test_first_invalid_draft_uses_a_verified_correction_without_recompose():
    config = _verification_config(
        [
            *_understand_and_plan(),
            ModelResponse(
                status="success",
                data=ComposeResponse(
                    answer_blocks=[Block(type="text", markdown="Revenue: 1000 USD")]
                ),
            ),
            ModelResponse(
                status="success",
                data=VerifyResponse(is_valid=False, corrected_markdown="Revenue: 1000 RUB"),
            ),
        ],
        thread_id="first_verified_correction",
    )
    result = build_agent_graph().invoke(
        {"chat_id": "c1", "messages": [Message(role="user", content="Show revenue")]},
        config=config,
    )
    assert result["compose_retries"] == 1
    assert result["verification"].status == "corrected"
    assert [block.markdown for block in result["answer_blocks"]] == ["Revenue: 1000 RUB"]
    assert config["configurable"]["client"].generate_typed_response.call_count == 4


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


def test_retry_and_final_verifier_receive_the_exact_rejected_claim():
    draft = "Всего в данных 500 001 запусков."
    corrected = "Всего 500 000 строк."
    state = {
        "messages": [Message(role="user", content="Что передо мной?")],
        "results": [
            ToolResult(
                result_id="r_rows",
                tool="describe_dataset",
                status="ok",
                facts=[{"text": "Таблица jobs: 500000 строк", "values": {"row_count": 500000}}],
            )
        ],
        "answer_blocks": [Block(type="text", markdown=draft)],
        "compose_retries": 2,
    }
    rejected = verify(state, {"configurable": {}})["verification"]
    assert rejected.status == "failed"
    assert rejected.rejected_assertions[0].reason_code == "number_not_found"
    client = MagicMock()

    def respond(system_prompt, user_prompt, schema, **kwargs):
        assert "Всего в данных 500 001 запусков" in user_prompt
        if schema is ComposeResponse:
            data = ComposeResponse(answer_blocks=[Block(type="text", markdown=corrected)])
        else:
            data = VerifyResponse(is_valid=False, corrected_markdown=corrected)
        return ModelResponse(status="success", data=data)

    client.generate_typed_response.side_effect = respond
    config = {"configurable": {"client": client}}
    retry = compose({**state, "verification": rejected}, config)
    assert retry["answer_blocks"][0].markdown == corrected
    final = verify(state, config)
    assert final["verification"].status == "corrected"
    assert final["answer_blocks"][0].markdown == corrected
    assert final["verification"].rejected_assertions[0].reason_code == "number_not_found"


def test_output_limit_keeps_an_answer_the_code_confirmed_but_not_an_unconfirmed_one():
    results = [
        ToolResult(
            result_id="r_rows",
            tool="describe_dataset",
            status="ok",
            facts=[{"text": "Таблица jobs: 500000 строк", "values": {"row_count": 500000}}],
        )
    ]
    client = MagicMock()
    client.generate_typed_response.return_value = ModelResponse(
        status="error", error_type="OUTPUT_LIMIT"
    )
    config = {"configurable": {"client": client}}

    def state(text):
        return {
            "messages": [Message(role="user", content="Сколько строк?")],
            "results": results,
            "answer_blocks": [Block(type="text", markdown=text)],
            "compose_retries": 2,
        }

    kept = verify(state("Всего 500 000 строк."), config)
    assert kept["verification"].status == "verified"
    assert "answer_blocks" not in kept
    retried = verify({**state("Всего 500 001 запусков."), "compose_retries": 1}, config)
    assert retried["is_verified"] is False
    assert "terminal_error" not in retried
    dropped = verify(state("Всего 500 001 запусков."), config)
    assert dropped["verification"].status == "failed"
    assert dropped["terminal_error"] == "OUTPUT_LIMIT"
