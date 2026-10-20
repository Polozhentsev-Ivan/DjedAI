"""Роутер чатов и сообщений агента."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import uuid4

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
    """StreamingResponse с гарантированным освобождением сессии БД даже при обрыве соединения."""

    def __init__(self, content: Any, *, session: Session, **kwargs: Any) -> None:
        self._db_session = session
        super().__init__(content, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._db_session.close()


@router.post(
    "/{chat_id}/messages",
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
    db: DbSession,
    session_factory: DbSessionFactory,
    runtime: AgentRuntimeDep,
) -> StreamingResponse:
    """Сохраняет вопрос и отдаёт ответ агента потоком SSE."""
    chat = get_chat_record(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Чат не найден")

    session = session_factory()
    is_disconnected = False

    def check_disconnected() -> bool:
        nonlocal is_disconnected
        return is_disconnected

    async def frames() -> AsyncIterator[str]:
        nonlocal is_disconnected
        try:
            for event in stream_chat_turn(
                session, chat_id, payload.text, runtime, is_cancelled=check_disconnected
            ):
                if await request.is_disconnected():
                    is_disconnected = True
                    break
                yield encode_sse_event(event)
        finally:
            session.close()

    return ClosableStreamingResponse(
        frames(),
        session=session,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
