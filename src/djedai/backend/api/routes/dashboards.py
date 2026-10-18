"""Заготовка роутера дашбордов (B5-6)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


@router.get("")
def list_dashboards(dataset_id: str | None = None) -> list[dict[str, Any]]:
    """Возвращает список дашбордов (заглушка для B5-6)."""
    return []
