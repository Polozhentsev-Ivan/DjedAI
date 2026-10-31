from unittest.mock import MagicMock

import duckdb
import pytest

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph, compose, verify
from djedai.analytics.tools import run_sql
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


def _parameter_verification_state(question: str, draft: str):
    return {
        "chat_id": "c_parameters",
        "messages": [Message(role="user", content=question)],
        "understanding": Understanding(
            intent="data_question", resolved_question="Сколько заказов при пороге 9999?"
        ),
        "history_results": [],
        "results": [
            ToolResult(
                result_id="r_count",
                status="ok",
                tool="run_sql",
                sql="SELECT COUNT(*) AS n FROM orders WHERE qty > 150",
                data=ToolResultData(columns=["n"], rows=[[3]], row_count=1),
            )
        ],
        "answer_blocks": [Block(type="text", markdown=draft)],
        "compose_retries": 2,
    }


def test_model_resolved_question_cannot_authorize_a_new_constant():
    state = _parameter_verification_state("Сколько заказов?", "Порог 9999; строк 3")
    result = verify(state, {"configurable": {}})
    assert result["verification"].status == "failed"
    assert result["answer_blocks"][-1].type == "error"
    invented = next(a for a in result["verification"].assertions if a.raw_text == "9999")
    assert invented.reason_code == "number_not_found"


def test_original_user_count_threshold_is_accepted_with_its_unit():
    state = _parameter_verification_state(
        "Сколько групп содержит более 150 заказов?", "Порог 150 заказов; строк 3"
    )
    result = verify(state, {"configurable": {}})
    assert result["verification"].status == "verified"
    threshold = next(a for a in result["verification"].assertions if a.raw_text == "150")
    assert threshold.kind == "constant" and threshold.unit == "count"
    assert "user question" in threshold.reason


def test_corrected_text_uses_the_same_original_parameter_sources():
    for corrected, expected in (
        ("Порог 150 заказов; строк 3", "corrected"),
        ("Порог 9999; строк 3", "failed"),
    ):
        state = _parameter_verification_state(
            "Сколько групп содержит более 150 заказов?", "Выручка 8888"
        )
        client = MagicMock()
        client.generate_typed_response.return_value = ModelResponse(
            status="success", data=VerifyResponse(is_valid=False, corrected_markdown=corrected)
        )
        result = verify(state, {"configurable": {"client": client}})
        assert result["verification"].status == expected
        if expected == "failed":
            assert result["answer_blocks"][-1].type == "error"
            assert "invalid_correction" in {issue.code for issue in result["verification"].issues}
        else:
            assert result["answer_blocks"][-1].markdown == corrected


def test_supported_user_arithmetic_does_not_trigger_an_unnecessary_compose_retry():
    state = _parameter_verification_state("Раздели 7200 секунд на 3600", "7200 / 3600 = 2")
    state["compose_retries"] = 1
    state["results"] = [
        ToolResult(
            result_id="r_seconds",
            status="ok",
            tool="run_sql",
            sql="SELECT duration_sec FROM orders",
            data=ToolResultData(columns=["duration_sec"], rows=[[7200]], row_count=1),
        )
    ]
    client = MagicMock()
    client.generate_typed_response.return_value = ModelResponse(
        status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")
    )
    result = verify(state, {"configurable": {"client": client}})
    assert result["is_verified"] is True
    assert result["verification"].status == "verified"
    client.generate_typed_response.assert_called_once()
    assert "Not found in the data" not in client.generate_typed_response.call_args.args[1]


@pytest.mark.parametrize(
    "question,draft,corrected,status",
    [
        ("Вычисли 77 - 5", "77 - 5 = 9999", "77 - 5 = 72", "corrected"),
        ("2024/10/31", "77 / 31 = 9999", "77 / 31 = 2.48", "failed"),
        ("10/31/2024", "77 / 31 = 9999", "77 / 31 = 2.48", "failed"),
        ("*/31/5", "77 / 31 = 9999", "77 / 31 = 2.48", "failed"),
    ],
)
def test_real_graph_final_formula_correction_uses_validated_original_arithmetic(
    question, draft, corrected, status
):
    client = MagicMock()
    client.generate_typed_response.side_effect = [
        ModelResponse(status="success", data=Understanding(intent="data_question")),
        ModelResponse(
            status="success",
            data=PlanResponse(
                plan=[PlannedCall(tool="run_sql", args={"sql": "SELECT MAX(qty) AS n FROM orders"})]
            ),
        ),
        *[
            ModelResponse(
                status="success",
                data=ComposeResponse(answer_blocks=[Block(type="text", markdown=draft)]),
            )
            for _ in range(2)
        ],
        ModelResponse(
            status="success", data=VerifyResponse(is_valid=False, corrected_markdown=corrected)
        ),
    ]
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE orders AS SELECT * FROM (VALUES (77), (20), (30)) t(qty)")

        def dispatch(tool, args):
            assert tool == "run_sql"
            return run_sql(**args, connection=connection, allowed_tables={"orders"})

        result = build_agent_graph().invoke(
            {
                "chat_id": "formula",
                "dataset_id": "arithmetic",
                "messages": [Message(role="user", content=question)],
            },
            {
                "configurable": {
                    "thread_id": question,
                    "client": client,
                    "tool_dispatcher": dispatch,
                }
            },
        )

    assert result["results"][0].data.rows == [[77]]
    assert result["compose_retries"] == 2
    assert result["verification"].status == status
    assert result["verification"].rejected_assertions
    if status == "corrected":
        assert result["answer_blocks"][-1].markdown == corrected
        assertion = result["verification"].assertions[0]
        assert assertion.operands[0].evidence[0].result_id == result["results"][0].result_id
        assert assertion.operands[1].evidence == []
    else:
        assert result["answer_blocks"][-1].type == "error"
        assert all(block.markdown != corrected for block in result["answer_blocks"])
        assert "invalid_correction" in {issue.code for issue in result["verification"].issues}


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
    draft = "Всего в данных 500 000 запусков."
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
    assert rejected.rejected_assertions[0].reason_code == "unrelated_result"
    client = MagicMock()

    def respond(system_prompt, user_prompt, schema, **kwargs):
        assert "Всего в данных 500 000 запусков" in user_prompt
        assert "Подпись числа относится к другому результату" in user_prompt
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
    assert final["verification"].rejected_assertions[0].reason_code == "unrelated_result"
