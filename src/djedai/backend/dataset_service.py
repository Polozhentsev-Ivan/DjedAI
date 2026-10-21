"""Persistent CSV dataset orchestration for the explicitly enabled CP1 runtime."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.db.models import DatasetModel
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.common import DatasetDetail, DatasetSummary, DatasetUploadAccepted
from djedai.contracts.sse import (
    DatasetDoneData,
    DatasetDoneEvent,
    DatasetErrorEvent,
    DatasetProgressData,
    DatasetProgressEvent,
    DatasetStreamEvent,
    SSEErrorData,
)
from djedai.contracts.tool_result import ToolError
from djedai.data.csv_ingest import CsvReadError
from djedai.data.service import CsvSource, ingest_csv_dataset

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetContext:
    catalog: DatasetCatalog
    duckdb_path: Path


class DatasetService:
    def __init__(self, session_factory: sessionmaker[Session], data_dir: str | Path) -> None:
        self.session_factory = session_factory
        self.data_dir = Path(data_dir).resolve()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def _path(self, relative: str) -> Path:
        path = (self.data_dir / relative).resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError("Путь источника выходит за каталог датасетов")
        return path

    @staticmethod
    def _owned(db: Session, owner_id: str, dataset_id: str) -> DatasetModel:
        model = db.scalar(
            select(DatasetModel).where(
                DatasetModel.dataset_id == dataset_id, DatasetModel.owner_id == owner_id
            )
        )
        if model is None:
            raise HTTPException(status_code=404, detail="Датасет не найден")
        return model

    @staticmethod
    def _detail(model: DatasetModel) -> DatasetDetail:
        catalog = DatasetCatalog.model_validate(model.catalog) if model.catalog else None
        return DatasetDetail(
            dataset_id=model.dataset_id,
            name=model.name,
            source={
                "kind": "csv_upload" if model.source_kind == "csv" else model.source_kind,
                "files": len(model.raw_files or []),
            },
            status="ready"
            if model.status == "ready"
            else "error"
            if model.status == "error"
            else "processing",
            table_count=len(catalog.tables) if catalog else None,
            row_count=sum(table.row_count for table in catalog.tables) if catalog else None,
            created_at=model.created_at,
            catalog=catalog,
            error=ToolError(code="dataset_processing_failed", message=model.error_message)
            if model.error_message
            else None,
        )

    def list_for_owner(self, owner_id: str) -> list[DatasetSummary]:
        with self.session_factory() as db:
            rows = db.scalars(
                select(DatasetModel)
                .where(DatasetModel.owner_id == owner_id)
                .order_by(DatasetModel.created_at.desc(), DatasetModel.dataset_id)
            )
            return [
                DatasetSummary.model_validate(
                    self._detail(row).model_dump(exclude={"catalog", "error"})
                )
                for row in rows
            ]

    def get_detail(self, owner_id: str, dataset_id: str) -> DatasetDetail:
        with self.session_factory() as db:
            return self._detail(self._owned(db, owner_id, dataset_id))

    def get_catalog(self, owner_id: str, dataset_id: str) -> DatasetCatalog:
        """Read the persisted catalog with the same owner/status rules as HTTP."""
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            if model.status != "ready" or not model.catalog:
                raise HTTPException(status_code=409, detail="Каталог датасета ещё не готов")
            return DatasetCatalog.model_validate(model.catalog)

    def get_context(self, owner_id: str, dataset_id: str) -> DatasetContext:
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            if model.status != "ready" or not model.catalog or not model.duckdb_path:
                raise HTTPException(status_code=409, detail="Датасет ещё не готов к запросам")
            path = self._path(model.duckdb_path)
            if not path.is_file():
                raise HTTPException(status_code=409, detail="Файл датасета недоступен")
            return DatasetContext(DatasetCatalog.model_validate(model.catalog), path)

    async def ingest_uploads(
        self, owner_id: str, files: Sequence[UploadFile]
    ) -> DatasetUploadAccepted:
        dataset_id = await asyncio.to_thread(self._persist_upload, owner_id, files)
        self._start(dataset_id)
        return DatasetUploadAccepted(dataset_id=dataset_id)

    async def ingest_csv(
        self, owner_id: str, sources: Sequence[CsvSource]
    ) -> DatasetUploadAccepted:
        """Snapshot Python file sources into owned storage and queue processing.

        All source bytes are retained before returning; callers may then close,
        change or remove their originals. This method requires no UploadFile or
        HTTP request and shares processing/recovery with the multipart adapter.
        """
        dataset_id = await asyncio.to_thread(self._persist_csv_sources, owner_id, sources)
        self._start(dataset_id)
        return DatasetUploadAccepted(dataset_id=dataset_id)

    def _persist_upload(self, owner_id: str, files: Sequence[UploadFile]) -> str:
        def copy_source(index: int, target: Path) -> None:
            stream = files[index].file
            stream.seek(0)
            with target.open("wb") as handle:
                shutil.copyfileobj(stream, handle, length=1024 * 1024)

        # FastAPI owns the multipart streams; copying never closes caller handles.
        return self._persist_sources(
            owner_id, [file.filename or "upload.csv" for file in files], copy_source
        )

    def _persist_csv_sources(self, owner_id: str, sources: Sequence[CsvSource]) -> str:
        def copy_source(index: int, target: Path) -> None:
            with Path(sources[index].path).open("rb") as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle, length=1024 * 1024)

        return self._persist_sources(
            owner_id, [source.original_name for source in sources], copy_source
        )

    def _persist_sources(
        self, owner_id: str, original_names: Sequence[str], copy_source: Callable[[int, Path], None]
    ) -> str:
        if not original_names:
            raise HTTPException(status_code=422, detail="Загрузите хотя бы один CSV-файл")
        names = [name.replace("\\", "/").rsplit("/", 1)[-1] for name in original_names]
        if any(not name or not name.lower().endswith(".csv") for name in names):
            raise HTTPException(status_code=422, detail="Поддерживаются только CSV-файлы")
        dataset_id = str(uuid4())
        directory = self._path(dataset_id)
        directory.mkdir(parents=True)
        try:
            raw_files = []
            for index, name in enumerate(names):
                relative = f"{dataset_id}/raw/{index:04d}.csv"
                target = self._path(relative)
                target.parent.mkdir(exist_ok=True)
                copy_source(index, target)
                raw_files.append({"path": relative, "name": name})
            with self.session_factory() as db:
                db.add(
                    DatasetModel(
                        dataset_id=dataset_id,
                        owner_id=owner_id,
                        name=names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1})",
                        source_kind="csv_upload",
                        status="processing",
                        raw_files=raw_files,
                        duckdb_path=f"{dataset_id}/dataset.duckdb",
                        progress={"stage": "queued", "progress": 0.0, "message": "В очереди"},
                    )
                )
                db.commit()
        except Exception:
            shutil.rmtree(directory)
            raise
        return dataset_id

    def _start(self, dataset_id: str) -> None:
        if dataset_id in self._tasks:
            return
        task = asyncio.create_task(asyncio.to_thread(self._process, dataset_id))
        self._tasks[dataset_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(dataset_id, None))

    def _update_progress(self, dataset_id: str, stage: str, fraction: float, message: str) -> None:
        with self.session_factory() as db:
            model = db.get(DatasetModel, dataset_id)
            if model is not None:
                # Recovery can repeat earlier stages; public completion never regresses.
                previous = (model.progress or {}).get("progress", 0.0)
                model.progress = DatasetProgressData(
                    stage=stage, progress=max(previous, fraction), message=message
                ).model_dump(mode="json")
                db.commit()

    def _process(self, dataset_id: str) -> None:
        try:
            with self.session_factory() as db:
                model = db.get(DatasetModel, dataset_id)
                if model is None or model.status == "ready":
                    return
                sources = [
                    CsvSource(self._path(item["path"]), item["name"])
                    for item in model.raw_files or []
                ]
                if not model.duckdb_path:
                    raise ValueError("Отсутствует путь базы CSV")
                target = self._path(model.duckdb_path)
                name, created_at = model.name, model.created_at
            working = target.with_suffix(".building.duckdb")
            # An interrupted build is discarded; retained raw files are authoritative.
            working.unlink(missing_ok=True)
            Path(str(working) + ".wal").unlink(missing_ok=True)
            catalog = ingest_csv_dataset(
                sources,
                working,
                dataset_id=dataset_id,
                name=name,
                created_at=created_at,
                progress=lambda stage, fraction, message: self._update_progress(
                    dataset_id, stage, fraction, message
                ),
            )
            working.replace(target)
            with self.session_factory() as db:
                model = db.get(DatasetModel, dataset_id)
                if model is not None:
                    model.catalog = catalog.model_dump(mode="json")
                    model.status = "ready"
                    model.error_message = None
                    model.progress = {"stage": "ready", "progress": 1.0, "message": "Готово"}
                    db.commit()
        except Exception as error:
            logger.exception("CSV processing failed for dataset %s", dataset_id)
            # Internal paths/SQL/stack traces must not leak through the JSON/SSE API.
            message = (
                str(error)
                if isinstance(error, CsvReadError)
                else ("Не удалось обработать CSV. Проверьте файлы и повторите загрузку.")
            )
            with self.session_factory() as db:
                model = db.get(DatasetModel, dataset_id)
                if model is not None:
                    model.status = "error"
                    model.error_message = message
                    model.catalog = None
                    db.commit()

    async def recover_interrupted(self) -> None:
        def pending() -> list[str]:
            with self.session_factory() as db:
                return list(
                    db.scalars(
                        select(DatasetModel.dataset_id).where(
                            DatasetModel.source_kind == "csv_upload",
                            DatasetModel.status.in_(["processing", "uploading", "profiling"]),
                        )
                    )
                )

        for dataset_id in await asyncio.to_thread(pending):
            self._start(dataset_id)

    async def close(self) -> None:
        # to_thread cannot be stopped safely; finish writes before disposing the engine.
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()))

    def _snapshot(self, owner_id: str, dataset_id: str) -> tuple[DatasetDetail, dict | None]:
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            return self._detail(model), model.progress

    async def events(self, owner_id: str, dataset_id: str) -> AsyncIterator[DatasetStreamEvent]:
        previous = None
        while True:
            detail, progress = await asyncio.to_thread(self._snapshot, owner_id, dataset_id)
            if detail.status == "ready":
                yield DatasetDoneEvent(data=DatasetDoneData(dataset_id=dataset_id))
                return
            if detail.status == "error":
                yield DatasetErrorEvent(
                    data=SSEErrorData(
                        message=detail.error.message if detail.error else "Ошибка обработки CSV"
                    )
                )
                return
            if progress and progress != previous:
                yield DatasetProgressEvent(data=DatasetProgressData.model_validate(progress))
                previous = progress
            await asyncio.sleep(0.2)
