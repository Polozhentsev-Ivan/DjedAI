"""Тесты API чатов, SSE-событий агента и сохранения истории."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from djedai.backend.app import create_app
from djedai.backend.chat_service import AgentRuntime, get_agent_runtime
from djedai.backend.config import Settings, get_settings
from djedai.backend.db.models import ToolResultModel
from djedai.backend.db.repositories import (
    create_chat,
    create_dataset,
    get_chat_messages,
    get_or_create_user,
)
from djedai.backend.db.session import create_db_engine, get_db, get_session_factory, init_db
from djedai.backend.sse import encode_sse_event
from djedai.contracts.agent import Block
from djedai.contracts.message import MessageBlock
from djedai.contracts.sse import AgentStreamEvent, SSEBlockEvent
from djedai.contracts.tool_result import Fact, ToolError, ToolResult, ToolResultData

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "chat" / "blocks.json"


class FakeGraph:
    """Управляемый граф: отдаёт заранее заданные обновления узлов или падает."""

    def __init__(
        self,
        updates: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.updates = updates or []
        self.error = error
        self.calls: list[tuple[dict[str, Any], dict[str, Any], str]] = []

    def stream(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
        stream_mode: str,
    ) -> Iterator[dict[str, Any]]:
        self.calls.append((state, config, stream_mode))
        if self.error is not None:
            raise self.error
        yield from self.updates


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'app.db'}")
    init_db(engine)
    factory = sessionmaker(bind=engine)

    with factory() as db:
        user = get_or_create_user(db, "u_chat", "chat_owner")
        create_dataset(db, "ds_chat", user.id, "Чат-датасет")

    monkeypatch.setenv("RESULTS_DATA_DIR", str(tmp_path / "results"))
    get_settings.cache_clear()

    app = create_app(Settings(_env_file=None))

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
        with TestClient(app) as client:
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
    assert response.headers["cache-control"] == "no-cache"
    events = parse_sse(response.text)
    names = [name for name, _data in events]

    assert names[0] == "status"
    assert names[-1] == "done"
    assert "tool_call" in names
    assert "tool_result" in names
    assert "block" in names

    statuses = [data for name, data in events if name == "status"]
    assert [status["node"] for status in statuses] == [
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
    assert graph.calls[0][2] == "updates"
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


def test_send_message_streams_all_prepared_block_types(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    client, _factory, use_runtime = chat_api
    chat_id = _create_chat(client)
    fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
    prepared_blocks = _agent_blocks(fixture["blocks"])
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
    assert "model connection lost" in error["detail"]

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
    assert "Модель не ответила" in events[-1][1]["detail"]


def test_send_message_rejects_blank_text(
    chat_api: tuple[TestClient, Any, Any],
) -> None:
    client, _factory, _use_runtime = chat_api
    chat_id = _create_chat(client)

    response = client.post(f"/api/chats/{chat_id}/messages", json={"text": "   "})

    assert response.status_code == 422


def test_chat_ids_are_unique(chat_api: tuple[TestClient, Any, Any]) -> None:
    client, _factory, _use_runtime = chat_api
    ids = {_create_chat(client) for _ in range(5)}

    assert len(ids) == 5
    assert all(chat_id.startswith("c_") for chat_id in ids)


@pytest.mark.anyio
async def test_stream_proxy_unbuffered_and_events(
    chat_api: tuple[TestClient, sessionmaker[Session], Callable[[AgentRuntime], None]],
) -> None:
    """Проверяет unbuffered SSE-стриминг через /api proxy, заголовки и пошаговую отдачу событий."""
    test_client, _factory, use_runtime = chat_api
    app = test_client.app
    chat_id = _create_chat(test_client)

    tool_res = ToolResult(
        result_id="r_proxy_1",
        status="ok",
        tool="run_sql",
        params={"sql": "SELECT 42"},
        sql="SELECT 42",
        data=ToolResultData(columns=["val"], rows=[[42]], row_count=1),
    )
    graph = FakeGraph(
        [
            {"plan": {"plan": [], "retries": 1}},
            {"execute": {"results": [tool_res]}},
            {"compose": {"answer_blocks": [Block(type="text", markdown="Ответ получен")]}},
            {"verify": {}},
        ]
    )
    use_runtime(AgentRuntime(graph=graph))

    class ViteDevProxy:
        def __init__(self, target_app: ASGIApp) -> None:
            self.target_app = target_app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.target_app(scope, receive, send)

    proxy_app = ViteDevProxy(app)
    transport = httpx.ASGITransport(app=proxy_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:3000") as client:
        async with client.stream(
            "POST",
            f"/api/chats/{chat_id}/messages",
            json={"text": "Сколько всего заказов?"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers.get("x-accel-buffering") == "no"

            raw_chunks: list[str] = []
            async for line in response.aiter_lines():
                if line:
                    raw_chunks.append(line)

    events = [
        line.removeprefix("event:").strip() for line in raw_chunks if line.startswith("event:")
    ]
    assert events[0] == "status"
    assert "tool_call" in events
    assert "tool_result" in events
    assert "block" in events
    assert events[-1] == "done"


def test_stream_cleans_up_session_and_connections_on_complete_and_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверяет, что сессии и соединения закрываются после завершения и отмены (дефект [P2])."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'cleanup.db'}")
    init_db(engine)
    base_factory = sessionmaker(bind=engine)

    with base_factory() as db:
        user = get_or_create_user(db, "u_clean", "clean_owner")
        create_dataset(db, "ds_clean", user.id, "Датасет очистки")
        chat = create_chat(db, "c_clean", user.id, "ds_clean")

    monkeypatch.setenv("RESULTS_DATA_DIR", str(tmp_path / "results_clean"))
    get_settings.cache_clear()

    app = create_app(Settings(_env_file=None))

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
        with TestClient(app) as client:
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
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
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
    assert "database is locked" in data["detail"]


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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
