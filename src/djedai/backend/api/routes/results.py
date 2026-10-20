"""Роутер реестра результатов (B5-5)."""

from __future__ import annotations

from fastapi import APIRouter

from djedai.contracts.tool_result import ToolResult, ToolResultData

router = APIRouter(prefix="/results", tags=["results"])


@router.get("/{result_id}", response_model=ToolResult)
def get_result(result_id: str) -> ToolResult:
    """Возвращает метаданные вызова инструмента (заглушка для B5-5)."""
    return ToolResult(result_id=result_id, status="empty")


@router.get("/{result_id}/data", response_model=ToolResultData)
def get_result_data(result_id: str) -> ToolResultData:
    """Возвращает табличные данные результата для рендера (заглушка для B5-5)."""
    return ToolResultData(columns=[], rows=[])
