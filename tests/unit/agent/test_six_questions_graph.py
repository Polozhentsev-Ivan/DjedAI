"""Поведение графа за вопросами организаторов (#243), без реальной модели."""

from unittest.mock import MagicMock

from djedai.agent.client import ModelResponse
from djedai.agent.data_policy import DataTransferPolicy
from djedai.agent.graph import _with_column_totals, build_agent_graph
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


class _Policy(DataTransferPolicy):
    """Каталог-заглушка не сериализуется: очистку каталога здесь не проверяем."""

    def sanitize_catalog(self, catalog):
        return catalog


def _ok(response):
    return ModelResponse(status="success", data=response)


def _result(result_id, columns, rows):
    return ToolResult(
        result_id=result_id,
        status="ok",
        data=ToolResultData(columns=columns, rows=rows, row_count=len(rows)),
    )


def _run(client, dispatcher, question="Вопрос"):
    return build_agent_graph().invoke(
        {"dataset_id": "d", "chat_id": "c", "messages": [Message(role="user", content=question)]},
        {"configurable": {"thread_id": question, "client": client, "tool_dispatcher": dispatcher}},
    )


def _text(markdown):
    return _ok(ComposeResponse(answer_blocks=[Block(type="text", markdown=markdown)]))


def _verified():
    return _ok(VerifyResponse(is_valid=True, corrected_markdown=""))


def _client(*responses, reasoning="provider_default"):
    client = MagicMock()
    client.reasoning_for.return_value = reasoning
    client.generate_typed_response.side_effect = list(responses)
    return client


def _plan(tool):
    return _ok(PlanResponse(plan=[PlannedCall(tool=tool, args={})]))


def _stages(client):
    return [c.kwargs.get("stage") for c in client.generate_typed_response.call_args_list]


def test_exploration_returns_to_plan_with_seen_values():
    calls = []

    def dispatcher(tool, args):
        calls.append(tool)
        if tool == "value_counts":
            return _result("r_a", ["status", "count"], [["Успешно", 7]])
        return _result("r_b", ["job", "share"], [["x", 5.24]])

    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("value_counts"),
        _plan("run_sql"),
        _text("Доля 5,24 %"),
        _verified(),
    )
    state = _run(client, dispatcher)

    assert calls == ["value_counts", "run_sql"]
    second_plan = client.generate_typed_response.call_args_list[2].args[0]
    assert "Успешно" in second_plan
    # На втором проходе остаются только расчётные инструменты.
    assert "- run_sql:" in second_plan and "- value_counts:" not in second_plan
    assert state["answer_blocks"][0].markdown == "Доля 5,24 %"


def test_exploration_that_answers_allows_empty_second_plan():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("value_counts"),
        _ok(PlanResponse(plan=[])),
        _text("Успешно: 7 раз"),
        _verified(),
    )
    dispatcher = MagicMock(return_value=_result("r_a", ["status", "count"], [["Успешно", 7]]))
    state = _run(client, dispatcher)

    assert dispatcher.call_count == 1
    assert state["focus_result_ids"] == ["r_a"]


def test_exploration_gets_only_one_second_pass():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("value_counts"),
        _plan("value_counts"),
        _text("Успешно: 7 раз"),
        _verified(),
    )
    dispatcher = MagicMock(return_value=_result("r_a", ["status", "count"], [["Успешно", 7]]))
    _run(client, dispatcher)

    assert _stages(client).count("plan") == 2


def test_calculation_does_not_replan():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Итого 7"),
        _verified(),
    )
    _run(client, MagicMock(return_value=_result("r_a", ["n"], [[7]])))
    assert _stages(client).count("plan") == 1


def test_clarification_gets_second_look_when_reasoning_is_off():
    client = _client(
        _ok(Understanding(intent="ambiguous")),
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Итого 7"),
        _verified(),
        reasoning="none",
    )
    _run(client, MagicMock(return_value=_result("r_a", ["n"], [[7]])))

    second = client.generate_typed_response.call_args_list[1]
    assert second.kwargs["reasoning"] == "low"
    assert _stages(client).count("plan") == 1


def test_failed_second_look_keeps_first_answer():
    client = _client(
        _ok(Understanding(intent="ambiguous")),
        ModelResponse(status="error", error_type="OUTPUT_LIMIT"),
        reasoning="none",
    )
    state = _run(client, MagicMock())

    assert state["understanding"].intent == "ambiguous"
    assert "plan" not in _stages(client)


