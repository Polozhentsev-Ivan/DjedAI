"""Diagnostics use the actual SDK retry transport and graph on a controlled HTTP server."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from openai import DefaultAsyncHttpxClient
from sqlalchemy.orm import sessionmaker

from djedai.agent.client import BIModelClient
from djedai.agent.diagnostics import DiagnosticHttpClient, RunDiagnostics, Tariff
from djedai.agent.graph import build_agent_graph
from djedai.backend.chat_service import AgentRuntime, stream_chat_turn
from djedai.backend.config import Settings
from djedai.backend.db.repositories import (
    create_chat,
    create_dataset,
    get_chat_messages,
    get_or_create_user,
)
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.diagnostics import DiagnosticJournal, read_events
from djedai.contracts.agent import Understanding
from djedai.contracts.tool_result import ToolResult, ToolResultData
from djedai.diagnostics import main as diagnostic_cli

SECRET = "secret-header-prompt-cell-and-provider-body"


def completion(content: str, *, usage: bool = True, finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "diagnostic-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 7},
        }
        if usage
        else None,
    }


@contextmanager
def model_server(
    answer: Callable[[int, dict[str, Any]], tuple[int, dict[str, Any], float]],
    *,
    timeout: float = 2,
) -> Iterator[tuple[BIModelClient, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                calls.append(payload)
                number = len(calls)
            status, body, delay = answer(number, payload)
            time.sleep(delay)
            encoded = json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except OSError:
                pass  # A timed-out client may have closed its socket.

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with patch(
        "djedai.agent.client.DiagnosticHttpClient",
        lambda: DiagnosticHttpClient(trust_env=False),
    ):
        client = BIModelClient(
            api_key=SECRET,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model_name="test-model",
            timeout=timeout,
            max_retries=2,
            retry_delay=0.01,
        )
    try:
        with patch(
            "djedai.agent.transport.DefaultAsyncHttpxClient",
            lambda: DefaultAsyncHttpxClient(trust_env=False),
        ):
            yield client, calls
    finally:
        client.client.close()
        server.shutdown()
        server.server_close()
        thread.join()


def recording(directory: Path, owner: str = "owner") -> RunDiagnostics:
    return RunDiagnostics(
        DiagnosticJournal(directory, 1_000_000, 3),
        owner_id=owner,
        chat_id=f"chat_{owner}",
        dataset_id=f"dataset_{owner}",
        user_message_id=f"message_{owner}",
        tariff=Tariff(Decimal("0.1"), Decimal("0.95"), "2026-10-23 fixture rates"),
    )


def test_model_retries_are_individually_recorded_without_contents(tmp_path: Path) -> None:
    def answer(number: int, _payload: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
        if number < 3:
            return (429 if number == 1 else 503), {"error": {"message": SECRET}}, 0
        return 200, completion('{"intent":"data_question"}'), 0

    run = recording(tmp_path)
    with model_server(answer) as (client, calls), run.stage("understand"):
        result = client.generate_typed_response(SECRET, SECRET, Understanding)
    run.finish("success")
    assert result.status == "success"
    assert len(calls) == 3
    events = read_events(tmp_path, run.run_id)
    attempts = [e for e in events if e["event"] == "model_attempt_finished"]
    assert [e["attempt"] for e in attempts] == [1, 2, 3]
    assert [e["http_status"] for e in attempts] == [429, 503, 200]
    assert all(e["duration_ms"] >= 0 and e["node"] == "understand" for e in attempts)
    assert len({e["call_id"] for e in attempts}) == 1
    call = next(e for e in events if e["event"] == "model_call_finished")
    assert (call["input_tokens"], call["output_tokens"], call["reasoning_tokens"]) == (100, 20, 7)
    assert Decimal(call["estimated_cost_usd"]) == Decimal("0.000029")
    assert call["actual_charge_usd"] is None
    assert SECRET not in json.dumps(events)


@pytest.mark.parametrize(
    ("body", "error"),
    [("{" + SECRET, "INVALID_JSON"), ('{"intent":"wrong"}', "SCHEMA_MISMATCH")],
)
def test_rejected_output_still_retains_usage(tmp_path: Path, body: str, error: str) -> None:
    run = recording(tmp_path)
    with model_server(lambda *_: (200, completion(body), 0)) as (client, _), run.stage("plan"):
        result = client.generate_typed_response(SECRET, SECRET, Understanding)
    run.finish("error", result.error_type)
    events = read_events(tmp_path)
    call = next(e for e in events if e["event"] == "model_call_finished")
    assert call["error_type"] == error
    assert call["output_tokens"] == 20
    assert events[-1]["trace"][-1]["node"] == "plan"
    assert events[-1]["trace"][-1]["error_type"] == error
    assert SECRET not in json.dumps(events)


def test_missing_usage_is_unknown_not_zero(tmp_path: Path) -> None:
    run = recording(tmp_path)
    with (
        model_server(
            lambda *_: (200, completion('{"intent":"data_question"}', usage=False), 0)
        ) as (client, _),
        run.stage("understand"),
    ):
        client.generate_typed_response("system", "question", Understanding)
    run.finish("success")
    call = next(e for e in read_events(tmp_path) if e["event"] == "model_call_finished")
    assert call["input_tokens"] is call["output_tokens"] is call["reasoning_tokens"] is None
    assert call["estimated_cost_usd"] is None


@pytest.mark.parametrize("failed_stage", ["understand", "plan", "verify", "cancelled", None])
def test_real_graph_preserves_failed_stage_and_trace_after_restart(
    tmp_path: Path, failed_stage: str | None
) -> None:
    bodies = {
        "Understanding": {"intent": "data_question"},
        "PlanResponse": {"plan": [{"tool": "run_sql", "args": {"sql": "SELECT 1"}}]},
        "ComposeResponse": {"answer_blocks": [{"type": "text", "markdown": "Одна строка"}]},
        "VerifyResponse": {"is_valid": True, "corrected_markdown": ""},
    }
    stages = dict(zip(bodies, ["understand", "plan", "compose", "verify"], strict=True))
    cancelled = threading.Event()

    def answer(_number: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
        schema = payload["response_format"]["json_schema"]["name"]
        if failed_stage == "cancelled" and schema == "Understanding":
            cancelled.set()
        delay = 0.5 if stages[schema] == failed_stage else 0
        return 200, completion(json.dumps(bodies[schema])), delay

    engine = create_db_engine(f"sqlite:///{tmp_path / 'app.db'}")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    directory = tmp_path / "journal"
    try:
        with factory() as db:
            get_or_create_user(db, "owner", "owner")
            create_dataset(db, "dataset", "owner", "Test")
            create_chat(db, "chat", "owner", "dataset")
        timeout = 0.2 if failed_stage in {"understand", "plan", "verify"} else 2
        with model_server(answer, timeout=timeout) as (client, calls), factory() as db:
            runtime = AgentRuntime(
                graph=build_agent_graph(),
                client=client,
                tool_dispatcher=lambda *_: ToolResult(
                    result_id="result_test",
                    tool="run_sql",
                    status="ok",
                    data=ToolResultData(columns=["n"], rows=[[1]], row_count=1),
                ),
                diagnostics=DiagnosticJournal(directory, 1_000_000, 3),
            )
            events = list(
                stream_chat_turn(
                    db, "chat", SECRET, runtime, is_cancelled=cancelled.is_set, owner_id="owner"
                )
            )
            expected_event = (
                "status" if failed_stage == "cancelled" else "error" if failed_stage else "done"
            )
            assert events[-1].event == expected_event
            assert len(get_chat_messages(db, "chat")) == (1 if failed_stage else 2)
        engine.dispose()
        # A fresh reader, without retaining a recorder or app object, sees durable history.
        saved = read_events(directory)
        finished = saved[-1]
        assert finished["event"] == "run_finished"
        status = (
            "cancelled" if failed_stage == "cancelled" else "timeout" if failed_stage else "success"
        )
        assert finished["status"] == status
        if failed_stage == "cancelled":
            assert finished["error_type"] == "CANCELLED"
            assert len(calls) == 1
            assert [s["node"] for s in finished["trace"]] == ["load_ctx", "understand"]
        elif failed_stage:
            assert finished["error_type"] == "TIMEOUT"
            assert finished["error_node"] == failed_stage
            assert failed_stage in [s["node"] for s in finished["trace"]]
            attempts = [
                e
                for e in saved
                if e["event"] == "model_attempt_finished" and e["node"] == failed_stage
            ]
            assert len(attempts) == 3
            assert all(a["error_type"] == "TIMEOUT" for a in attempts)
        else:
            assert len(calls) == 4
            assert [e["node"] for e in saved if e["event"] == "model_call_finished"] == [
                "understand",
                "plan",
                "compose",
                "verify",
            ]
        assert saved[0]["user_message_id"].startswith("m_")
        assert SECRET not in json.dumps(saved)
    finally:
        engine.dispose()


def test_concurrent_runs_do_not_share_model_context(tmp_path: Path) -> None:
    runs = [recording(tmp_path, owner) for owner in ("alice", "bob")]
    with model_server(lambda *_: (200, completion('{"intent":"data_question"}'), 0.02)) as (
        client,
        _,
    ):

        def invoke(run: RunDiagnostics) -> None:
            with run.stage("understand"):
                client.generate_typed_response(SECRET, SECRET, Understanding)
            run.finish("success")

        with ThreadPoolExecutor(2) as pool:
            list(pool.map(invoke, runs))
        count_before = len(read_events(tmp_path))
        client.generate_typed_response("not a chat", "probe", Understanding)
        assert len(read_events(tmp_path)) == count_before
    for run in runs:
        own = read_events(tmp_path, run.run_id)
        assert len([e for e in own if e["event"] == "model_attempt_finished"]) == 1
        assert {e["owner_id"] for e in own} == {run.context["owner_id"]}


def test_journal_rotation_and_partial_write(tmp_path: Path) -> None:
    journal = DiagnosticJournal(tmp_path, 100, 2)
    for index in range(12):
        journal({"run_id": str(index), "event": "fixture", "padding": "x" * 30})
    assert len(list(tmp_path.glob("agent.jsonl*"))) == 3
    assert [e["run_id"] for e in read_events(tmp_path)] == ["9", "10", "11"]
    with journal.path.open("ab") as handle:
        handle.write(b'{"interrupted":"\xf0\x9f')
    assert len(read_events(tmp_path)) == 3
    DiagnosticJournal(tmp_path, 1000, 2)({"run_id": "after_restart"})
    assert read_events(tmp_path)[-1]["run_id"] == "after_restart"


def test_storage_failure_is_safe_and_visible(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / SECRET
    blocked.write_text("file instead of directory")
    DiagnosticJournal(blocked, 1000, 1)({"run_id": "r"})
    assert "Agent diagnostics unavailable" in caplog.text
    assert SECRET not in caplog.text


def test_tariff_must_be_complete_and_finite() -> None:
    assert (
        Settings(
            _env_file=None, model_input_price_usd="", model_output_price_usd=""
        ).model_input_price_usd
        is None
    )
    with pytest.raises(ValueError, match="MODEL_PRICE_REFERENCE"):
        Settings(_env_file=None, model_input_price_usd="0.1")
    for invalid in ("-1", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            Settings(
                _env_file=None,
                model_input_price_usd=invalid,
                model_output_price_usd="0.95",
                model_price_reference="2026-10-23 fixture",
            )


def test_stage_exception_preserves_type_without_message(tmp_path: Path) -> None:
    run = recording(tmp_path)
    with pytest.raises(ValueError), run.stage("plan"):
        raise ValueError(SECRET)
    run.finish("error", run.last_error)
    events = read_events(tmp_path)
    assert events[-2]["exception_type"] == "ValueError"
    assert events[-1]["error_node"] == "plan"
    assert SECRET not in json.dumps(events)


def test_operator_cli_filters_a_retained_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = recording(tmp_path, "alice"), recording(tmp_path, "bob")
    first.finish("success")
    second.finish("cancelled", "CANCELLED")
    monkeypatch.setattr(
        "djedai.diagnostics.get_settings", lambda: SimpleNamespace(diagnostics_dir=tmp_path)
    )
    monkeypatch.setattr(sys, "argv", ["diagnostics", "--run", second.run_id])
    diagnostic_cli()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 2
    assert {e["owner_id"] for e in events} == {"bob"}
