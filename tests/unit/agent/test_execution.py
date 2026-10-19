from djedai.agent.graph import build_agent_graph
from djedai.contracts.agent import Message, PlannedCall
from djedai.contracts.tool_result import ToolResult


def test_execution_run_sql_integration():
    """Verifies that the execute node correctly delegates to analytics.run_sql"""
    graph = build_agent_graph()
    config = {"configurable": {"thread_id": "exec_test_sql"}}

    # We bypass LLM generation by directly injecting the plan into the initial state
    initial_state = {
        "chat_id": "chat_1",
        "dataset_id": "ds_1",
        "messages": [Message(role="user", content="Select all from test")],
        # Planning a query against the in-memory 'test' table defined in run_sql
        "plan": [PlannedCall(tool="run_sql", args={"sql": "SELECT * FROM test"})],
    }

    result = graph.invoke(initial_state, config=config)

    assert len(result["results"]) == 1
    tool_res = result["results"][0]
    assert tool_res.status == "ok"
    assert tool_res.tool == "run_sql"
    assert tool_res.data["row_count"] == 2  # The mock table in run_sql has 2 rows
    assert tool_res.data["columns"] == ["id", "name"]

    assert len(result["answer_blocks"]) >= 1


def test_execution_retry_loop():
    """Verifies the error recovery loop triggers and logs both failure and success."""
    graph = build_agent_graph()
    call_count = {"count": 0}

    def mock_dispatcher(tool: str, args: dict):
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise ValueError("Table 'ordrs' not found")
        return ToolResult(
            result_id="r_99",
            status="ok",
            tool=tool,
            params=args,
            facts=[{"text": "Recovered successfully"}],
        )

    config = {"configurable": {"thread_id": "exec_test_retry", "tool_dispatcher": mock_dispatcher}}

    initial_state = {
        "chat_id": "chat_2",
        "plan": [PlannedCall(tool="run_sql", args={"sql": "SELECT * FROM ordrs"})],
    }

    result = graph.invoke(initial_state, config=config)

    # Retry loop triggered: 1 initial fail + 1 retry
    assert call_count["count"] == 2
    assert result["retries"] == 2

    # Both history records must be preserved
    assert len(result["results"]) == 2
    assert result["results"][0].status == "error"
    assert "ordrs" in result["results"][0].error.message

    assert result["results"][1].status == "ok"
