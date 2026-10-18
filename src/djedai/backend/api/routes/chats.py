"""Заготовка роутера чатов и сообщений агента (B5-4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/chats", tags=["chats"])


@router.get("")
def list_chats(dataset_id: str | None = None) -> list[dict[str, Any]]:
    """Возвращает список чатов (заглушка для B5-4)."""
    return []
