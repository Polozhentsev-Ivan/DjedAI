"""Роутер датасетов (B5-3)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from djedai.contracts.catalog import DatasetCatalog, DatasetSource
from djedai.contracts.common import DatasetSummary

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetSummary])
def list_datasets() -> list[DatasetSummary]:
    """Возвращает список доступных датасетов текущего пользователя (заглушка для B5-3)."""
    return []


@router.get("/{dataset_id}", response_model=DatasetCatalog)
def get_dataset(dataset_id: str) -> DatasetCatalog:
    """Возвращает статус и каталог датасета по идентификатору (заглушка для B5-3)."""
    return DatasetCatalog(
        dataset_id=dataset_id,
        name="stub_dataset",
        source=DatasetSource(kind="csv"),
        tables=[],
    )


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
