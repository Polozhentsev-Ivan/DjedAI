"""Роутер чатов и сообщений агента."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Receive, Scope, Send

from djedai.backend.chat_service import (
    AgentRuntime,
    chat_summary,
    get_agent_runtime,
    message_to_chat_message,
    stream_chat_turn,
)
from djedai.backend.db.repositories import (
    create_chat as create_chat_record,
)
from djedai.backend.db.repositories import (
    get_chat as get_chat_record,
)
from djedai.backend.db.repositories import (
    get_chat_messages,
    get_dataset,
)
from djedai.backend.db.repositories import (
    list_chats as list_chat_records,
)
from djedai.backend.db.session import get_db, get_session_factory
from djedai.backend.sse import encode_sse_event
from djedai.contracts.chat import (
    ChatCreate,
    ChatCreated,
    ChatDetail,
    ChatSummary,
    SendMessageRequest,
)
from djedai.contracts.sse import SSEErrorData, SSEErrorEvent

router = APIRouter(prefix="/chats", tags=["chats"])

DbSession = Annotated[Session, Depends(get_db)]
DbSessionFactory = Annotated[sessionmaker[Session], Depends(get_session_factory)]
AgentRuntimeDep = Annotated[AgentRuntime, Depends(get_agent_runtime)]


@router.get("", response_model=list[ChatSummary])
def list_chats(dataset_id: str, db: DbSession) -> list[ChatSummary]:
    """Возвращает чаты датасета, сначала новые."""
    return [chat_summary(chat) for chat in list_chat_records(db, dataset_id=dataset_id)]


@router.post("", response_model=ChatCreated, status_code=201)
def create_chat(payload: ChatCreate, db: DbSession) -> ChatCreated:
    """Создаёт новый чат, привязанный к датасету и его владельцу."""
    dataset = get_dataset(db, payload.dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Датасет не найден")
    chat = create_chat_record(
        db,
        chat_id=f"c_{uuid4().hex[:12]}",
        owner_id=dataset.owner_id,
        dataset_id=dataset.dataset_id,
    )
    return ChatCreated(chat_id=chat.chat_id)


@router.get("/{chat_id}", response_model=ChatDetail)
def get_chat(chat_id: str, db: DbSession) -> ChatDetail:
    """Возвращает чат вместе с историей сообщений."""
    chat = get_chat_record(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Чат не найден")
    summary = chat_summary(chat)
    messages = [message_to_chat_message(item) for item in get_chat_messages(db, chat_id)]
    return ChatDetail(**summary.model_dump(), messages=messages)


class ClosableStreamingResponse(StreamingResponse):
    """StreamingResponse с освобождением сессии БД и отменой агента при обрыве связи."""

    def __init__(
        self,
        content: Any,
        *,
        cancel_event: threading.Event,
        worker_thread: threading.Thread | None = None,
        session_holder: list[Session] | None = None,
        **kwargs: Any,
    ) -> None:
        self._cancel_event = cancel_event
        self._worker_thread = worker_thread
        self._session_holder = session_holder
        super().__init__(content, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._cancel_event.set()
            if self._worker_thread is not None and self._worker_thread.is_alive():
                await anyio.to_thread.run_sync(self._worker_thread.join, 1.0)
            if self._session_holder and self._session_holder[0] is not None:
                try:
                    self._session_holder[0].close()
                except Exception:
                    pass


def _require_chat(chat_id: str, db: DbSession) -> None:
    """Проверяет существование чата в рабочем потоке FastAPI до открытия SSE."""
    if get_chat_record(db, chat_id) is None:
        raise HTTPException(status_code=404, detail="Чат не найден")


@router.post(
    "/{chat_id}/messages",
    dependencies=[Depends(_require_chat)],
    responses={
        200: {
            "content": {
                "text/event-stream": {
                    "schema": {"$ref": "#/components/schemas/AgentStreamEvent"},
                },
            },
            "description": (
                "Поток SSE-событий агента (status, tool_call, tool_result, block, done, error)"
            ),
        }
    },
)
async def send_chat_message(
    chat_id: str,
    payload: SendMessageRequest,
    request: Request,
    session_factory: DbSessionFactory,
    runtime: AgentRuntimeDep,
) -> StreamingResponse:
    """Сохраняет вопрос и отдаёт ответ агента потоком SSE вне event loop."""
    cancel_event = threading.Event()
    queue: asyncio.Queue[str | object] = asyncio.Queue()
    done_sentinel = object()
    loop = asyncio.get_running_loop()
    session_holder: list[Session] = []

    def worker() -> None:
        session = session_factory()
        session_holder.append(session)
        try:
            for event in stream_chat_turn(
                session, chat_id, payload.text, runtime, is_cancelled=cancel_event.is_set
            ):
                if cancel_event.is_set():
                    break
                frame = encode_sse_event(event)
                loop.call_soon_threadsafe(queue.put_nowait, frame)
        except Exception as exc:
            err_event = SSEErrorEvent(
                data=SSEErrorData(
                    message="Не удалось получить ответ агента",
                    detail=str(exc),
                )
            )
            loop.call_soon_threadsafe(queue.put_nowait, encode_sse_event(err_event))
        finally:
            try:
                session.close()
            except Exception:
                pass
            loop.call_soon_threadsafe(queue.put_nowait, done_sentinel)

    worker_thread = threading.Thread(target=worker, name=f"chat-agent-{chat_id}", daemon=True)
    worker_thread.start()

    async def frames() -> AsyncIterator[str]:
        try:
            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    break
                item = await queue.get()
                if item is done_sentinel:
                    break
                if isinstance(item, str):
                    yield item
        finally:
            cancel_event.set()

    return ClosableStreamingResponse(
        frames(),
        cancel_event=cancel_event,
        worker_thread=worker_thread,
        session_holder=session_holder,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
