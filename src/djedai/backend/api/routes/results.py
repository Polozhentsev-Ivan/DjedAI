"""Заготовка роутера реестра результатов (B5-5)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/results", tags=["results"])


@router.get("/{result_id}")
def get_result(result_id: str) -> dict[str, Any]:
    """Возвращает метаданные вызова инструмента (заглушка для B5-5)."""
    return {"result_id": result_id, "status": "empty"}
