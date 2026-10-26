"""Owned CSV/PostgreSQL/ClickHouse datasets, catalogs and persistent profiling streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse

from djedai.backend.auth.access import SessionAccess, get_session_access, guard_stream
from djedai.backend.auth.dependencies import current_owner_id
from djedai.backend.dataset_service import DatasetService
from djedai.backend.sse import encode_sse_event
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.common import (
    ConnectRequest,
    DatasetDetail,
    DatasetSummary,
    DatasetUploadAccepted,
    SourceCheckRequest,
    SourceCheckResult,
)
from djedai.contracts.freshness import SourceFreshness
from djedai.contracts.sse import DatasetErrorEvent, SSEErrorData

router = APIRouter(prefix="/datasets", tags=["datasets"])


def get_dataset_service(request: Request) -> DatasetService:
    return request.app.state.dataset_service


DatasetServiceDep = Annotated[DatasetService, Depends(get_dataset_service)]
OwnerDep = Annotated[str, Depends(current_owner_id)]


@router.post("/upload", response_model=DatasetUploadAccepted, status_code=202)
async def upload_dataset(
    files: Annotated[list[UploadFile], File()], owner_id: OwnerDep, service: DatasetServiceDep
) -> DatasetUploadAccepted:
    """Persist repeated multipart ``files`` fields and schedule the CSV pipeline."""
    try:
        return await service.ingest_uploads(owner_id, files)
    finally:
        for file in files:
            try:
                await file.close()
            except Exception:
                pass


@router.get("", response_model=list[DatasetSummary])
def list_datasets(owner_id: OwnerDep, service: DatasetServiceDep) -> list[DatasetSummary]:
    return service.list_for_owner(owner_id)


@router.post("/connect", response_model=DatasetUploadAccepted, status_code=202)
async def connect_dataset(
    payload: ConnectRequest, owner_id: OwnerDep, service: DatasetServiceDep
) -> DatasetUploadAccepted:
    return await service.connect_db(
        owner_id,
        payload.dsn.get_secret_value(),
        payload.tables,
        kind="postgresql" if payload.kind == "postgres" else payload.kind,
    )


@router.post("/connect/check", response_model=SourceCheckResult)
async def check_connection(
    payload: SourceCheckRequest, _owner_id: OwnerDep, service: DatasetServiceDep
) -> SourceCheckResult:
    """List tables of a read-only source before connecting; requires a session, stores nothing."""
    return await service.check_db(
        payload.dsn.get_secret_value(),
        kind="postgresql" if payload.kind == "postgres" else payload.kind,
    )


@router.get("/{dataset_id}", response_model=DatasetDetail)
def get_dataset(dataset_id: str, owner_id: OwnerDep, service: DatasetServiceDep) -> DatasetDetail:
    return service.get_detail(owner_id, dataset_id)


@router.get("/{dataset_id}/catalog", response_model=DatasetCatalog)
def get_dataset_catalog(
    dataset_id: str, owner_id: OwnerDep, service: DatasetServiceDep
) -> DatasetCatalog:
    return service.get_catalog(owner_id, dataset_id)


@router.post("/{dataset_id}/refresh", response_model=DatasetDetail, status_code=202)
async def refresh_dataset(
    dataset_id: str, owner_id: OwnerDep, service: DatasetServiceDep
) -> DatasetDetail:
    """Re-profile from the retained source; the current revision stays usable meanwhile."""
    return await service.refresh(owner_id, dataset_id)


@router.post("/{dataset_id}/freshness", response_model=SourceFreshness)
def check_dataset_freshness(
    dataset_id: str, owner_id: OwnerDep, service: DatasetServiceDep
) -> SourceFreshness:
    """Compare the source schema with the catalog now; nothing is computed or rebuilt."""
    return service.check_freshness(owner_id, dataset_id)


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
    dataset_id: str,
    request: Request,
    owner_id: OwnerDep,
    service: DatasetServiceDep,
    access: Annotated[SessionAccess, Depends(get_session_access)],
) -> StreamingResponse:
    # Access is checked before sending HTTP headers, including terminal/replayed streams.
    await asyncio.to_thread(service.get_detail, owner_id, dataset_id)
    await asyncio.to_thread(access.require)

    async def stream() -> AsyncIterator[str]:
        expired = DatasetErrorEvent(
            data=SSEErrorData(
                code="unauthorized",
                message="Сессия недействительна",
                detail="Войдите снова, чтобы продолжить",
            )
        )
        async for event in guard_stream(service.events(owner_id, dataset_id), access, expired):
            if await request.is_disconnected():
                return
            yield encode_sse_event(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
