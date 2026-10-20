"""Роутер чатов и сообщений агента."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

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
from djedai.backend.db.session import get_db
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
def send_chat_message(
    chat_id: str,
    payload: SendMessageRequest,
    db: DbSession,
    runtime: AgentRuntimeDep,
) -> StreamingResponse:
    """Сохраняет вопрос и отдаёт ответ агента потоком SSE."""
    chat = get_chat_record(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Чат не найден")

    def frames() -> Iterator[str]:
        for event in stream_chat_turn(db, chat, payload.text, runtime):
            yield encode_sse_event(event)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
