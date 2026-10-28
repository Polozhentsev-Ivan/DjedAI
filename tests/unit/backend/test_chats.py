"""Тесты API чатов, SSE-событий агента и сохранения истории."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy.orm import Session, sessionmaker

import djedai.agent.graph as agent_graph
from djedai.agent.client import BIModelClient, ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.backend.app import create_app
from djedai.backend.auth import create_session, hash_password
from djedai.backend.chat_service import AgentRuntime, get_agent_runtime, stream_chat_turn
from djedai.backend.config import Settings, get_settings
from djedai.backend.db.models import ChatModel, MessageModel, ToolResultModel
from djedai.backend.db.repositories import (
    create_chat,
    create_dashboard,
    create_dataset,
    get_chat_messages,
    get_or_create_user,
    save_assistant_message,
    save_message,
    save_tool_result,
    update_chat_memory,
)
from djedai.backend.db.session import create_db_engine, get_db, get_session_factory, init_db
from djedai.backend.diagnostics import DiagnosticJournal, read_events
from djedai.backend.sse import encode_sse_event
from djedai.contracts.agent import Block
from djedai.contracts.chart import ChartSpec, XAxis, YMeasure
from djedai.contracts.chat import ChatTurnState
from djedai.contracts.message import AssistantMessage, ChartBlock, MessageBlock
from djedai.contracts.sse import AgentStreamEvent, SSEBlockEvent
from djedai.contracts.tool_result import Fact, ToolError, ToolResult, ToolResultData

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "chat" / "blocks.json"


@pytest.fixture(scope="module")
def account_password_hash() -> str:
    return hash_password("test-chat-password")


class FakeGraph:
    """Управляемый граф: отдаёт заранее заданные обновления узлов или падает."""

    def __init__(
        self,
        updates: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.updates = updates or []
        self.error = error
        self.calls: list[tuple[dict[str, Any], dict[str, Any], str | list[str]]] = []

    def stream(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
        stream_mode: str | list[str],
    ) -> Iterator[Any]:
        self.calls.append((state, config, stream_mode))
        if self.error is not None:
            raise self.error
        for update in self.updates:
            if isinstance(stream_mode, list):
                for node in update:
                    yield "custom", {"node": node, "phase": "stage"}
                yield "updates", update
            else:
                yield update


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Разбирает wire-формат SSE на пары (event, data)."""
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        name = ""
        data: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data.append(line.removeprefix("data:").lstrip())
        events.append((name, json.loads("\n".join(data))))
    return events


@pytest.fixture
def chat_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_password_hash: str
) -> Iterator[tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'app.db'}")
    init_db(engine)
    factory = sessionmaker(bind=engine)

    with factory() as db:
        user = get_or_create_user(db, "u_chat", "chat_owner")
        user.password_hash = account_password_hash
        db.commit()
        create_dataset(db, "ds_chat", user.id, "Чат-датасет")
        token = create_session(db, user.id)

    monkeypatch.setenv("RESULTS_DATA_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    app = create_app(Settings(_env_file=None))
    app.state.session_factory = factory

    def override_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def use_runtime(runtime: AgentRuntime) -> None:
        app.dependency_overrides[get_agent_runtime] = lambda: runtime

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_factory] = lambda: factory
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            yield client, factory, use_runtime
    finally:
        get_settings.cache_clear()
        engine.dispose()


def _runtime(*updates: dict[str, Any], error: Exception | None = None) -> AgentRuntime:
    return AgentRuntime(graph=FakeGraph(list(updates), error=error))


def _agent_blocks(raw_blocks: list[dict[str, Any]]) -> list[Block]:
    blocks: list[Block] = []
    for raw in raw_blocks:
        data = dict(raw)
        if data["type"] == "clarification":
            data["markdown"] = data.pop("question")
        blocks.append(Block.model_validate(data))
    return blocks


def _create_chat(client: TestClient) -> str:
    response = client.post("/api/chats", json={"dataset_id": "ds_chat"})
    assert response.status_code == 201
    return response.json()["chat_id"]


def test_create_list_and_read_chat(chat_api: tuple[TestClient, Any, Any]) -> None:
    client, _factory, _use_runtime = chat_api

    chat_id = _create_chat(client)

    listed = client.get("/api/chats", params={"dataset_id": "ds_chat"})
    assert listed.status_code == 200
    chats = listed.json()
    assert [chat["chat_id"] for chat in chats] == [chat_id]
    assert chats[0]["dataset_id"] == "ds_chat"
    assert chats[0]["title"] == "Новый чат"
    assert chats[0]["created_at"]

    detail = client.get(f"/api/chats/{chat_id}")
    assert detail.status_code == 200
    assert detail.json()["chat_id"] == chat_id
    assert detail.json()["messages"] == []


def test_chat_api_reports_missing_resources(chat_api: tuple[TestClient, Any, Any]) -> None:
    client, _factory, _use_runtime = chat_api

    missing_dataset = client.post("/api/chats", json={"dataset_id": "ds_unknown"})
    assert missing_dataset.status_code == 404

    missing_chat = client.get("/api/chats/c_unknown")
    assert missing_chat.status_code == 404

    unknown_message = client.post(
        "/api/chats/c_unknown/messages",
        json={"text": "Покажи выручку"},
    )
    assert unknown_message.status_code == 404


@pytest.mark.parametrize("rejected", [False, True])
def test_verify_replaces_candidate_before_saving_or_publishing(chat_api, rejected):
    client, _factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    verified = Block(type="error" if rejected else "text", markdown="Проверенный ответ")
    use_runtime(
        _runtime(
            {
                "compose": {
                    "answer_blocks": [Block(type="text", markdown="Непроверенная цифра 999")]
                }
            },
            {"verify": {"answer_blocks": [verified]}},
        )
    )
    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Сколько?"})
    events = parse_sse(response.text)
    assert "Непроверенная цифра" not in response.text
    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    if rejected:
        assert events[-1][0] == "error"
        assert len(messages) == 1
    else:
        assert events[-1][0] == "done"
        assert messages[-1]["blocks"] == [{"type": "text", "markdown": "Проверенный ответ"}]