def test_second_look_is_skipped_when_reasoning_is_not_explicitly_off():
    client = _client(_ok(Understanding(intent="ambiguous")))
    _run(client, MagicMock())
    assert client.generate_typed_response.call_count == 1


def test_data_question_needs_no_second_look():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Итого 7"),
        _verified(),
        reasoning="none",
    )
    _run(client, MagicMock(return_value=_result("r_a", ["n"], [[7]])))
    assert _stages(client).count("understand") == 1


def test_number_missing_from_data_goes_back_to_compose_without_model_verify():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Рост 23,34 %"),
        _text("Рост 30,32 %"),
        _verified(),
    )
    state = _run(client, MagicMock(return_value=_result("r_a", ["growth"], [[30.3211]])))

    assert _stages(client) == ["understand", "plan", "compose", "compose", "verify"]
    assert state["answer_blocks"][0].markdown == "Рост 30,32 %"


def test_numbers_unsupported_after_retry_are_named_to_verify():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Рост 23,34 %"),
        _text("Рост 23,34 %"),
        _ok(VerifyResponse(is_valid=False, corrected_markdown="Рост посчитать не удалось")),
    )
    state = _run(client, MagicMock(return_value=_result("r_a", ["growth"], [[30.3211]])))

    assert "23,34" in client.generate_typed_response.call_args_list[4].args[1]
    assert state["answer_blocks"][0].markdown == "Рост посчитать не удалось"


def test_seconds_reach_compose_as_hours():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("72,49 ч"),
        _verified(),
    )
    _run(client, MagicMock(return_value=_result("r_a", ["p90_sec"], [[260964.0]])))

    compose_prompt = client.generate_typed_response.call_args_list[2].args[0]
    assert '"hours": 72.49' in compose_prompt


def test_compose_and_verify_see_the_query():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Итого 7"),
        _verified(),
    )
    result = _result("r_a", ["n"], [[7]]).model_copy(update={"sql": "SELECT 7 AS n WHERE 1 = 1"})
    _run(client, MagicMock(return_value=result))

    prompts = [call.args[0] for call in client.generate_typed_response.call_args_list[2:4]]
    assert all("WHERE 1 = 1" in prompt for prompt in prompts)


def test_how_much_without_unit_reaches_planner():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Итого 7"),
        _verified(),
    )
    _run(client, MagicMock(return_value=_result("r_a", ["n"], [[7]])), "Сколько было согласовано?")

    assert "COUNT(*) and SUM" in client.generate_typed_response.call_args_list[1].args[1]


def test_compose_gets_field_names_without_catalog_statistics():
    catalog = MagicMock()
    column = MagicMock(currency=None, original_name="Статус", description=None)
    column.name = "status"
    catalog.tables = [
        MagicMock(table_id="jobs", original_name="jobs", description=None, columns=[column])
    ]
    catalog.to_prompt.return_value = "строк 500000"
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Итого 7"),
        _verified(),
    )
    build_agent_graph().invoke(
        {"dataset_id": "d", "chat_id": "c", "messages": [Message(role="user", content="Вопрос")]},
        {
            "configurable": {
                "thread_id": "fields",
                "client": client,
                "tool_dispatcher": MagicMock(return_value=_result("r_a", ["n"], [[7]])),
                "catalog_loader": lambda _: catalog,
                "data_policy": _Policy(),
            }
        },
    )
    calls = client.generate_typed_response.call_args_list
    assert "jobs.status «Статус»" in calls[2].args[0]
    assert "500000" not in calls[2].args[0] and "500000" not in calls[3].args[0]


def test_column_totals_are_a_fact_of_the_result_unless_it_is_truncated():
    client = _client(
        _ok(Understanding(intent="data_question")),
        _plan("run_sql"),
        _text("Всего 7"),
        _verified(),
    )
    state = _run(client, MagicMock(return_value=_result("r_a", ["g", "n"], [["a", 3], ["b", 4]])))
    (fact,) = state["results"][0].facts
    assert fact.kind == "summary" and fact.values == {"column_totals": {"n": 7}}

    truncated = _result("r_b", ["g", "n"], [["a", 3], ["b", 4]])
    truncated = truncated.model_copy(
        update={"data": truncated.data.model_copy(update={"truncated": True})}
    )
    assert _with_column_totals(truncated).facts == []
