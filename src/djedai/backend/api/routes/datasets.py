"""CSV upload, catalog and persistent profiling state for CP1."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse

from djedai.backend.dataset_service import DatasetService
from djedai.backend.local_user import local_owner_id
from djedai.backend.sse import encode_sse_event
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.common import DatasetDetail, DatasetSummary, DatasetUploadAccepted

router = APIRouter(prefix="/datasets", tags=["datasets"])


def get_dataset_service(request: Request) -> DatasetService:
    return request.app.state.dataset_service


DatasetServiceDep = Annotated[DatasetService, Depends(get_dataset_service)]
OwnerDep = Annotated[str, Depends(local_owner_id)]


@router.post("/upload", response_model=DatasetUploadAccepted, status_code=202)
async def upload_dataset(
    files: Annotated[list[UploadFile], File()], owner_id: OwnerDep, service: DatasetServiceDep
) -> DatasetUploadAccepted:
    """Persist repeated multipart ``files`` fields and schedule the CSV pipeline."""
    return await service.ingest_uploads(owner_id, files)


@router.get("", response_model=list[DatasetSummary])
def list_datasets(owner_id: OwnerDep, service: DatasetServiceDep) -> list[DatasetSummary]:
    return service.list_for_owner(owner_id)


@router.get("/{dataset_id}", response_model=DatasetDetail)
def get_dataset(dataset_id: str, owner_id: OwnerDep, service: DatasetServiceDep) -> DatasetDetail:
    return service.get_detail(owner_id, dataset_id)


@router.get("/{dataset_id}/catalog", response_model=DatasetCatalog)
def get_dataset_catalog(
    dataset_id: str, owner_id: OwnerDep, service: DatasetServiceDep
) -> DatasetCatalog:
    return service.get_catalog(owner_id, dataset_id)


@router.get(
    "/{dataset_id}/events",
    responses={
        200: {
            "content": {
                "text/event-stream": {"schema": {"$ref": "#/components/schemas/DatasetStreamEvent"}}
            },
            "description": "Поток прогресса профилирования с терминальным done либо error",
        }
    },
)
async def get_dataset_events(
    dataset_id: str, request: Request, owner_id: OwnerDep, service: DatasetServiceDep
) -> StreamingResponse:
    # Access is checked before sending HTTP headers, including terminal/replayed streams.
    await asyncio.to_thread(service.get_detail, owner_id, dataset_id)

    async def stream() -> AsyncIterator[str]:
        async for event in service.events(owner_id, dataset_id):
            if await request.is_disconnected():
                return
            yield encode_sse_event(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
