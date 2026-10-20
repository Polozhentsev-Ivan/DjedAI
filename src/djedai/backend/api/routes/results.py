"""Чтение сохранённого вычисления и его данных для локальной приёмки CP1."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from djedai.backend.db.models import ToolResultModel
from djedai.backend.db.repositories import load_tool_result
from djedai.backend.db.session import get_db
from djedai.backend.local_user import local_owner_id
from djedai.contracts.tool_result import ToolResult, ToolResultData

router = APIRouter(prefix="/results", tags=["results"])
DbSession = Annotated[Session, Depends(get_db)]
LocalOwner = Annotated[str, Depends(local_owner_id)]


def _owned_result(db: Session, result_id: str, owner_id: str) -> None:
    record = db.get(ToolResultModel, result_id)
    if record is None or record.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Результат не найден")


@router.get("/{result_id}", response_model=ToolResult)
def get_result(result_id: str, db: DbSession, owner_id: LocalOwner) -> ToolResult:
    """Возвращает происхождение расчёта без загрузки строк Parquet."""
    _owned_result(db, result_id, owner_id)
    return load_tool_result(db, result_id, load_data=False)


@router.get("/{result_id}/data", response_model=ToolResultData)
def get_result_data(
    result_id: str, request: Request, db: DbSession, owner_id: LocalOwner
) -> ToolResultData:
    """Читает сохранённые строки; отсутствие данных возвращает понятную ошибку."""
    _owned_result(db, result_id, owner_id)
    result = load_tool_result(
        db, result_id, storage_dir=request.app.state.settings.results_data_dir
    )
    if result.data is None:
        raise HTTPException(status_code=404, detail="Данные результата не найдены")
    return result.data
