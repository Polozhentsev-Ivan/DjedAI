"""Агент и потери типизации (B2-10): ограничения в ответе, уточнение и правила промптов.

Модель подменена, инструменты и проверка качества настоящие: quality_dataset.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.analytics.quality import assess
from djedai.analytics.registry import execute_tool
from djedai.contracts.agent import (
    Block,
    ComposeResponse,
    Message,
    PlannedCall,
    PlanResponse,
    Understanding,
    VerifyResponse,
)

SUM = "SELECT SUM(amount) FROM orders"
BY_DATE = "SELECT d_ambiguous, COUNT(*) FROM edges GROUP BY 1"
OPTION = "edges.d_ambiguous: день/месяц/год (%d/%m/%Y)"


def run(dataset, responses, messages):
    connection, catalog = dataset
    client = MagicMock()
    client.generate_typed_response.side_effect = responses
    tables = {t.table_id for t in catalog.tables}

    def dispatch(tool, args):
        result = execute_tool(tool, args, connection=connection, allowed_tables=tables)
        return assess(result, connection, catalog)

    config = {
        "configurable": {
            "thread_id": f"quality-{id(responses)}",
            "client": client,
            "catalog_loader": lambda _: catalog,
            "tool_dispatcher": dispatch,
        }
    }
    state = {
        "chat_id": "c",
        "dataset_id": catalog.dataset_id,
        "messages": [Message(role="user", content=text) for text in messages],
    }
    return build_agent_graph().invoke(state, config=config), client


def ok(data):
    return ModelResponse(status="success", data=data)


def answer(sql: str, text: str) -> list[ModelResponse]:
    return [
        ok(Understanding(intent="data_question")),
        ok(PlanResponse(plan=[PlannedCall(tool="run_sql", args={"sql": sql})])),
        ok(ComposeResponse(answer_blocks=[Block(type="text", markdown=text)])),
        ok(VerifyResponse(is_valid=True, corrected_markdown=text)),
    ]


def test_losses_are_appended_even_if_the_model_is_silent(quality_dataset):
    result, client = run(quality_dataset, answer(SUM, "Сумма — 5 290,5."), ["Сумма заказов?"])
    (computed,) = result["results"]
    assert computed.quality.columns[0].parse_failed == 4
    text, limits = result["answer_blocks"]
    assert text.markdown == "Сумма — 5 290,5."
    assert limits.markdown.startswith("**Ограничения данных.**")
    assert "4 из 100" in limits.markdown

    compose_prompt = client.generate_typed_response.call_args_list[2].args[0]
    verify_prompt = client.generate_typed_response.call_args_list[3].args[0]
    for prompt in (compose_prompt, verify_prompt):
        assert "data_quality" in prompt and "4 из 100" in prompt
    assert "Never call such a result complete" in compose_prompt
    assert "never add ₽" in compose_prompt


def test_result_without_losses_has_no_limitations_block(quality_dataset):
    sql = "SELECT SUM(amount) FROM orders WHERE order_id >= 'o010'"
    result, _ = run(quality_dataset, answer(sql, "Готово"), ["Сумма?"])
    assert [b.markdown for b in result["answer_blocks"]] == ["Готово"]


def test_ambiguous_date_grouping_asks_for_the_format(quality_dataset):
    responses = answer(BY_DATE, "не дойдёт")[:2]
    result, client = run(quality_dataset, responses, ["Сколько строк по датам?"])
    (block,) = result["answer_blocks"]
    assert block.type == "clarification"
    assert "день и месяц не различить" in block.markdown
    assert block.options == [
        "edges.d_ambiguous: день/месяц/год (%d/%m/%Y)",
        "edges.d_ambiguous: месяц/день/год (%m/%d/%Y)",
    ]
    assert client.generate_typed_response.call_count == 2  # без compose и перепланирования


def test_answered_format_lets_the_calculation_through_with_a_note(quality_dataset):
    question = (
        "Сколько строк по датам?\nУточнение пользователя: "
        "edges.d_ambiguous: день/месяц/год (%d/%m/%Y)."
    )
    sql = "SELECT strptime(d_ambiguous, '%d/%m/%Y') AS d, COUNT(*) FROM edges GROUP BY 1"
    result, _ = run(quality_dataset, answer(sql, "12 дат"), [question])
    text, limits = result["answer_blocks"]
    assert text.markdown == "12 дат"
    assert "день и месяц не различить" in limits.markdown
    assert "Выбор уточнён пользователем." in limits.markdown


def test_choice_from_an_earlier_question_does_not_answer_a_new_one(quality_dataset):
    earlier = f"Сколько строк по датам?\nУточнение пользователя: {OPTION}."
    responses = answer(BY_DATE, "не дойдёт")[:2]
    result, _ = run(quality_dataset, responses, [earlier, "А сколько строк по датам сейчас?"])
    (block,) = result["answer_blocks"]
    assert block.type == "clarification"
    assert "Выбор уточнён" not in (block.markdown or "")