def test_send_message_streams_events_and_saves_history(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)

    ok_result = ToolResult(
        result_id="r_chat_ok",
        status="ok",
        tool="run_sql",
        params={"sql": "SELECT 1 AS value"},
        sql="SELECT 1 AS value",
        data=ToolResultData(
            columns=["value"],
            rows=[[1]],
            row_count=1,
            truncated=False,
        ),
        facts=[Fact(text="Получена одна строка", kind="summary", values={"row_count": 1})],
    )
    failed_result = ToolResult(
        result_id="r_chat_err",
        status="error",
        tool="describe_column",
        params={"table": "orders", "column": "missing"},
        error=ToolError(
            code="invalid_column",
            message="Колонка 'missing' не найдена",
            suggestion="Проверьте имя колонки",
        ),
    )
    graph = FakeGraph(
        [
            {"load_ctx": {"catalog_prompt": "orders"}},
            {"understand": {"understanding": None}},
            {"plan": {"plan": [], "retries": 1}},
            {"execute": {"results": [ok_result, failed_result]}},
            {"visualize": {"charts": []}},
            {"compose": {"answer_blocks": [Block(type="text", markdown="Готово")]}},
            {"verify": {}},
        ]
    )
    use_runtime(AgentRuntime(graph=graph, tool_dispatcher=None))

    response = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"text": "Покажи выручку"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    events = parse_sse(response.text)
    names = [name for name, _data in events]

    assert names[0] == "status"
    assert names[-1] == "done"
    assert "tool_call" in names
    assert "tool_result" in names
    assert "block" in names

    statuses = [data for name, data in events if name == "status"]
    assert [status["node"] for status in statuses] == [
        "load_ctx",
        "load_ctx",
        "understand",
        "plan",
        "execute",
        "visualize",
        "compose",
        "verify",
    ]

    tool_calls = [data for name, data in events if name == "tool_call"]
    assert [call["tool"] for call in tool_calls] == ["run_sql", "describe_column"]
    tool_results = [data for name, data in events if name == "tool_result"]
    assert tool_results[0] == {"result_id": "r_chat_ok", "ok": True, "error": None}
    assert tool_results[1]["result_id"] == "r_chat_err"
    assert tool_results[1]["ok"] is False
    assert "не найдена" in tool_results[1]["error"]

    blocks = [data for name, data in events if name == "block"]
    assert blocks == [{"type": "text", "markdown": "Готово"}]

    done = next(data for name, data in events if name == "done")
    assert done["message_id"].startswith("m_")

    # У графа был отдельный thread на чат и сохранённый вопрос в истории.
    assert graph.calls[0][1]["configurable"]["thread_id"] == chat_id
    assert graph.calls[0][2] == ["custom", "updates"]
    assert [message.content for message in graph.calls[0][0]["messages"]] == ["Покажи выручку"]

    with factory() as db:
        messages = get_chat_messages(db, chat_id)
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[0].content == "Покажи выручку"
        assert messages[1].message_id == done["message_id"]
        assert messages[1].blocks == [{"type": "text", "markdown": "Готово"}]
        assert messages[1].result_ids == ["r_chat_ok"]

        saved_results = db.query(ToolResultModel).order_by(ToolResultModel.result_id).all()
        assert [result.result_id for result in saved_results] == ["r_chat_err", "r_chat_ok"]
        assert all(result.chat_id == chat_id for result in saved_results)

    # Повторное открытие чата возвращает сохранённую историю и provenance.
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["text"] == "Покажи выручку"
    assert detail["messages"][1]["blocks"] == [{"type": "text", "markdown": "Готово"}]
    assert detail["messages"][1]["result_ids"] == ["r_chat_ok"]


def _reused_chart() -> ChartSpec:
    return ChartSpec(
        chart_id="ch_reused",
        result_id="r_reused",
        type="bar",
        title="Категории",
        x=XAxis(field="category"),
        y=[YMeasure(field="value")],
    )


def test_chart_change_reuses_result_after_it_leaves_the_history_window(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    """Смена вида не теряет расчёт, когда исходный ответ ушёл из последних 10 сообщений."""
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    saved = ToolResult(
        result_id="r_reused",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["category", "value"], rows=[["A", 10], ["B", 20]]),
    )
    chart = _reused_chart()
    with factory() as db:
        save_tool_result(db, saved, chat_id=chat_id, owner_id="u_chat", persist_data=False)

    first = FakeGraph(
        [
            {"execute": {"results": [saved]}},
            {"compose": {"answer_blocks": [Block(type="text", markdown="Готово")]}},
        ]
    )
    use_runtime(AgentRuntime(graph=first))
    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Покажи категории"})
    assert parse_sse(response.text)[-1][0] == "done"

    # Шесть смен вида без пересчёта: исходный ответ выходит за границу истории.
    for _ in range(6):
        graph = FakeGraph(
            [{"compose": {"answer_blocks": [Block(type="chart", chart=chart.model_dump())]}}]
        )
        use_runtime(AgentRuntime(graph=graph))
        response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Покажи линейным"})
        assert parse_sse(response.text)[-1][0] == "done"
        assert [result.result_id for result in graph.calls[0][0]["history_results"]] == ["r_reused"]

    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert messages[-1]["result_ids"] == ["r_reused"]
    assert messages[-1]["blocks"][0]["chart"]["result_id"] == "r_reused"


def test_focus_survives_text_refinements_after_its_message_leaves_window(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    """Сохранённый фокус загружается, даже когда его сообщение ушло из последних 10."""
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    saved = ToolResult(
        result_id="r_kept",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["value"], rows=[[42]]),
    )
    with factory() as db:
        save_tool_result(db, saved, chat_id=chat_id, owner_id="u_chat", persist_data=False)

    first = FakeGraph(
        [
            {"execute": {"results": [saved]}},
            {"compose": {"answer_blocks": [Block(type="text", markdown="42")]}},
        ]
    )
    use_runtime(AgentRuntime(graph=first))
    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Посчитай 42"})
    assert parse_sse(response.text)[-1][0] == "done"

    # Six text-only refinements: no new results, so the original assistant
    # message with r_kept drops out of the HISTORY_WINDOW.
    for index in range(6):
        graph = FakeGraph(
            [{"compose": {"answer_blocks": [Block(type="text", markdown=f"Уточнение {index}")]}}]
        )
        use_runtime(AgentRuntime(graph=graph))
        response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Продолжай"})
        assert parse_sse(response.text)[-1][0] == "done"

    graph = FakeGraph([{"compose": {"answer_blocks": [Block(type="text", markdown="Готово")]}}])
    use_runtime(AgentRuntime(graph=graph))
    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Продолжай"})
    assert parse_sse(response.text)[-1][0] == "done"

    state = graph.calls[0][0]
    assert state["focus_result_ids"] == ["r_kept"]
    assert [result.result_id for result in state["history_results"]] == ["r_kept"]
    assert len(state["messages"]) == 10
    assert all("42" not in message.content for message in state["messages"])


def test_history_loads_result_referenced_by_saved_chart_block(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    """Сохранённый график без result_ids всё равно приносит свой результат в следующий ход."""
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    chart = _reused_chart()
    with factory() as db:
        save_tool_result(
            db,
            ToolResult(
                result_id="r_reused",
                status="ok",
                tool="run_sql",
                data=ToolResultData(columns=["category", "value"], rows=[["A", 10], ["B", 20]]),
            ),
            chat_id=chat_id,
            owner_id="u_chat",
            persist_data=False,
        )
        save_assistant_message(
            db,
            AssistantMessage(
                message_id="m_legacy_chart",
                chat_id=chat_id,
                created_at=datetime.now(UTC),
                blocks=[ChartBlock(chart=chart)],
                result_ids=[],
            ),
        )

    graph = FakeGraph(
        [{"compose": {"answer_blocks": [Block(type="chart", chart=chart.model_dump())]}}]
    )
    use_runtime(AgentRuntime(graph=graph))
    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Покажи линейным"})

    assert parse_sse(response.text)[-1][0] == "done"
    assert [result.result_id for result in graph.calls[0][0]["history_results"]] == ["r_reused"]


def test_send_message_streams_all_prepared_block_types(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
    prepared_blocks = _agent_blocks(fixture["blocks"])
    with factory() as db:
        save_tool_result(
            db,
            ToolResult(
                result_id="r_fixture",
                status="ok",
                tool="run_sql",
                data=ToolResultData(columns=["month", "revenue"], rows=[["2026-01", 120]]),
            ),
            chat_id=chat_id,
            owner_id="u_chat",
            persist_data=False,
        )
        create_dashboard(db, "d_fixture", "u_chat", "ds_chat", "Квартальная статистика")
    use_runtime(_runtime({"compose": {"answer_blocks": prepared_blocks}}))

    response = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"text": "Покажи всё, что умеешь"},
    )

    events = parse_sse(response.text)
    blocks = [data for name, data in events if name == "block"]
    assert [block["type"] for block in blocks] == [
        "text",
        "chart",
        "table",
        "clarification",
        "dashboard_link",
        "suggestions",
    ]

    detail = client.get(f"/api/chats/{chat_id}").json()
    assert [block["type"] for block in detail["messages"][1]["blocks"]] == [
        "text",
        "chart",
        "table",
        "clarification",
        "dashboard_link",
        "suggestions",
    ]


