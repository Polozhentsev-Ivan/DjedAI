"""Роутер чатов и сообщений агента (B5-4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response

from djedai.contracts.message import AssistantMessage

router = APIRouter(prefix="/chats", tags=["chats"])


@router.get("")
def list_chats(dataset_id: str | None = None) -> list[dict[str, Any]]:
    """Возвращает список чатов (заглушка для B5-4)."""
    return []


@router.post("", status_code=201)
def create_chat(payload: dict[str, Any] | None = None) -> dict[str, str]:
    """Создает новый чат (заглушка для B5-4)."""
    return {"chat_id": "c_stub"}


@router.get("/{chat_id}/messages", response_model=list[AssistantMessage])
def get_chat_messages(chat_id: str) -> list[AssistantMessage]:
    """Возвращает историю сообщений чата (заглушка для B5-4)."""
    return []


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
def send_chat_message(chat_id: str, payload: dict[str, Any] | None = None) -> Response:
    """Отправляет сообщение в чат и возвращает SSE-поток агента (заглушка для B5-4)."""
    return Response(content="", media_type="text/event-stream")
