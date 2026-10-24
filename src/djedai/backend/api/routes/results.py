"""Чтение сохранённого вычисления и его данных для локальной приёмки CP1."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from djedai.backend.auth.dependencies import current_owner_id
from djedai.backend.db.repositories import get_owned_tool_result, load_tool_result
from djedai.backend.db.session import get_db
from djedai.contracts.common import ErrorResponse
from djedai.contracts.tool_result import ToolResult, ToolResultData

router = APIRouter(prefix="/results", tags=["results"])
logger = logging.getLogger(__name__)
DbSession = Annotated[Session, Depends(get_db)]
LocalOwner = Annotated[str, Depends(current_owner_id)]


def _owned_result(db: Session, result_id: str, owner_id: str) -> None:
    if get_owned_tool_result(db, result_id, owner_id) is None:
        raise HTTPException(status_code=404, detail="Результат не найден")


@router.get(
    "/{result_id}",
    response_model=ToolResult,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Результат не найден или принадлежит другому пользователю",
        }
    },
)
def get_result(result_id: str, db: DbSession, owner_id: LocalOwner) -> ToolResult:
    """Возвращает происхождение расчёта без загрузки строк Parquet."""
    _owned_result(db, result_id, owner_id)
    result = load_tool_result(db, result_id, load_data=False, owner_id=owner_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Результат не найден")
    return result


@router.get(
    "/{result_id}/data",
    response_model=ToolResultData,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Данные результата не найдены, либо результат недоступен",
        }
    },
)
def get_result_data(
    result_id: str, request: Request, db: DbSession, owner_id: LocalOwner
) -> ToolResultData:
    """Читает сохранённые строки; отсутствие данных возвращает понятную ошибку."""
    _owned_result(db, result_id, owner_id)
    result = load_tool_result(
        db, result_id, storage_dir=request.app.state.settings.results_data_dir, owner_id=owner_id
    )
    if result is None or result.data is None:
        logger.warning("Данные результата '%s' не найдены (owner_id='%s')", result_id, owner_id)
        raise HTTPException(status_code=404, detail="Данные результата не найдены")
    return result.data