def test_prepared_event_fixtures_round_trip_through_wire_format() -> None:
    fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
    adapter = TypeAdapter(AgentStreamEvent)
    block_adapter = TypeAdapter(MessageBlock)

    raw_events = [
        *fixture["events"],
        *[{"event": "block", "data": block} for block in fixture["blocks"]],
    ]

    for raw in raw_events:
        event = adapter.validate_python(raw)
        frame = encode_sse_event(event)
        assert frame.endswith("\n\n")
        assert parse_sse(frame) == [(event.event, event.data.model_dump(mode="json"))]

    for block in fixture["blocks"]:
        assert SSEBlockEvent(data=block_adapter.validate_python(block)).event == "block"


def test_thread_error_is_streamed_as_error_event(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    use_runtime(_runtime(error=RuntimeError("model connection lost")))

    response = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"text": "Покажи выручку"},
    )

    assert response.status_code == 200
    events = parse_sse(response.text)
    assert [name for name, _data in events] == ["status", "error"]
    error = events[-1][1]
    assert error["message"] == "Не удалось получить ответ агента"
    assert "model connection lost" not in error["detail"]
    assert "повторите явно" in error["detail"]

    with factory() as db:
        messages = get_chat_messages(db, chat_id)
        assert [message.role for message in messages] == ["user"]


def test_agent_error_block_becomes_error_event(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, _factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    use_runtime(
        _runtime(
            {
                "compose": {
                    "answer_blocks": [
                        Block(type="error", markdown="Модель не ответила за 15 секунд")
                    ]
                }
            }
        )
    )

    response = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"text": "Покажи выручку"},
    )

    events = parse_sse(response.text)
    assert [name for name, _data in events] == ["status", "status", "error"]
    assert events[1][1]["node"] == "compose"
    assert events[-1][1]["code"] == "agent_error"
    assert "15 секунд" not in events[-1][1]["detail"]


def test_send_message_rejects_blank_text(
    chat_api: tuple[TestClient, Any, Any],
) -> None:
    client, _factory, use_runtime = chat_api
    # Request validation is independent of a developer's local model credentials.
    use_runtime(_runtime())
    chat_id = _create_chat(client)

    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "   "})

    assert response.status_code == 422


def test_chat_ids_are_unique(chat_api: tuple[TestClient, Any, Any]) -> None:
    client, _factory, _use_runtime = chat_api
    ids = {_create_chat(client) for _ in range(5)}

    assert len(ids) == 5
    assert all(chat_id.startswith("c_") for chat_id in ids)


class BarrierFakeGraph:
    """Управляемый граф с барьером для проверки unbuffered-потока, неблокирующего цикла и отмены."""

    def __init__(
        self,
        updates: list[dict[str, Any]] | None = None,
        barrier: threading.Event | None = None,
    ) -> None:
        self.updates = updates or []
        self.barrier = barrier
        self.started = threading.Event()
        self.completed = threading.Event()

    def stream(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
        stream_mode: str,
    ) -> Iterator[dict[str, Any]]:
        self.started.set()
        is_cancelled = config.get("configurable", {}).get("is_cancelled")
        if self.barrier is not None:
            while not self.barrier.is_set():
                if is_cancelled and is_cancelled():
                    return
                time.sleep(0.01)

        for update in self.updates:
            if is_cancelled and is_cancelled():
                return
            yield update
        self.completed.set()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_chat_deadline_is_terminal_and_next_question_recovers(chat_api) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    graph = BarrierFakeGraph(barrier=threading.Event())
    use_runtime(AgentRuntime(graph=graph, turn_timeout_seconds=0.2))
    started = time.monotonic()
    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "slow"})
    assert time.monotonic() - started < 1.5
    events = parse_sse(response.text)
    assert [name for name, _ in events].count("error") == 1
    assert events[-1][1]["code"] == "turn_timeout"
    assert not any(name in {"done", "block"} for name, _ in events)
    with factory() as db:
        assert [m.role for m in get_chat_messages(db, chat_id)] == ["user"]
    use_runtime(_runtime({"compose": {"answer_blocks": [Block(type="text", markdown="OK")]}}))
    next_response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "next"})
    assert parse_sse(next_response.text)[-1][0] == "done"


def _network_runtime(endpoint: Any, seconds: float) -> AgentRuntime:
    return AgentRuntime(
        graph=build_agent_graph(),
        client=BIModelClient(
            "fixture-key", endpoint.base_url, "fixture-model", timeout=5, max_retries=1
        ),
        tool_dispatcher=lambda *_: ToolResult(
            result_id="network-fixture",
            tool="run_sql",
            status="ok",
            data=ToolResultData(columns=["n"], rows=[[1]]),
            facts=[Fact(text="1 строка", values={"n": 1})],
        ),
        turn_timeout_seconds=seconds,
    )


def test_deadline_during_real_verify_never_publishes_draft(chat_api, model_endpoint) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)

    def reply(_number, payload):
        schema = payload["response_format"]["json_schema"]["name"]
        return {"mode": "stall"} if schema == "VerifyResponse" else {}

    with model_endpoint(reply) as endpoint:
        runtime = _network_runtime(endpoint, 1)
        use_runtime(runtime)
        try:
            started = time.monotonic()
            response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Сколько?"})
            assert time.monotonic() - started < 2.5
            events = parse_sse(response.text)
            assert [data["code"] for name, data in events if name == "error"] == ["turn_timeout"]
            assert all(name not in {"done", "block"} for name, _ in events)
            assert len(endpoint.calls) == 4  # understand, plan, compose, verify
            assert endpoint.peer_closed.wait(1)
            with factory() as db:
                assert [m.role for m in get_chat_messages(db, chat_id)] == ["user"]
        finally:
            runtime.client.client.close()


