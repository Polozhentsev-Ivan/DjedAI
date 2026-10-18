from djedai.agent.graph import build_agent_graph
from djedai.contracts.agent import Message


def test_graph_shortest_path_and_isolation():
    graph = build_agent_graph()

    # --- Test Chat 1 (Standard Flow) ---
    config_1 = {"configurable": {"thread_id": "chat_1"}}
    initial_state_1 = {
        "chat_id": "chat_1",
        "dataset_id": "ds_alpha",
        "messages": [Message(role="user", content="Show me the revenue.")],
    }

    result_1 = graph.invoke(initial_state_1, config=config_1)

    # Verify state updates mapping to Chat 1
    assert result_1["chat_id"] == "chat_1"
    assert result_1["dataset_id"] == "ds_alpha"
    assert result_1["catalog_prompt"] == "Catalog mapped for ds_alpha"
    assert result_1["understanding"].intent == "data_question"
    assert len(result_1["answer_blocks"]) == 1
    assert result_1["answer_blocks"][0].markdown == "Calculated response"

    # --- Test Chat 2 (Alternative Flow + Isolation) ---
    config_2 = {"configurable": {"thread_id": "chat_2"}}
    initial_state_2 = {
        "chat_id": "chat_2",
        "dataset_id": "ds_beta",
        "messages": [Message(role="user", content="This is an ambiguous request.")],
    }

    result_2 = graph.invoke(initial_state_2, config=config_2)

    # Verify Chat 2 state updates correctly routed to 'clarify'
    assert result_2["chat_id"] == "chat_2"
    assert result_2["dataset_id"] == "ds_beta"
    assert result_2["catalog_prompt"] == "Catalog mapped for ds_beta"
    assert result_2["understanding"].intent == "ambiguous"
    assert result_2["answer_blocks"][0].type == "clarification"

    # --- Verify Strict Isolation ---
    # Fetching state of chat_1 from memory should show absolutely no trace of chat_2
    saved_state_1 = graph.get_state(config_1).values
    assert saved_state_1["dataset_id"] == "ds_alpha"
    assert saved_state_1["understanding"].intent == "data_question"
