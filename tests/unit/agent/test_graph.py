from pathlib import Path
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
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.tool_result import ChartHint, ToolResult, ToolResultData

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
