from pathlib import Path
from unittest.mock import MagicMock

from djedai.agent.client import ModelResponse
from djedai.agent.diagnostics import NODES, RunDiagnostics
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
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.tool_result import ChartHint, ToolError, ToolResult, ToolResultData

EXAMPLE = (
    Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples" / "dataset_catalog.json"
)


def test_graph_shortest_path_and_isolation():
    graph = build_agent_graph()

    # --- Test Chat 1 (Turn 1: Standard Flow) ---
    config_1 = {"configurable": {"thread_id": "chat_1"}}
    initial_state_1 = {
        "chat_id": "chat_1",
        "dataset_id": "ds_alpha",
        "messages": [Message(role="user", content="Show me the revenue.")],
        "history_summary": "User asked for revenue.",
        "user_context": "Financial Analyst",
    }

    result_1 = graph.invoke(initial_state_1, config=config_1)

    # Verify state updates mapping to Chat 1
    assert result_1["chat_id"] == "chat_1"
    assert result_1["dataset_id"] == "ds_alpha"
    assert result_1["history_summary"] == "User asked for revenue."
    assert result_1["user_context"] == "Financial Analyst"
    assert result_1["catalog_prompt"] == "Catalog mapped for ds_alpha"
    assert result_1["understanding"].intent == "data_question"
    assert len(result_1["answer_blocks"]) == 1
    assert result_1["answer_blocks"][0].markdown == "Нет данных для формирования ответа."

    # --- Test Chat 1 (Turn 2: Ambiguous Flow) ---
    # This verifies that previous turn's results (answer_blocks, plan, etc.)
    # are cleared when running the same thread_id again.
    turn_2_update = {
        "messages": [Message(role="user", content="This is an ambiguous request.")],
    }
    result_turn_2 = graph.invoke(turn_2_update, config=config_1)

    assert result_turn_2["understanding"].intent == "ambiguous"
    assert len(result_turn_2["answer_blocks"]) == 1
    assert result_turn_2["answer_blocks"][0].type == "clarification"
    assert result_turn_2["plan"] == []
    assert result_turn_2["results"] == []

    # --- Test Chat 2 (Strict Isolation) ---
    config_2 = {"configurable": {"thread_id": "chat_2"}}
    initial_state_2 = {
        "chat_id": "chat_2",
        "dataset_id": "ds_beta",
        "messages": [Message(role="user", content="Show sales.")],
        "history_summary": "New chat context.",
        "user_context": "Sales Manager",
    }

    result_2 = graph.invoke(initial_state_2, config=config_2)

    assert result_2["chat_id"] == "chat_2"
    assert result_2["dataset_id"] == "ds_beta"
    assert result_2["history_summary"] == "New chat context."


def test_graph_passes_each_model_stage_to_the_client():
    client = MagicMock()
    client.generate_typed_response.side_effect = [
        ModelResponse(status="success", data=Understanding(intent="visualize")),
        ModelResponse(
            status="success",
            data=PlanResponse(plan=[PlannedCall(tool="run_sql", args={"sql": "SELECT 1"})]),
        ),
        ModelResponse(status="success", data=VisualizeResponse(charts=[])),
        ModelResponse(
            status="success",
            data=ComposeResponse(answer_blocks=[Block(type="text", markdown="Готово")]),
        ),
        ModelResponse(status="success", data=VerifyResponse(is_valid=True, corrected_markdown="")),
    ]

    def dispatcher(_tool, _args):
        return ToolResult(
            result_id="r_stage",
            status="ok",
            tool="run_sql",
            data=ToolResultData(columns=["n"], rows=[[1]]),
            chart_hint=ChartHint(type="bar", x="n", y=["n"]),
        )

    result = build_agent_graph().invoke(
        {
            "chat_id": "c_stage",
            "dataset_id": "ds_stage",
            "messages": [Message(role="user", content="Покажи график")],
        },
        config={
            "configurable": {
                "thread_id": "c_stage",
                "client": client,
                "tool_dispatcher": dispatcher,
            }
        },
    )

    assert result["answer_blocks"][0].markdown == "Готово"
    assert [call.kwargs["stage"] for call in client.generate_typed_response.call_args_list] == [
        "understand",
        "plan",
        "visualize",
        "compose",
        "verify",
    ]


def test_graph_uses_catalog_prompt_from_loader():
    catalog = DatasetCatalog.model_validate_json(EXAMPLE.read_text(encoding="utf-8"))
    config = {"configurable": {"thread_id": "chat_catalog", "catalog_loader": lambda _: catalog}}
    state = {
        "chat_id": "chat_catalog",
        "dataset_id": catalog.dataset_id,
        "messages": [Message(role="user", content="Show me the revenue.")],
    }

    result = build_agent_graph().invoke(state, config=config)

    assert result["catalog_prompt"] == catalog.to_prompt()
    assert "Уникальный номер заказа" in result["catalog_prompt"]


def _catalog() -> DatasetCatalog:
    return DatasetCatalog.model_validate_json(EXAMPLE.read_text(encoding="utf-8"))


def _fast_config(catalog, thread_id, **configurable):
    return {
        "configurable": {
            "thread_id": thread_id,
            "catalog_loader": lambda _: catalog,
            **configurable,
        }
    }


def _invoke_fast(question, dispatcher, catalog=None, dataset_id=None, **configurable):
    catalog = catalog or _catalog()
    client = MagicMock()
    client.generate_typed_response.side_effect = AssertionError("model must not be called")
    result = build_agent_graph().invoke(
        {
            "chat_id": f"chat_{dataset_id or catalog.dataset_id}",
            "dataset_id": dataset_id or catalog.dataset_id,
            "messages": [Message(role="user", content=question)],
        },
        config=_fast_config(
            catalog,
            f"chat_{dataset_id or catalog.dataset_id}",
            client=client,
            tool_dispatcher=dispatcher,
            **configurable,
        ),
    )
    client.generate_typed_response.assert_not_called()
    return result


