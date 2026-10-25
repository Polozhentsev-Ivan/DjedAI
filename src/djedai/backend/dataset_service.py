"""Persistent owned CSV, read-only PostgreSQL and ClickHouse dataset orchestration."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import duckdb
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.db.models import DatasetModel
from djedai.backend.source_secrets import SourceSecretError, SourceSecrets
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
from djedai.data.clickhouse import ClickHouseError, ClickHouseLimits, SourceTooLargeError
from djedai.data.clickhouse import parse_dsn as parse_clickhouse_dsn
from djedai.data.csv_ingest import CsvReadError
from djedai.data.descriptions import ModelClient
from djedai.data.postgres import PostgresError, attach_postgres, parse_dsn
from djedai.data.service import (
    CsvSource,
    connect_clickhouse_dataset,
    connect_postgres_dataset,
    describe_dataset,
    ingest_csv_dataset,
)
from djedai.data.typed_views import _lit

logger = logging.getLogger(__name__)
# connect_db kinds: display name and stored source_kind
_DB_SOURCES = {"postgresql": "PostgreSQL", "clickhouse": "ClickHouse"}
_SOURCE_KINDS = {"postgresql": "postgres", "clickhouse": "clickhouse"}


@dataclass(frozen=True)
class DatasetContext:
    catalog: DatasetCatalog
    duckdb_path: Path
    _owner_id: str = field(default="", repr=False)
    _source_secret: str | None = field(default=None, repr=False)
    _secrets: SourceSecrets | None = field(default=None, repr=False)

    @contextmanager
    def open_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        # Recheck the persisted file at use time as well as at ownership lookup.
        if (
            not self.duckdb_path.is_file()
            or self.duckdb_path.is_symlink()
            or self.duckdb_path.parent.is_symlink()
            or self.duckdb_path.resolve() != self.duckdb_path
        ):
            raise HTTPException(status_code=409, detail="Файл датасета недоступен")
        postgres = self._source_secret is not None
        # Connections opened directly on one DuckDB file share its database instance,
        # including ATTACHes and configuration. A private memory instance keeps each
        # PostgreSQL attachment/secret independent while sharing only read-only storage.
        with duckdb.connect(
            ":memory:" if postgres else str(self.duckdb_path), read_only=not postgres
        ) as connection:
            if postgres:
                connection.execute(
                    f"ATTACH {_lit(str(self.duckdb_path))} AS owned_dataset (READ_ONLY)"
                )
                connection.execute("USE owned_dataset")
            if self._source_secret is not None:
                try:
                    if self._secrets is None:
                        raise SourceSecretError("Ключ источника недоступен")
                    source = self._secrets.decrypt(
                        self._owner_id, self.catalog.dataset_id, self._source_secret
                    )
                    attach_postgres(connection, source.dsn)
                except (SourceSecretError, PostgresError):
                    raise HTTPException(
                        status_code=503, detail="Источник PostgreSQL недоступен"
                    ) from None
            connection.execute("SET enable_external_access=false")
            yield connection


class DatasetService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        data_dir: str | Path,
        *,
        source_encryption_key: str | None = None,
        description_client: Callable[[], ModelClient] | None = None,
        clickhouse_limits: ClickHouseLimits | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.data_dir = Path(data_dir).resolve()
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._secrets = SourceSecrets(source_encryption_key)
        # None disables LLM descriptions; the factory is called once per build.
        self._description_client = description_client
        self._clickhouse_limits = clickhouse_limits or ClickHouseLimits()

    def _path(self, relative: str) -> Path:
        path = (self.data_dir / relative).resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError("Путь источника выходит за каталог датасетов")
        return path

    def _dataset_path(self, dataset_id: str, relative: str) -> Path:
        """Reject references to another dataset, even within the same data root."""
        if (
            not dataset_id
            or Path(dataset_id).name != dataset_id
            or dataset_id in {".", ".."}
            or "\\" in dataset_id
            or "/" in dataset_id
            or not relative.startswith(f"{dataset_id}/")
        ):
            raise ValueError("Некорректный путь датасета")
        unresolved = self.data_dir / relative
        directory = self.data_dir / dataset_id
        resolved = unresolved.resolve()
        if resolved != unresolved or not resolved.is_relative_to(directory):
            raise ValueError("Путь источника выходит за каталог датасета")
        for path in (unresolved, *unresolved.parents):
            if path == self.data_dir:
                break
            if path.is_symlink():
                raise ValueError("Символические ссылки источников не поддерживаются")
        return resolved

    def _database_path(self, model: DatasetModel) -> Path:
        relative = f"{model.dataset_id}/dataset.duckdb"
        if model.duckdb_path != relative:
            raise ValueError("Некорректный путь базы датасета")
        return self._dataset_path(model.dataset_id, relative)

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
                "files": len(model.raw_files or [])
                if model.source_kind in {"csv", "csv_upload"}
                else None,
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
            if model.catalog.get("dataset_id") != dataset_id:
                raise HTTPException(status_code=409, detail="Каталог датасета недоступен")
            return DatasetCatalog.model_validate(model.catalog)

    def get_context(self, owner_id: str, dataset_id: str) -> DatasetContext:
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            if model.status != "ready" or not model.catalog or not model.duckdb_path:
                raise HTTPException(status_code=409, detail="Датасет ещё не готов к запросам")
            try:
                path = self._database_path(model)
            except ValueError:
                raise HTTPException(status_code=409, detail="Файл датасета недоступен") from None
            if not path.is_file():
                raise HTTPException(status_code=409, detail="Файл датасета недоступен")
            if model.catalog.get("dataset_id") != dataset_id:
                raise HTTPException(status_code=409, detail="Каталог датасета недоступен")
            if model.source_kind == "postgres":
                try:
                    if not model.source_secret:
                        raise SourceSecretError("Источник не настроен")
                    self._secrets.decrypt(owner_id, dataset_id, model.source_secret)
                except SourceSecretError:
                    raise HTTPException(
                        status_code=503, detail="Источник PostgreSQL недоступен"
                    ) from None
            return DatasetContext(
                DatasetCatalog.model_validate(model.catalog),
                path,
                owner_id,
                model.source_secret if model.source_kind == "postgres" else None,
                self._secrets,
            )

    async def ingest_uploads(
        self, owner_id: str, files: Sequence[UploadFile]
    ) -> DatasetUploadAccepted:
        dataset_id = await asyncio.to_thread(self._persist_upload, owner_id, files)
        self._start(owner_id, dataset_id)
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
        self._start(owner_id, dataset_id)
        return DatasetUploadAccepted(dataset_id=dataset_id)

    async def connect_db(
        self,
        owner_id: str,
        dsn: str,
        tables: Sequence[str] | None = None,
        *,
        kind: str = "postgresql",
        name: str | None = None,
    ) -> DatasetUploadAccepted:
        """Persist an encrypted source before queuing its owned, restartable build."""
        if kind not in _DB_SOURCES:
            raise HTTPException(status_code=501, detail="Этот источник пока не поддерживается")
        try:
            self._secrets.require_key()
        except SourceSecretError:
            raise HTTPException(
                status_code=503, detail="Шифрование источников не настроено"
            ) from None
        try:
            if kind == "clickhouse":
                parse_clickhouse_dsn(dsn)
            else:
                parse_dsn(dsn)
        except (PostgresError, ClickHouseError, ValueError):
            raise HTTPException(
                status_code=422, detail=f"Проверьте строку подключения {_DB_SOURCES[kind]}"
            ) from None
        if tables is not None and (
            not tables or any(not isinstance(table, str) or not table.strip() for table in tables)
        ):
            raise HTTPException(status_code=422, detail="Выберите хотя бы одну таблицу")
        if name is not None and (not name.strip() or len(name) > 255):
            raise HTTPException(
                status_code=422, detail="Имя датасета должно содержать 1–255 символов"
            )
        dataset_id = await asyncio.to_thread(
            self._persist_db_source, owner_id, kind, dsn, tables, name
        )
        self._start(owner_id, dataset_id)
        return DatasetUploadAccepted(dataset_id=dataset_id)

    def _persist_db_source(
        self,
        owner_id: str,
        kind: str,
        dsn: str,
        tables: Sequence[str] | None,
        name: str | None,
    ) -> str:
        dataset_id = str(uuid4())
        encrypted = self._secrets.encrypt(owner_id, dataset_id, dsn, tables, kind=kind)
        directory = self._dataset_path(dataset_id, f"{dataset_id}/dataset.duckdb").parent
        directory.mkdir(parents=True)
        try:
            with self.session_factory() as db:
                db.add(
                    DatasetModel(
                        dataset_id=dataset_id,
                        owner_id=owner_id,
                        name=name.strip() if name is not None else _DB_SOURCES[kind],
                        source_kind=_SOURCE_KINDS[kind],
                        source_secret=encrypted,
                        status="processing",
                        duckdb_path=f"{dataset_id}/dataset.duckdb",
                        progress={"stage": "queued", "progress": 0.0, "message": "В очереди"},
                    )
                )
                db.commit()
        except Exception:
            shutil.rmtree(directory)
            raise
        return dataset_id

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
        directory = self._dataset_path(dataset_id, f"{dataset_id}/dataset.duckdb").parent
        directory.mkdir(parents=True)
        try:
            raw_files = []
            for index, name in enumerate(names):
                relative = f"{dataset_id}/raw/{index:04d}.csv"
                target = self._dataset_path(dataset_id, relative)
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

    def _start(self, owner_id: str, dataset_id: str) -> None:
        key = (owner_id, dataset_id)
        if key in self._tasks:
            return
        task = asyncio.create_task(asyncio.to_thread(self._process, owner_id, dataset_id))
        self._tasks[key] = task
        task.add_done_callback(lambda _: self._tasks.pop(key, None))

    def _update_progress(
        self, owner_id: str, dataset_id: str, stage: str, fraction: float, message: str
    ) -> None:
        with self.session_factory() as db:
            model = db.scalar(
                select(DatasetModel).where(
                    DatasetModel.dataset_id == dataset_id, DatasetModel.owner_id == owner_id
                )
            )
            if model is not None:
                # Recovery can repeat earlier stages; public completion never regresses.
                previous = (model.progress or {}).get("progress", 0.0)
                model.progress = DatasetProgressData(
                    stage=stage, progress=max(previous, fraction), message=message
                ).model_dump(mode="json")
                db.commit()

    def _process(self, owner_id: str, dataset_id: str) -> None:
        try:
            with self.session_factory() as db:
                model = self._owned(db, owner_id, dataset_id)
                if model.status == "ready":
                    return
                sources = []
                for index, item in enumerate(model.raw_files or []):
                    if item["path"] != f"{dataset_id}/raw/{index:04d}.csv":
                        raise ValueError("Некорректный путь исходного CSV")
                    sources.append(
                        CsvSource(self._dataset_path(dataset_id, item["path"]), item["name"])
                    )
                target = self._database_path(model)
                name, created_at, source_kind = model.name, model.created_at, model.source_kind
                secret = model.source_secret
            working = target.with_suffix(".building.duckdb")
            self._dataset_path(dataset_id, working.relative_to(self.data_dir).as_posix())
            # An interrupted build is discarded; retained raw files are authoritative.
            working.unlink(missing_ok=True)
            Path(str(working) + ".wal").unlink(missing_ok=True)

            def progress(stage: str, fraction: float, message: str) -> None:
                self._update_progress(owner_id, dataset_id, stage, fraction, message)

            if source_kind == "postgres":
                if not secret:
                    raise SourceSecretError("Источник не настроен")
                source = self._secrets.decrypt(owner_id, dataset_id, secret)
                catalog = connect_postgres_dataset(
                    source.dsn,
                    source.tables,
                    working,
                    dataset_id=dataset_id,
                    name=name,
                    created_at=created_at,
                    progress=progress,
                )
            elif source_kind == "clickhouse":
                if not secret:
                    raise SourceSecretError("Источник не настроен")
                source = self._secrets.decrypt(owner_id, dataset_id, secret, kind="clickhouse")
                catalog = connect_clickhouse_dataset(
                    source.dsn,
                    source.tables,
                    working,
                    dataset_id=dataset_id,
                    name=name,
                    created_at=created_at,
                    limits=self._clickhouse_limits,
                    progress=progress,
                )
            elif source_kind in {"csv", "csv_upload"}:
                catalog = ingest_csv_dataset(
                    sources,
                    working,
                    dataset_id=dataset_id,
                    name=name,
                    created_at=created_at,
                    progress=progress,
                )
            else:
                raise ValueError("Неподдерживаемый источник")
            if self._description_client is not None:
                # Keep the current UI stage: a zero fraction leaves stored progress unchanged.
                progress(
                    "relations" if source_kind in {"postgres", "clickhouse"} else "profile",
                    0.0,
                    "Описание данных",
                )
                catalog = self._describe(dataset_id, working, catalog)
            working.replace(target)
            with self.session_factory() as db:
                model = db.scalar(
                    select(DatasetModel).where(
                        DatasetModel.dataset_id == dataset_id, DatasetModel.owner_id == owner_id
                    )
                )
                if model is not None:
                    model.catalog = catalog.model_dump(mode="json")
                    model.status = "ready"
                    model.error_message = None
                    model.progress = {"stage": "ready", "progress": 1.0, "message": "Готово"}
                    db.commit()
        except Exception as error:
            # Driver exceptions can contain source credentials; never log their traceback.
            logger.warning("Dataset processing failed for owned dataset %s", dataset_id)
            # Internal paths/SQL/stack traces must not leak through the JSON/SSE API.
            message = (
                str(error)
                if isinstance(error, CsvReadError | SourceTooLargeError)
                else "Не удалось обработать источник. Проверьте подключение или файлы."
            )
            with self.session_factory() as db:
                model = db.scalar(
                    select(DatasetModel).where(
                        DatasetModel.dataset_id == dataset_id, DatasetModel.owner_id == owner_id
                    )
                )
                if model is not None:
                    model.status = "error"
                    model.error_message = message
                    model.catalog = None
                    db.commit()

    def _describe(self, dataset_id: str, database: Path, catalog: DatasetCatalog) -> DatasetCatalog:
        """Descriptions are optional: any failure publishes the statistical catalog."""
        try:
            return describe_dataset(database, catalog, self._description_client())
        except Exception:
            # Model client errors can echo endpoint or key details; log no traceback.
            logger.warning("Catalog descriptions failed for owned dataset %s", dataset_id)
            warning = (
                "Описания данных не сгенерированы: модель не настроена или недоступна; "
                "структура и статистика каталога доступны"
            )
            return catalog.model_copy(update={"warnings": [*catalog.warnings, warning]})

    async def recover_interrupted(self) -> None:
        def pending() -> list[tuple[str, str]]:
            with self.session_factory() as db:
                return list(
                    db.execute(
                        select(DatasetModel.owner_id, DatasetModel.dataset_id).where(
                            DatasetModel.source_kind.in_(
                                ["csv", "csv_upload", "postgres", "clickhouse"]
                            ),
                            DatasetModel.status.in_(["processing", "uploading", "profiling"]),
                        )
                    )
                )

        for owner_id, dataset_id in await asyncio.to_thread(pending):
            self._start(owner_id, dataset_id)

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
