"""Real SDK/network cancellation and a shared budget across graph retries."""

import threading
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from typing import Any

import duckdb
import pytest

from djedai.agent.client import BIModelClient, ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.analytics.tools import run_sql
from djedai.config import LimitSettings
from djedai.contracts.agent import (
    Block,
    ComposeResponse,
    Message,
    PlannedCall,
    PlanResponse,
    Understanding,
    VerifyResponse,
)
from djedai.contracts.tool_result import ToolError, ToolResult, ToolResultData
from djedai.execution import ExecutionBudget, TurnCancelled, TurnTimeout


def client_for(endpoint: Any, **options: Any) -> BIModelClient:
    return BIModelClient(
        "fixture-key", endpoint.base_url, "fixture-model", retry_delay=0.01, **options
    )


def test_transient_statuses_retry_without_hidden_sdk_attempts(model_endpoint) -> None:
    with model_endpoint(lambda n, _: {"status": [429, 503, 200][n - 1]}) as endpoint:
        client = client_for(endpoint, max_retries=2)
        try:
            assert (
                client.generate_typed_response("sys", "question", Understanding).status == "success"
            )
            assert len(endpoint.calls) == 3
            assert client.client.max_retries == 0
        finally:
            client.client.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 501])
def test_permanent_errors_do_not_retry(model_endpoint, status: int) -> None:
    with model_endpoint(lambda *_: {"status": status}) as endpoint:
        client = client_for(endpoint, max_retries=3)
        try:
            assert (
                client.generate_typed_response("sys", "question", Understanding).status == "error"
            )
            assert len(endpoint.calls) == 1
        finally:
            client.client.close()


@pytest.mark.parametrize("status", [401, 403, 429])
def test_slow_error_body_does_not_hide_status_or_retry_after(model_endpoint, status) -> None:
    with model_endpoint(
        lambda *_: {"status": status, "headers": {"Retry-After": "600"}, "mode": "drip"}
    ) as endpoint:
        client = client_for(endpoint, timeout=0.25, max_retries=3)
        try:
            started = monotonic()
            with ExecutionBudget(3).activate():
                response = client.generate_typed_response("sys", "question", Understanding)
            assert response.status == "error"
            assert response.error_type == "SERVER_ERROR"
            assert len(endpoint.calls) == 1
            assert monotonic() - started < 1
            assert endpoint.peer_closed.wait(1)
        finally:
            client.client.close()


def test_slow_transient_error_body_still_honours_short_retry_after(model_endpoint) -> None:
    with model_endpoint(
        lambda n, _: (
            {"status": 503, "headers": {"Retry-After": "0.1"}, "mode": "drip"} if n == 1 else {}
        )
    ) as endpoint:
        client = client_for(endpoint, timeout=0.25, max_retries=1)
        try:
            started = monotonic()
            with ExecutionBudget(3).activate():
                response = client.generate_typed_response("sys", "question", Understanding)
            assert response.status == "success", response
            assert len(endpoint.calls) == 2
            assert monotonic() - started >= 0.1
        finally:
            client.client.close()


@pytest.mark.parametrize("retry_after", ["600", "1e999", "Thu, 01 Jan 2099 00:00:00 GMT"])
def test_retry_after_that_exceeds_remaining_budget_is_not_ignored(
    model_endpoint, retry_after: str
) -> None:
    with model_endpoint(
        lambda *_: {"status": 429, "headers": {"Retry-After": retry_after}}
    ) as endpoint:
        client = client_for(endpoint, max_retries=3)
        try:
            start = monotonic()
            with ExecutionBudget(1).activate():
                response = client.generate_typed_response("sys", "question", Understanding)
            assert response.status == "error"
            assert len(endpoint.calls) == 1
            assert monotonic() - start < 1
        finally:
            client.client.close()


@pytest.mark.parametrize(
    "headers", [{"Retry-After": "0.1"}, {"retry-after-ms": "100"}, {"Retry-After": "invalid"}]
)
def test_retry_after_or_fallback_wait_stays_inside_budget(model_endpoint, headers) -> None:
    with model_endpoint(
        lambda n, _: {"status": 429, "headers": headers} if n == 1 else {}
    ) as endpoint:
        client = client_for(endpoint, max_retries=1)
        try:
            started = monotonic()
            with ExecutionBudget(2).activate():
                response = client.generate_typed_response("sys", "question", Understanding)
            assert response.status == "success"
            assert len(endpoint.calls) == 2
            if headers.get("Retry-After") != "invalid":
                assert monotonic() - started >= 0.1
        finally:
            client.client.close()


@pytest.mark.parametrize("mode", ["stall", "drip"])
def test_total_deadline_closes_a_stalled_or_trickling_response(model_endpoint, mode: str) -> None:
    with model_endpoint(lambda *_: {"mode": mode}) as endpoint:
        client = client_for(endpoint, timeout=5, max_retries=3)
        try:
            start = monotonic()
            with pytest.raises(TurnTimeout), ExecutionBudget(0.3).activate():
                client.generate_typed_response("sys", "question", Understanding)
            assert monotonic() - start < 1.5
            assert endpoint.peer_closed.wait(1)
            assert len(endpoint.calls) == 1
        finally:
            client.client.close()


def test_single_attempt_timeout_can_recover_inside_shared_budget(model_endpoint) -> None:
    with model_endpoint(lambda n, _: {"mode": "stall"} if n == 1 else {}) as endpoint:
        client = client_for(endpoint, timeout=0.25, max_retries=1)
        try:
            with ExecutionBudget(2).activate():
                response = client.generate_typed_response("sys", "question", Understanding)
            assert response.status == "success", (response, len(endpoint.calls))
            assert len(endpoint.calls) == 2
            assert endpoint.peer_closed.wait(1)
        finally:
            client.client.close()