def test_fast_count_runs_secure_sql_and_composes_without_model():
    calls = []

    def dispatcher(tool, args):
        calls.append((tool, args))
        return ToolResult(
            result_id="r_fast_count",
            status="ok",
            tool=tool,
            sql=args["sql"],
            data=ToolResultData(columns=["row_count"], rows=[[125430]]),
        )

    result = _invoke_fast("Сколько всего строк в таблице orders?", dispatcher)

    assert calls == [("run_sql", {"sql": 'SELECT COUNT(*) AS row_count FROM "orders"'})]
    assert result["fast_path"].kind == "row_count"
    assert result["answer_blocks"][0].markdown == "Строк в таблице `orders`: 125430."
    assert result["results"][0].result_id == "r_fast_count"
    assert result["focus_result_ids"] == ["r_fast_count"]


def test_fast_path_diagnostics_measure_stages_without_model_calls():
    catalog = _catalog()
    events = []
    diagnostics = RunDiagnostics(
        events.append,
        owner_id="owner",
        chat_id="chat_diag",
        dataset_id=catalog.dataset_id,
        user_message_id="m_diag",
    )

    def dispatcher(tool, args):
        return ToolResult(
            result_id="r_diag",
            status="ok",
            tool=tool,
            sql=args["sql"],
            data=ToolResultData(columns=["row_count"], rows=[[2]]),
        )

    client = MagicMock()
    client.generate_typed_response.side_effect = AssertionError("model must not be called")
    build_agent_graph().invoke(
        {
            "chat_id": "chat_diag",
            "dataset_id": catalog.dataset_id,
            "messages": [Message(role="user", content="Сколько строк в таблице clients?")],
        },
        config=_fast_config(
            catalog,
            "chat_diag",
            client=client,
            diagnostics=diagnostics,
            tool_dispatcher=dispatcher,
        ),
    )
    diagnostics.finish("success")

    assert {"fast_route", "fast_compose"} <= NODES
    assert {event["node"] for event in events if event["event"] == "stage_finished"} >= {
        "fast_route",
        "execute",
        "fast_compose",
    }
    selected = next(event for event in events if event["event"] == "fast_path_selected")
    assert (selected["kind"], selected["table_id"]) == ("row_count", "clients")
    assert not [event for event in events if event["event"].startswith("model_call_")]
    finished = events[-1]
    assert finished["event"] == "run_finished"
    assert finished["duration_ms"] >= 0
    client.generate_typed_response.assert_not_called()


def test_fast_count_returns_exact_zero():
    def dispatcher(_tool, _args):
        return ToolResult(
            result_id="r_zero",
            status="ok",
            data=ToolResultData(columns=["row_count"], rows=[[0]]),
        )

    result = _invoke_fast("Сколько строк в таблице orders?", dispatcher)

    assert result["answer_blocks"][0].markdown == "Строк в таблице `orders`: 0."


def test_fast_count_null_and_tool_error_are_explicit():
    def null_dispatcher(_tool, _args):
        return ToolResult(
            result_id="r_null",
            status="ok",
            data=ToolResultData(columns=["row_count"], rows=[[None]]),
        )

    null = _invoke_fast("Сколько строк в таблице orders?", null_dispatcher)
    assert null["answer_blocks"][0].type == "error"
    assert "NULL" in null["answer_blocks"][0].markdown

    def error_dispatcher(_tool, _args):
        return ToolResult(
            result_id="r_error",
            status="error",
            tool="run_sql",
            error=ToolError(code="database_error", message="Источник недоступен"),
        )

    failed = _invoke_fast("Сколько строк в таблице orders?", error_dispatcher)
    assert failed["answer_blocks"][0].type == "error"
    assert failed["answer_blocks"][0].markdown == "Источник недоступен"


def test_fast_structure_uses_catalog_without_tools_or_model():
    calls = []

    def dispatcher(tool, args):
        calls.append((tool, args))
        raise AssertionError("structure must not execute tools")

    result = _invoke_fast("Покажи структуру таблицы orders", dispatcher)

    assert calls == []
    assert result["results"] == []
    assert result["fast_path"].kind == "structure"
    assert "`order_id`" in result["answer_blocks"][0].markdown
    assert "`created_at`" in result["answer_blocks"][0].markdown


def test_ambiguous_multi_table_and_negative_question_use_general_route():
    catalog = _catalog()
    for question in (
        "Сколько строк?",
        "Сколько строк в таблице orders после 2024 года?",
        "Сколько уникальных клиентов в orders?",
        "Сколько строк в orders и clients?",
    ):
        result = build_agent_graph().invoke(
            {
                "chat_id": "chat_general",
                "dataset_id": catalog.dataset_id,
                "messages": [Message(role="user", content=question)],
            },
            config=_fast_config(catalog, "chat_general"),
        )

        assert result["fast_path"] is None
        assert result["plan"] == []
        assert result["results"] == []
        assert result["understanding"].intent == "data_question"


def test_mismatched_catalog_does_not_enable_fast_path():
    catalog = _catalog()

    def dispatcher(_tool, _args):
        raise AssertionError("mismatched catalog must not execute")

    result = build_agent_graph().invoke(
        {
            "chat_id": "chat_other",
            "dataset_id": "ds_other",
            "messages": [Message(role="user", content="Сколько строк?")],
        },
        config=_fast_config(catalog, "chat_other", tool_dispatcher=dispatcher),
    )

    assert result["fast_path"] is None
    assert result["results"] == []
