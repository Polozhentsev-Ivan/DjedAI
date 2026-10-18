"""Заготовка роутера датасетов (B5-3)."""

from __future__ import annotations

from fastapi import APIRouter

from djedai.contracts.common import DatasetSummary

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetSummary])
def list_datasets() -> list[DatasetSummary]:
    """Возвращает список доступных датасетов текущего пользователя (заглушка для B5-3)."""
    return []
