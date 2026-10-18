"""Заготовка роутера авторизации и профиля (AUTH-2)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
def get_current_user_stub() -> dict[str, Any]:
    """Возвращает информацию о текущем пользователе (заглушка для AUTH-2)."""
    return {"user_id": "anon", "username": "anonymous"}
