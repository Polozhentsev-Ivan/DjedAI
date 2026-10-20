"""Роутер датасетов (B5-3)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from djedai.contracts.common import DatasetDetail, DatasetSummary

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetSummary])
def list_datasets() -> list[DatasetSummary]:
    """Возвращает список доступных датасетов текущего пользователя (заглушка для B5-3)."""
    return []


@router.get("/{dataset_id}", response_model=DatasetDetail)
def get_dataset(dataset_id: str) -> DatasetDetail:
    """Возвращает статус и каталог датасета по идентификатору (заглушка для B5-3)."""
    raise HTTPException(status_code=501, detail="API датасетов ещё не реализован")


@router.get(
    "/{dataset_id}/events",
    responses={
        200: {
            "content": {
                "text/event-stream": {"schema": {"$ref": "#/components/schemas/DatasetStreamEvent"}}
            },
            "description": "Поток SSE-событий прогресса профилирования датасета",
        }
    },
)
def get_dataset_events(dataset_id: str) -> Response:
    """Возвращает SSE-поток прогресса профилирования датасета (заглушка для B5-3)."""
    return Response(content="", media_type="text/event-stream")