def test_expired_budget_does_not_start_a_prepared_http_request(model_endpoint, monkeypatch) -> None:
    with model_endpoint(lambda *_: {}) as endpoint:
        client = client_for(endpoint, timeout=1)
        create = client.client.chat.completions.create

        def slow_preparation(**kwargs):
            sleep(0.05)
            return create(**kwargs)

        monkeypatch.setattr(client.client.chat.completions, "create", slow_preparation)
        try:
            with pytest.raises(TurnTimeout), ExecutionBudget(0.02).activate():
                client.generate_typed_response("sys", "question", Understanding)
            assert endpoint.calls == []
        finally:
            client.client.close()


@pytest.mark.parametrize("during_backoff", [False, True])
def test_cancel_stops_network_or_backoff_and_allows_next_question(
    model_endpoint, during_backoff: bool
) -> None:
    def reply(_number, payload):
        if payload["messages"][-1]["content"] == "stop":
            return (
                {"status": 429, "headers": {"Retry-After": "1"}}
                if during_backoff
                else {"mode": "stall"}
            )
        return {}

    with model_endpoint(reply) as endpoint:
        client = client_for(endpoint, timeout=5, max_retries=3)
        cancelled = threading.Event()
        waiting = threading.Event()

        class ObservedBudget(ExecutionBudget):
            def wait(self, seconds: float) -> None:
                waiting.set()
                super().wait(seconds)

        def invoke():
            with ObservedBudget(5, cancelled.is_set).activate():
                return client.generate_typed_response("sys", "stop", Understanding)

        try:
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(invoke)
                assert endpoint.started.wait(2)
                if during_backoff:
                    assert waiting.wait(2)
                cancelled.set()
                with pytest.raises(TurnCancelled):
                    pending.result(timeout=1.5)
            assert len(endpoint.calls) == 1
            if not during_backoff:
                assert endpoint.peer_closed.wait(1)
            with ExecutionBudget(2).activate():
                assert (
                    client.generate_typed_response("sys", "next", Understanding).status == "success"
                )
            assert len(endpoint.calls) == 2
        finally:
            client.client.close()


@pytest.mark.parametrize("replan", [True, False])
def test_replan_and_recompose_share_the_original_budget(replan: bool, monkeypatch) -> None:
    calls = []
    clock = [0.0]
    monkeypatch.setattr("djedai.execution.monotonic", lambda: clock[0])
    budget = ExecutionBudget(3 if replan else 5)

    class Model:
        def generate_typed_response(self, _system, _question, response_model, **kwargs):
            calls.append(response_model)
            from djedai.execution import current_budget

            assert current_budget() is budget
            clock[0] += 1
            current_budget().check()
            values = {
                Understanding: Understanding(intent="data_question"),
                PlanResponse: PlanResponse(plan=[PlannedCall(tool="run_sql", args={})]),
                ComposeResponse: ComposeResponse(
                    answer_blocks=[Block(type="text", markdown="draft")]
                ),
                VerifyResponse: VerifyResponse(is_valid=False, corrected_markdown=""),
            }
            return ModelResponse(status="success", data=values[response_model])

    result = ToolResult(
        result_id="fixture",
        status="error" if replan else "ok",
        error=ToolError(code="fixture", message="fixture") if replan else None,
        data=ToolResultData(columns=["n"], rows=[[1]]) if not replan else None,
    )
    graph = build_agent_graph()
    with pytest.raises(TurnTimeout):
        graph.invoke(
            {"dataset_id": "fixture", "messages": [Message(role="user", content="question")]},
            {
                "configurable": {
                    "thread_id": "fixture",
                    "budget": budget,
                    "client": Model(),
                    "tool_dispatcher": lambda *_: result,
                }
            },
        )
    assert calls.count(PlanResponse if replan else ComposeResponse) == 2


def test_sql_is_interrupted_by_turn_budget_and_connection_remains_usable() -> None:
    connection = duckdb.connect()
    try:
        started = monotonic()
        with pytest.raises(TurnTimeout), ExecutionBudget(0.2).activate():
            result = run_sql(
                "SELECT sum(sin(a.i + b.i)) FROM range(1000000) a(i), range(1000000) b(i)",
                connection=connection,
            )
            assert result.status == "error"
        assert monotonic() - started < 2
        assert connection.execute("SELECT 42").fetchone() == (42,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "values",
    [
        {"agent_turn_timeout_seconds": 0},
        {"agent_turn_timeout_seconds": 601},
        {"model_timeout_seconds": 0},
        {"model_timeout_seconds": 601},
        {"model_max_retries": -1},
        {"model_max_retries": 4},
        {"model_retry_delay_seconds": -1},
    ],
)
def test_invalid_budget_configuration_is_rejected(values) -> None:
    with pytest.raises(ValueError):
        LimitSettings(_env_file=None, **values)


@pytest.mark.parametrize("base_url", ["", "localhost:8000/v1", "ftp://localhost/v1"])
def test_invalid_endpoint_is_rejected_before_any_attempt(base_url) -> None:
    with pytest.raises(ValueError, match="MODEL_BASE_URL"):
        BIModelClient("fixture-key", base_url, "fixture-model")


@pytest.mark.parametrize("key", ["", " ", "key\r\nInjected: value"])
def test_invalid_key_is_rejected_before_any_attempt(key) -> None:
    with pytest.raises(ValueError, match="MODEL_API_KEY"):
        BIModelClient(key, "http://localhost:8000/v1", "fixture-model")


def test_deadline_is_not_relabelled_as_cancelled_by_stream_cleanup() -> None:
    budget = ExecutionBudget(1, lambda: True)
    budget.deadline = monotonic() - 1
    with pytest.raises(TurnTimeout):
        budget.check()