@pytest.mark.parametrize("lock_kind", ["writer", "reader"])
def test_sqlite_contention_never_saves_a_late_assistant(
    chat_api, model_endpoint, tmp_path, lock_kind: str
) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    with factory() as db:
        engine = db.get_bind()
        chat = db.get(ChatModel, chat_id)
        chat.summary = "Старое summary"
        chat.focus_result_ids = ["r_old"]
        db.commit()
    blocker = sqlite3.connect(engine.url.database, check_same_thread=False)
    locked, finished = threading.Event(), threading.Event()

    def reply(_number, payload):
        if payload["response_format"]["json_schema"]["name"] == "VerifyResponse":
            if not locked.is_set():
                if lock_kind == "writer":
                    blocker.execute("BEGIN IMMEDIATE")
                else:
                    blocker.execute("BEGIN")
                    blocker.execute("SELECT * FROM messages").fetchall()
                locked.set()
        return {}

    def tracked_factory():
        session = factory()
        close = session.close

        def tracked_close():
            close()
            if session.info.get("chat_turn_worker"):
                finished.set()

        session.close = tracked_close
        return session

    client.app.dependency_overrides[get_session_factory] = lambda: tracked_factory
    directory = tmp_path / "deadline-journal"
    try:
        with model_endpoint(reply) as endpoint:
            runtime = replace(
                _network_runtime(endpoint, 2),
                diagnostics=DiagnosticJournal(directory, 1_000_000, 2),
            )
            use_runtime(runtime)
            try:
                started = time.monotonic()
                response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Сколько?"})
                assert time.monotonic() - started < 3.5
                events = parse_sse(response.text)
                assert locked.is_set()
                assert [data["code"] for name, data in events if name == "error"] == [
                    "turn_timeout"
                ]
                assert all(name not in {"block", "done"} for name, _ in events)
                # The worker must close while the database lock is still held.
                assert finished.wait(0.5)
                blocker.rollback()
                with factory() as db:
                    assert [m.role for m in get_chat_messages(db, chat_id)] == ["user"]
                    chat = db.get(ChatModel, chat_id)
                    assert chat.summary == "Старое summary"
                    assert chat.focus_result_ids == ["r_old"]
                recorded = read_events(directory)
                assert recorded[-1]["status"] == "timeout"
                assert not any(
                    e.get("status") == "success" for e in recorded if e["event"] == "run_finished"
                )
            finally:
                runtime.client.client.close()

            # Releasing the lock cannot revive the old write, and a new question works.
            runtime = _network_runtime(endpoint, 5)
            use_runtime(runtime)
            try:
                next_response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "next"})
                assert parse_sse(next_response.text)[-1][0] == "done", next_response.text
                with factory() as db:
                    assert [m.role for m in get_chat_messages(db, chat_id)] == [
                        "user",
                        "user",
                        "assistant",
                    ]
            finally:
                runtime.client.client.close()
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.anyio
@pytest.mark.parametrize("logout", [False, True])
async def test_live_disconnect_or_logout_closes_model_request(
    chat_api, model_endpoint, logout: bool
) -> None:
    test_client, factory, use_runtime = chat_api
    chat_id = _create_chat(test_client)
    port = _free_port()

    with model_endpoint(lambda n, _: {"mode": "stall"} if n == 1 else {}) as endpoint:
        runtime = _network_runtime(endpoint, 5)
        use_runtime(runtime)
        server = uvicorn.Server(
            uvicorn.Config(test_client.app, host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            with anyio.fail_after(3):
                while not server.started:
                    await anyio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers=test_client.headers,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST", f"/api/chats/{chat_id}/messages", json={"text": "stop"}
                ) as response:
                    lines = response.aiter_lines()
                    with anyio.fail_after(3):
                        async for line in lines:
                            if line == "event: status":
                                break
                        while not endpoint.started.is_set():
                            await anyio.sleep(0.01)
                    started = time.monotonic()
                    if logout:
                        assert (await client.post("/api/auth/logout")).status_code == 204
                        tail = "\n".join([line async for line in lines])
                        assert '"code":"unauthorized"' in tail.replace(" ", "")
                        assert "event: done" not in tail
                    # Exiting the stream closes TCP, as fetch AbortController does.
                with anyio.fail_after(2):
                    while not endpoint.peer_closed.is_set():
                        await anyio.sleep(0.01)
                assert time.monotonic() - started < 2
                assert len(endpoint.calls) == 1
                # The model socket can close before the terminal state is committed.
                with anyio.fail_after(2):
                    while True:
                        with factory() as db:
                            messages = get_chat_messages(db, chat_id)
                            assert [m.role for m in messages] == ["user"]
                            if messages[0].turn["status"] != "running":
                                break
                        await anyio.sleep(0.01)
                assert messages[0].turn["code"] == (
                    "unauthorized" if logout else "stream_interrupted"
                )
                if logout:
                    login = await client.post(
                        "/api/auth/login",
                        json={"username": "chat_owner", "password": "test-chat-password"},
                    )
                    assert login.status_code == 200
                    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
                # An explicit new question runs all stages, including verify, successfully.
                runtime.client.client.close()
                runtime = _network_runtime(endpoint, 5)
                use_runtime(runtime)
                next_response = await client.post(
                    f"/api/chats/{chat_id}/messages", json={"text": "next"}
                )
                events = parse_sse(next_response.text)
                assert events[-1][0] == "done"
                assert len(endpoint.calls) == 5
        finally:
            server.should_exit = True
            await anyio.to_thread.run_sync(thread.join, 3)
            runtime.client.client.close()


@pytest.mark.anyio
async def test_stream_proxy_unbuffered_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_password_hash: str
) -> None:
    """Проверяет unbuffered-поток, неблокирующий event loop, ранний обрыв и AbortController."""
    backend_port = _free_port()
    proxy_port = _free_port()

    engine = create_db_engine(f"sqlite:///{tmp_path / 'live.db'}")
    init_db(engine)
    factory = sessionmaker(bind=engine)

    with factory() as db:
        user = get_or_create_user(db, "u_live", "live_owner")
        user.password_hash = account_password_hash
        db.commit()
        create_dataset(db, "ds_live", user.id, "Live DS")
        chat = create_chat(db, "c_live", user.id, "ds_live")
        token = create_session(db, user.id)
        db.refresh(chat)

    monkeypatch.setenv("RESULTS_DATA_DIR", str(tmp_path / "results_live"))
    get_settings.cache_clear()

    barrier1 = threading.Event()
    graph1 = BarrierFakeGraph(
        [
            {"plan": {}},
            {"execute": {}},
            {"compose": {"answer_blocks": [Block(type="text", markdown="Живой ответ")]}},
        ],
        barrier=barrier1,
    )
    current_graph = graph1

    backend_app = create_app(Settings(_env_file=None))
    backend_app.state.session_factory = factory

    def live_db() -> Iterator[Session]:
        with factory() as db:
            yield db

    backend_app.dependency_overrides[get_db] = live_db
    backend_app.dependency_overrides[get_session_factory] = lambda: factory
    backend_app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(graph=current_graph)

    # Настоящий сетевой Reverse Proxy (эмуляция Vite dev proxy / nginx по /api)
    proxy_app = FastAPI()
    proxy_client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}", trust_env=False)

    @proxy_app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def reverse_proxy(request: Request, path: str):
        body = await request.body()
        req = proxy_client.build_request(
            request.method,
            f"/api/{path}",
            headers=request.headers.raw,
            content=body,
        )
        resp = await proxy_client.send(req, stream=True)
        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers=resp.headers,
            media_type=resp.headers.get("content-type"),
        )

    s1 = uvicorn.Server(
        uvicorn.Config(backend_app, host="127.0.0.1", port=backend_port, log_level="error")
    )
    t1 = threading.Thread(target=s1.run, daemon=True)
    t1.start()

    s2 = uvicorn.Server(
        uvicorn.Config(proxy_app, host="127.0.0.1", port=proxy_port, log_level="error")
    )
    t2 = threading.Thread(target=s2.run, daemon=True)
    t2.start()

    while not (s1.started and s2.started):
        await anyio.sleep(0.05)

    try:
        # 1. Потоковое чтение через живой TCP proxy без буферизации + параллельный /api/health
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{proxy_port}",
            trust_env=False,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            async with client.stream(
                "POST",
                f"/api/chats/{chat.chat_id}/messages",
                json={"text": "Привет через живой прокси"},
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["cache-control"] == "no-store"
                assert response.headers.get("x-accel-buffering") == "no"

                raw_chunks: list[str] = []
                lines_iter = response.aiter_lines()
                first_event_found = False
                async for line in lines_iter:
                    if line:
                        raw_chunks.append(line)
                        if "event: status" in line:
                            first_event_found = True
                            break

                assert first_event_found, "Первое событие status должно быть получено клиентом"
                # КРИТЕРИЙ [P2]: Бэкенд удерживается на барьере и НЕ завершил ответ,
                # но клиент уже получил первый кадр по сети через proxy!
                assert not graph1.completed.is_set(), "Бэкенд не должен завершаться до барьера"
                assert not barrier1.is_set(), "Барьер ещё не должен быть снят"

                # КРИТЕРИЙ [P1]: Параллельный запрос к /api/health во время удержания графа
                # должен выполниться мгновенно (< 200мс), доказывая, что event loop не заблокирован
                t0 = time.perf_counter()
                health_resp = await client.get("/api/health")
                health_lat = time.perf_counter() - t0
                assert health_resp.status_code == 200
                assert health_resp.json()["status"] == "ok"
                assert health_lat < 0.2, (
                    f"/api/health задержан ({health_lat * 1000:.1f}мс), event loop заблокирован"
                )

                # Отпускаем барьер и дочитываем остаток потока
                barrier1.set()
                async for line in lines_iter:
                    if line:
                        raw_chunks.append(line)

        events = [
            line.removeprefix("event:").strip() for line in raw_chunks if line.startswith("event:")
        ]
        assert events[0] == "status"
        assert events[-1] == "done", raw_chunks
        assert graph1.completed.is_set(), "Граф должен штатно завершиться после снятия барьера"

        # 2. Реальный обрыв сокета клиентом через proxy ДО завершения агента
        barrier2 = threading.Event()
        graph2 = BarrierFakeGraph(
            [
                {"plan": {}},
                {
                    "compose": {
                        "answer_blocks": [Block(type="text", markdown="Не должно сохраниться")]
                    }
                },
            ],
            barrier=barrier2,
        )
        current_graph = graph2

        with socket.socket() as sock:
            sock.connect(("127.0.0.1", proxy_port))
            req_body = json.dumps({"text": "Обрыв до конца"}).encode("utf-8")
            req = (
                f"POST /api/chats/{chat.chat_id}/messages HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{proxy_port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Authorization: Bearer {token}\r\n"
                f"Content-Length: {len(req_body)}\r\n\r\n"
            ).encode() + req_body
            sock.sendall(req)

            received = b""
            while b"event: status" not in received:
                chunk = sock.recv(512)
                if not chunk:
                    break
                received += chunk

            assert b"200 OK" in received
            assert b"event: status" in received
            # Граф ещё удерживается на барьере и НЕ завершился
            assert not graph2.completed.is_set(), "Граф не должен завершиться до обрыва сокета"
            sock.close()

        await anyio.sleep(0.1)
        barrier2.set()
        await anyio.sleep(0.1)

        assert not graph2.completed.is_set(), "Граф не должен завершаться после обрыва"
        with factory() as db:
            messages = get_chat_messages(db, chat.chat_id)
            assistant_messages = [m for m in messages if m.role == "assistant"]
            assert len(assistant_messages) == 1  # Только от первого успешного запроса!

        # 3. Браузерный fetch через Node.js с прерыванием ДО завершения backend
        if shutil.which("node"):
            barrier3 = threading.Event()
            graph3 = BarrierFakeGraph(
                [
                    {"plan": {}},
                    {"compose": {"answer_blocks": [Block(type="text", markdown="Node отмена")]}},
                ],
                barrier=barrier3,
            )
            current_graph = graph3

            url = f"http://127.0.0.1:{proxy_port}/api/chats/{chat.chat_id}/messages"
            node_code = f"""
            const controller = new AbortController();
            const resp = await fetch('{url}', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json', Authorization: 'Bearer {token}' }},
                body: JSON.stringify({{ text: 'Fetch с отменой на барьере' }}),
                signal: controller.signal
            }});
            const reader = resp.body.getReader();
            const {{ value }} = await reader.read(); // Получили первый чанк до завершения
            controller.abort(); // Отмена до завершения backend!
            """
            p = subprocess.run(
                ["node", "--input-type=module", "-e", node_code],
                capture_output=True,
                text=True,
            )
            assert p.returncode == 0
            await anyio.sleep(0.1)
            barrier3.set()
            await anyio.sleep(0.1)
            assert not graph3.completed.is_set(), (
                "Граф не должен завершаться после Node AbortController"
            )
            with factory() as db:
                messages = get_chat_messages(db, chat.chat_id)
                assistant_messages = [m for m in messages if m.role == "assistant"]
                assert len(assistant_messages) == 1
    finally:
        s1.should_exit = True
        s2.should_exit = True
        await proxy_client.aclose()
        engine.dispose()


def test_stream_cleans_up_session_and_connections_on_complete_and_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_password_hash: str
) -> None:
    """Проверяет, что сессии и соединения закрываются после завершения и отмены (дефект [P2])."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'cleanup.db'}")
    init_db(engine)
    base_factory = sessionmaker(bind=engine)

    with base_factory() as db:
        user = get_or_create_user(db, "u_clean", "clean_owner")
        user.password_hash = account_password_hash
        db.commit()
        create_dataset(db, "ds_clean", user.id, "Датасет очистки")
        chat = create_chat(db, "c_clean", user.id, "ds_clean")
        token = create_session(db, user.id)
        db.refresh(chat)

    monkeypatch.setenv("RESULTS_DATA_DIR", str(tmp_path / "results_clean"))
    get_settings.cache_clear()

    app = create_app(Settings(_env_file=None))
    app.state.session_factory = base_factory

    open_sessions: set[Session] = set()

    def tracking_factory() -> Session:
        session = base_factory()
        open_sessions.add(session)
        orig_close = session.close

        def tracked_close() -> None:
            open_sessions.discard(session)
            orig_close()

        session.close = tracked_close
        return session

    def override_db() -> Iterator[Session]:
        session = tracking_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_factory] = lambda: tracking_factory
    app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
        graph=FakeGraph([{"compose": {"answer_blocks": [Block(type="text", markdown="OK")]}}])
    )

    try:
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            # 1. Четыре успешных запроса
            for i in range(4):
                res = client.post(
                    f"/api/chats/{chat.chat_id}/messages", json={"text": f"Вопрос {i}"}
                )
                assert res.status_code == 200
                assert "event: done" in res.text

            # 2. Два запроса с ошибкой агента
            app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
                graph=FakeGraph(error=RuntimeError("Имитация сбоя"))
            )
            for i in range(2):
                res = client.post(f"/api/chats/{chat.chat_id}/messages", json={"text": f"Сбой {i}"})
                assert res.status_code == 200
                assert "event: error" in res.text

            # 3. Два запроса с отменой клиентом (генератор прерывается до конца)
            app.dependency_overrides[get_agent_runtime] = lambda: AgentRuntime(
                graph=FakeGraph([{"plan": {}}, {"execute": {}}])
            )

            async def test_abort() -> None:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                    headers={"Authorization": f"Bearer {token}"},
                ) as c:
                    for i in range(2):
                        async with c.stream(
                            "POST",
                            f"/api/chats/{chat.chat_id}/messages",
                            json={"text": f"Отмена {i}"},
                        ) as r:
                            assert r.status_code == 200
                            async for line in r.aiter_lines():
                                if "event: status" in line:
                                    break
                            await r.aclose()

            import anyio

            anyio.run(test_abort)

        assert len(open_sessions) == 0, f"Остались незакрытые сессии: {len(open_sessions)}"
    finally:
        get_settings.cache_clear()
        engine.dispose()


def test_stream_initial_save_failure_emits_error_event(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    """Проверяет, что сбой начального save_message отдаёт event: error (дефект [P2])."""
    client, _factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    use_runtime(_runtime({"compose": {"answer_blocks": [Block(type="text", markdown="Успех")]}}))

    with patch(
        "djedai.backend.chat_service.save_message",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        response = client.post(
            f"/api/chats/{chat_id}/messages",
            json={"text": "Вопрос при заблокированной БД"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    assert len(events) == 1
    name, data = events[0]
    assert name == "error"
    assert data["message"] == "Не удалось получить ответ агента"
    assert "database is locked" not in data["detail"]


def test_worker_session_factory_failure_terminates_stream(chat_api) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    use_runtime(_runtime())

    def broken_factory():
        if threading.current_thread().name.startswith("chat-agent-"):
            raise RuntimeError("private database connection details")
        return factory()

    client.app.dependency_overrides[get_session_factory] = lambda: broken_factory
    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Сколько?"})
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["error"]
    assert "private database" not in response.text


@pytest.mark.anyio
async def test_stream_network_error_vs_event_error_distinguishable(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    """Проверяет различимость ошибки приложения (SSE error) и разрыва сети (обрыв соединения)."""
    test_client, _factory, use_runtime = chat_api
    app = test_client.app
    chat_id = _create_chat(test_client)

    # Случай 1: Ошибка приложения формирует валидное событие event: error
    use_runtime(_runtime(error=ValueError("Ошибка вычислений")))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=test_client.headers
    ) as client:
        async with client.stream(
            "POST", f"/api/chats/{chat_id}/messages", json={"text": "Тест ошибки"}
        ) as resp:
            content = await resp.aread()
            events = parse_sse(content.decode("utf-8"))
            event_names = [ev[0] for ev in events]
            assert "error" in event_names
            assert "done" not in event_names

    # Случай 2: Сетевой обрыв (клиент закрывает сокет до получения done/error)
    use_runtime(_runtime({"plan": {}}, {"execute": {}}, {"compose": {}}))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=test_client.headers
    ) as client:
        async with client.stream(
            "POST", f"/api/chats/{chat_id}/messages", json={"text": "Тест обрыва"}
        ) as resp:
            received_events: list[str] = []
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    received_events.append(line.removeprefix("event:").strip())
                    if len(received_events) == 1:
                        break
            await resp.aclose()
            assert "done" not in received_events
            assert "error" not in received_events


def test_turn_receives_summary_focus_and_previous_blocks_then_persists_memory(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    started = datetime(2026, 10, 1, tzinfo=UTC)

    with factory() as db:
        old_result = ToolResult(
            result_id="r_old",
            status="ok",
            tool="run_sql",
            data=ToolResultData(columns=["value"], rows=[[1]]),
        )
        save_tool_result(db, old_result, chat_id=chat_id, owner_id="u_chat", persist_data=False)
        update_chat_memory(
            db,
            chat_id,
            summary="Старое summary из хранилища",
            focus_result_ids=["r_old"],
        )
        for index in range(11):
            role = "user" if index % 2 == 0 else "assistant"
            blocks = None
            result_ids = None
            if role == "assistant":
                if index == 9:
                    blocks = [{"type": "table", "result_id": "r_old", "columns": ["value"]}]
                    result_ids = ["r_old"]
                else:
                    blocks = [{"type": "text", "markdown": f"Старый ответ {index}"}]
            save_message(
                db,
                message_id=f"m_old_{index}",
                chat_id=chat_id,
                role=role,
                content=f"Старое сообщение {index}",
                blocks=blocks,
                result_ids=result_ids,
                created_at=started + timedelta(minutes=index),
            )

    new_result = ToolResult(
        result_id="r_new",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["value"], rows=[[2]]),
    )
    graph = FakeGraph(
        [
            {"execute": {"results": [new_result]}},
            {"compose": {"answer_blocks": [Block(type="text", markdown="Новый ответ")]}},
        ]
    )
    use_runtime(AgentRuntime(graph=graph))

    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Новый вопрос"})
    assert parse_sse(response.text)[-1][0] == "done"

    state = graph.calls[0][0]
    assert len(state["messages"]) == 10
    assert state["messages"][-1].content == "Новый вопрос"
    assert "Старое сообщение 0" in state["history_summary"]
    assert state["focus_result_ids"] == ["r_old"]
    assert [result.result_id for result in state["history_results"]] == ["r_old"]
    assert state["previous_blocks"][0].type == "table"
    assert state["previous_blocks"][0].result_id == "r_old"

    with factory() as db:
        chat = db.get(ChatModel, chat_id)
        assert chat.summary is not None
        assert "Старое сообщение 0" in chat.summary
        assert chat.focus_result_ids == ["r_new"]

    restored = client.get(f"/api/chats/{chat_id}").json()
    assert len(restored["messages"]) == 13
    assert restored["messages"][-1]["blocks"] == [{"type": "text", "markdown": "Новый ответ"}]


def test_foreign_results_cannot_enter_chat_memory_context(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    with factory() as db:
        get_or_create_user(db, "u_other_memory", "other-memory")
        create_dataset(db, "ds_other_memory", "u_other_memory", "Чужой датасет")
        create_chat(db, "c_other_memory", "u_other_memory", "ds_other_memory")
        create_chat(db, "c_sibling_memory", "u_chat", "ds_chat")
        for result_id, owner_id, target_chat in (
            ("r_foreign", "u_other_memory", "c_other_memory"),
            ("r_sibling", "u_chat", "c_sibling_memory"),
        ):
            save_tool_result(
                db,
                ToolResult(result_id=result_id, status="ok", tool="run_sql"),
                chat_id=target_chat,
                owner_id=owner_id,
                persist_data=False,
            )
            save_message(
                db,
                message_id=f"m_{result_id}",
                chat_id=chat_id,
                role="assistant",
                content="Чужая ссылка",
                result_ids=[result_id],
            )
        update_chat_memory(
            db,
            chat_id,
            focus_result_ids=["r_foreign", "r_sibling"],
        )

    graph = FakeGraph([{"compose": {"answer_blocks": [Block(type="text", markdown="OK")]}}])
    use_runtime(AgentRuntime(graph=graph))

    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Обычный вопрос"})
    assert parse_sse(response.text)[-1][0] == "done"

    state = graph.calls[0][0]
    assert state["history_results"] == []
    assert state["focus_result_ids"] == []


def test_focus_loader_rejects_results_from_another_chat(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    """Явная загрузка фокуса не обходит проверки владельца, чата и датасета."""
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    with factory() as db:
        create_chat(db, "c_focus_sibling", "u_chat", "ds_chat")
        save_tool_result(
            db,
            ToolResult(result_id="r_focus_sibling", status="ok", tool="run_sql"),
            chat_id="c_focus_sibling",
            owner_id="u_chat",
            persist_data=False,
        )
        update_chat_memory(db, chat_id, focus_result_ids=["r_focus_sibling"])

    graph = FakeGraph([{"compose": {"answer_blocks": [Block(type="text", markdown="OK")]}}])
    use_runtime(AgentRuntime(graph=graph))

    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Продолжай"})
    assert parse_sse(response.text)[-1][0] == "done"

    state = graph.calls[0][0]
    assert state["history_results"] == []
    assert state["focus_result_ids"] == []


def test_two_chats_use_independent_summary_and_focus(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, factory, use_runtime = chat_api
    first_chat = _create_chat(client)
    second_chat = _create_chat(client)
    with factory() as db:
        for chat_id, result_id, summary in (
            (first_chat, "r_first_chat", "Первый чат"),
            (second_chat, "r_second_chat", "Второй чат"),
        ):
            save_tool_result(
                db,
                ToolResult(result_id=result_id, status="ok", tool="run_sql"),
                chat_id=chat_id,
                owner_id="u_chat",
                persist_data=False,
            )
            save_message(
                db,
                message_id=f"m_{result_id}",
                chat_id=chat_id,
                role="assistant",
                content="Сохранённый ответ",
                result_ids=[result_id],
            )
            update_chat_memory(db, chat_id, summary=summary, focus_result_ids=[result_id])

    graph = FakeGraph([{"compose": {"answer_blocks": [Block(type="text", markdown="OK")]}}])
    use_runtime(AgentRuntime(graph=graph))

    assert (
        client.post(f"/api/chats/{first_chat}/messages", json={"text": "Вопрос 1"}).status_code
        == 200
    )
    assert (
        client.post(f"/api/chats/{second_chat}/messages", json={"text": "Вопрос 2"}).status_code
        == 200
    )

    first_state = graph.calls[0][0]
    second_state = graph.calls[1][0]
    assert first_state["chat_id"] == first_chat
    assert second_state["chat_id"] == second_chat
    assert "Первый чат" in first_state["history_summary"]
    assert "Второй чат" in second_state["history_summary"]
    assert first_state["focus_result_ids"] == ["r_first_chat"]
    assert second_state["focus_result_ids"] == ["r_second_chat"]
    assert [result.result_id for result in first_state["history_results"]] == ["r_first_chat"]
    assert [result.result_id for result in second_state["history_results"]] == ["r_second_chat"]

    with factory() as db:
        assert db.get(ChatModel, first_chat).focus_result_ids == ["r_first_chat"]
        assert db.get(ChatModel, second_chat).focus_result_ids == ["r_second_chat"]


def test_failed_questions_keep_distinct_durable_outcomes(chat_api):
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    use_runtime(_runtime(error=RuntimeError("private-provider-response")))
    ids = []
    from uuid import uuid4

    for _ in range(3):
        request_id = uuid4()
        ids.append(f"m_{request_id.hex}")
        response = client.post(
            f"/api/chats/{chat_id}/messages",
            json={"text": "Одинаковый вопрос", "request_id": str(request_id)},
        )
        events = parse_sse(response.text)
        assert events[0][1]["message_id"] == ids[-1]
        assert events[-1][1]["code"] == "agent_error"
        assert "private-provider-response" not in response.text
    # A fresh HTTP read restores every outcome without creating an assistant.
    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert [m["message_id"] for m in messages] == ids
    assert all(m["role"] == "user" and m["turn"]["status"] == "error" for m in messages)
    assert len({m["turn"]["run_id"] for m in messages}) == 3
    assert all(m["turn"]["finished_at"] for m in messages)
    with factory() as db:
        assert len(get_chat_messages(db, chat_id)) == 3


@pytest.mark.parametrize(
    "node",
    ["load_ctx", "understand", "plan", "execute", "visualize", "compose", "verify", "clarify"],
)
def test_status_arrives_before_a_blocked_stage_finishes(chat_api, monkeypatch, node):
    client, factory, _ = chat_api
    chat_id = _create_chat(client)
    release, completed = threading.Event(), threading.Event()
    original = getattr(agent_graph, node)

    def held(*args):
        assert release.wait(5)
        result = original(*args)
        completed.set()
        return result

    monkeypatch.setattr(agent_graph, node, held)

    class HeldClient:
        def generate_typed_response(self, system_prompt, user_prompt, response_model, **kwargs):
            if response_model.__name__ == "Understanding":
                data = {"intent": "ambiguous" if node == "clarify" else "data_question"}
            elif response_model.__name__ == "PlanResponse":
                data = {"plan": []}
            elif response_model.__name__ == "ComposeResponse":
                data = {"answer_blocks": [{"type": "text", "markdown": "Готово"}]}
            else:
                data = {"is_valid": True, "corrected_markdown": ""}
            return ModelResponse(status="success", data=response_model.model_validate(data))

    runtime = AgentRuntime(graph=build_agent_graph(), client=HeldClient())
    with factory() as db:
        stream = stream_chat_turn(db, chat_id, "Вопрос", runtime, owner_id="u_chat")
        try:
            assert next(stream).data.node == "load_ctx"  # Acceptance precedes graph execution.
            events = []
            for event in stream:
                events.append(event)
                if event.event == "status" and event.data.node == node:
                    break
            assert events[-1].data.node == node
            assert not completed.is_set()
            question = client.get(f"/api/chats/{chat_id}").json()["messages"][0]
            assert question["turn"]["node"] == node
            assert question["turn"]["status"] == "running"
            release.set()
            assert list(stream)[-1].event == "done"
        finally:
            release.set()
            stream.close()


@pytest.mark.parametrize(
    "mode,code,status",
    [
        ("stall", "model_timeout", "timeout"),
        ("unavailable", "model_unavailable", "error"),
    ],
)
def test_model_failures_have_normalized_persisted_reasons(
    chat_api, model_endpoint, mode, code, status
):
    client, _, use_runtime = chat_api
    chat_id = _create_chat(client)
    with model_endpoint(
        lambda *_: {"mode": "stall"} if mode == "stall" else {"status": 503}
    ) as endpoint:
        runtime = _network_runtime(endpoint, 10)
        runtime.client.timeout = 0.15
        runtime.client.max_retries = 0
        use_runtime(runtime)
        try:
            events = parse_sse(
                client.post(f"/api/chats/{chat_id}/messages", json={"text": "Вопрос"}).text
            )
            assert events[-1][1]["code"] == code
            assert not any(name in {"done", "block"} for name, _ in events)
            saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
            assert len(saved) == 1
            assert saved[0]["turn"]["code"] == code
            assert saved[0]["turn"]["status"] == status
        finally:
            runtime.client.client.close()


def test_retry_pause_and_resumed_stage_are_streamed_in_order(chat_api, model_endpoint):
    client, _, use_runtime = chat_api
    chat_id = _create_chat(client)
    with model_endpoint(lambda n, _: {"status": 503} if n == 1 else {}) as endpoint:
        runtime = _network_runtime(endpoint, 5)
        runtime.client.retry_delay = 0.05
        use_runtime(runtime)
        try:
            events = parse_sse(
                client.post(f"/api/chats/{chat_id}/messages", json={"text": "Вопрос"}).text
            )
            stages = [data for name, data in events if name == "status"]
            retry = next(i for i, data in enumerate(stages) if data["phase"] == "retry")
            assert stages[retry]["node"] == "understand"
            assert stages[retry + 1]["node"] == "understand"
            assert stages[retry + 1]["phase"] == "stage"
            assert events[-1][0] == "done"
        finally:
            runtime.client.client.close()


def test_explicit_cancellation_is_durable_and_suppresses_late_success(chat_api):
    client, factory, _ = chat_api
    chat_id = _create_chat(client)
    runtime = _runtime(
        {"compose": {"answer_blocks": [Block(type="text", markdown="Поздний ответ")]}}
    )
    with factory() as db:
        stream = stream_chat_turn(db, chat_id, "Останови", runtime, owner_id="u_chat")
        accepted = next(stream)
        message_id = accepted.data.message_id
        cancelled = client.post(f"/api/chats/{chat_id}/messages/{message_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert list(stream) == []
    saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert len(saved) == 1 and saved[0]["turn"]["code"] == "cancelled"
    assert (
        client.post(f"/api/chats/{chat_id}/messages/{message_id}/cancel").json()["status"]
        == "cancelled"
    )


def test_followup_after_confirmed_cancel_waits_for_the_old_worker(chat_api):
    from uuid import uuid4

    client, _, use_runtime = chat_api
    chat_id = _create_chat(client)
    started, release = threading.Event(), threading.Event()
    calls = []

    class StoppingGraph:
        def stream(self, *_args, **_kwargs):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                started.set()
                assert release.wait(5)
            yield "updates", {"compose": {"answer_blocks": [Block(type="text", markdown="OK")]}}

    use_runtime(AgentRuntime(graph=StoppingGraph()))
    request_id = uuid4()
    admission = client.app.state.chat_admission
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.post,
            f"/api/chats/{chat_id}/messages",
            json={
                "text": "First",
                "request_id": str(request_id),
            },
        )
        try:
            assert started.wait(3)
            cancelled = client.post(f"/api/chats/{chat_id}/messages/m_{request_id.hex}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            followup = pool.submit(
                client.post, f"/api/chats/{chat_id}/messages", json={"text": "Next"}
            )
            until = time.monotonic() + 2
            while admission.snapshot()["queued_chats_total"] != 1 and time.monotonic() < until:
                time.sleep(0.01)
            assert admission.snapshot()["queued_chats_total"] == 1
            assert admission.snapshot()["active_chats_total"] == 1
            assert calls == [1]
            assert not followup.done()
            release.set()
            response = followup.result(5)
            assert response.status_code == 200, response.text
            assert parse_sse(response.text)[-1][0] == "done"
            assert calls == [1, 2]
        finally:
            release.set()
            first.result(5)
    history = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert [m["role"] for m in history] == ["user", "user", "assistant"]
    assert history[0]["turn"]["status"] == "cancelled"


def test_duplicate_request_does_not_run_graph_or_overwrite_success(chat_api):
    from uuid import uuid4

    client, _, use_runtime = chat_api
    chat_id = _create_chat(client)
    runtime = _runtime({"compose": {"answer_blocks": [Block(type="text", markdown="Готово")]}})
    use_runtime(runtime)
    request_id = uuid4()
    body = {"text": "Вопрос", "request_id": str(request_id)}
    assert parse_sse(client.post(f"/api/chats/{chat_id}/messages", json=body).text)[-1][0] == "done"
    assert client.post(f"/api/chats/{chat_id}/messages", json=body).status_code == 409
    assert len(runtime.graph.calls) == 1
    state = client.post(f"/api/chats/{chat_id}/messages/m_{request_id.hex}/cancel").json()
    assert state["status"] == "success"
    saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert saved[0]["turn"]["assistant_message_id"] == saved[1]["message_id"]


def test_unconfigured_runtime_keeps_a_failed_question(chat_api):
    client, _, use_runtime = chat_api
    chat_id = _create_chat(client)
    use_runtime(AgentRuntime(graph=None, unavailable=True))
    events = parse_sse(client.post(f"/api/chats/{chat_id}/messages", json={"text": "Вопрос"}).text)
    assert events[-1][1]["code"] == "model_unavailable"
    saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert len(saved) == 1 and saved[0]["turn"]["code"] == "model_unavailable"


def test_reasoning_configuration_failure_is_explained_and_persisted(chat_api):
    from djedai.agent.reasoning import ReasoningConfigurationError

    client, _, _ = chat_api
    chat_id = _create_chat(client)
    with patch(
        "djedai.backend.chat_service.get_agent_client",
        side_effect=ReasoningConfigurationError("unsupported", "private-key-and-dsn"),
    ):
        response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Вопрос"})
    assert response.status_code == 200
    terminal = parse_sse(response.text)[-1][1]
    assert terminal["code"] == "model_configuration"
    assert "MODEL_REASONING_SUPPORTED" in terminal["message"]
    assert "private-key-and-dsn" not in response.text
    saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert len(saved) == 1
    assert saved[0]["turn"]["code"] == "model_configuration"
    assert saved[0]["turn"]["message"] == terminal["message"]
    assert "private-key-and-dsn" not in str(saved)


def test_get_expires_orphaned_turn_but_preserves_legacy_messages(chat_api):
    client, factory, _ = chat_api
    chat_id = _create_chat(client)
    now = datetime.now(UTC)
    state = ChatTurnState(
        run_id="run_orphan",
        status="running",
        node="plan",
        message="Строю план анализа…",
        started_at=now - timedelta(seconds=60),
        deadline_at=now - timedelta(seconds=1),
    )
    with factory() as db:
        db.add_all(
            [
                MessageModel(
                    message_id="m_legacy", chat_id=chat_id, role="user", content="Старый вопрос"
                ),
                MessageModel(
                    message_id="m_orphan",
                    chat_id=chat_id,
                    role="user",
                    content="Новый вопрос",
                    turn=state.model_dump(mode="json"),
                ),
            ]
        )
        db.commit()
    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert messages[0]["turn"] is None
    assert messages[1]["turn"]["status"] == "timeout"
    assert messages[1]["turn"]["code"] == "turn_timeout"
    assert client.get(f"/api/chats/{chat_id}").json()["messages"] == messages


def test_cancel_requires_owned_chat_and_matching_question(chat_api, account_password_hash):
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    use_runtime(_runtime(error=RuntimeError("failure")))
    client.post(f"/api/chats/{chat_id}/messages", json={"text": "Вопрос"})
    question = client.get(f"/api/chats/{chat_id}").json()["messages"][0]
    other_chat = _create_chat(client)
    assert (
        client.post(f"/api/chats/{other_chat}/messages/{question['message_id']}/cancel").status_code
        == 404
    )
    with factory() as db:
        user = get_or_create_user(db, "u_other_cancel", "other_cancel")
        user.password_hash = account_password_hash
        db.commit()
        token = create_session(db, user.id)
    assert (
        client.post(
            f"/api/chats/{chat_id}/messages/{question['message_id']}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 404
    )
    assert client.get(f"/api/chats/{chat_id}").json()["messages"][0] == question


def test_stage_persistence_uses_turn_budget_during_short_sqlite_contention(chat_api):
    client, factory, _ = chat_api
    chat_id = _create_chat(client)
    runtime = _runtime({"compose": {"answer_blocks": [Block(type="text", markdown="Готово")]}})
    with factory() as db:
        stream = stream_chat_turn(db, chat_id, "Вопрос", runtime, owner_id="u_chat")
        assert next(stream).event == "status"
        blocker = sqlite3.connect(db.get_bind().url.database, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        release = threading.Timer(0.35, blocker.rollback)
        release.start()
        try:
            events = list(stream)
            assert events[-1].event == "done"
            assert not any(event.event == "error" for event in events)
        finally:
            release.join()
            blocker.close()
            stream.close()
    saved = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert saved[0]["turn"]["status"] == "success"


def test_final_plan_focus_does_not_include_superseded_attempts(chat_api) -> None:
    """Все попытки остаются в истории сообщения, но фокус берётся из графа."""
    client, factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    first = ToolResult(result_id="r_attempt", status="ok", tool="run_sql")
    final = ToolResult(result_id="r_final", status="ok", tool="run_sql")
    graph = FakeGraph(
        [
            {"execute": {"results": [first, final]}},
            {
                "compose": {
                    "answer_blocks": [Block(type="text", markdown="Итог")],
                    "focus_result_ids": ["r_final"],
                }
            },
        ]
    )
    use_runtime(AgentRuntime(graph=graph))

    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "Покажи иначе"})
    assert parse_sse(response.text)[-1][0] == "done"

    with factory() as db:
        chat = db.get(ChatModel, chat_id)
        assert chat.focus_result_ids == ["r_final"]
        saved = get_chat_messages(db, chat_id)
        assert saved[-1].result_ids == ["r_attempt", "r_final"]
