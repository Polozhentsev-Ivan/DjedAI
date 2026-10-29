"""Persistent owned CSV, read-only PostgreSQL and ClickHouse dataset orchestration."""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.db.models import DatasetModel
from djedai.backend.source_reads import (
    CatalogStaleError,
    SourceReader,
    check_live_freshness,
    snapshot_freshness,
)
from djedai.backend.source_secrets import SourceSecretError, SourceSecrets
from djedai.backend.upload_form import is_out_of_space
from djedai.config import LimitSettings, get_limits
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.common import (
    DatasetDetail,
    DatasetSummary,
    DatasetUploadAccepted,
    SourceCheckResult,
    SourceTable,
)
from djedai.contracts.freshness import DatasetRefresh, SourceFreshness
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
from djedai.data.postgres import ALIAS, PostgresError, attach_postgres, parse_dsn
from djedai.data.service import (
    CsvSource,
    check_clickhouse_source,
    check_postgres_source,
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
MAX_CHECKED_TABLES = 1000
# Source check answers with one of these; driver text never reaches the client.
_CHECK_ERRORS = (
    ("пароль", "Неверный пользователь или пароль"),
    ("не найдена", None),  # «база данных «x» не найдена» names only what the user typed
    ("не разрешает подключение", "Сервер не разрешает подключение этому пользователю"),
    (
        "только для чтения",
        "Учётная запись может изменять данные — подключите пользователя только для чтения",
    ),
    ("недоступен", "Сервер базы данных недоступен — проверьте адрес, порт и сеть"),
)


@dataclass(frozen=True)
class DatasetContext:
    catalog: DatasetCatalog
    duckdb_path: Path
    _owner_id: str = field(default="", repr=False)
    _source_secret: str | None = field(default=None, repr=False)
    _secrets: SourceSecrets | None = field(default=None, repr=False)
    max_profile_age: timedelta = timedelta(hours=24)
    # Последняя проверка живого источника сохраняется для карточки датасета.
    _record_freshness: Callable[[SourceFreshness], None] | None = field(
        default=None, repr=False, compare=False
    )

    @contextmanager
    def open_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self.open_reader() as reader:
            yield reader.connection

    @contextmanager
    def open_reader(self, *, strict: bool = True) -> Iterator[SourceReader]:
        """Соединение со свежестью источника; ``strict`` отклоняет несовместимый каталог."""
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
                try:
                    freshness = check_live_freshness(
                        connection, self.catalog, ALIAS, self.max_profile_age
                    )
                except duckdb.Error:
                    raise HTTPException(
                        status_code=503, detail="Источник PostgreSQL недоступен"
                    ) from None
                if self._record_freshness is not None:
                    self._record_freshness(freshness)
                if strict and freshness.state == "incompatible":
                    raise CatalogStaleError(freshness)
            else:
                freshness = snapshot_freshness(self.catalog)
            connection.execute("SET enable_external_access=false")
            yield SourceReader(connection, self.catalog, freshness, live=postgres)


class StorageQuotaManager:
    """Атомарный менеджер дисковой квоты владельца и in-flight резервирования."""

    def __init__(self, get_owner_disk_usage: Callable[[str], int], max_owner_bytes: int) -> None:
        self._get_owner_disk_usage = get_owner_disk_usage
        self.max_owner_bytes = max_owner_bytes
        self._lock = threading.Lock()
        self._reserved: dict[str, int] = defaultdict(int)

    def reserve(self, owner_id: str, bytes_to_reserve: int) -> None:
        with self._lock:
            current = self._get_owner_disk_usage(owner_id)
            total = current + self._reserved[owner_id] + bytes_to_reserve
            if total > self.max_owner_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Превышена дисковая квота владельца (максимум {self.max_owner_bytes} байт)"
                    ),
                )
            self._reserved[owner_id] += bytes_to_reserve

    def release(self, owner_id: str, bytes_to_release: int) -> None:
        with self._lock:
            self._reserved[owner_id] = max(0, self._reserved[owner_id] - bytes_to_release)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._reserved)


class DatasetJobQueue:
    """Ограниченная очередь и справедливый диспетчер обработки датасетов (§7.4 / #172)."""

    def __init__(
        self,
        process_func: Callable[[str, str], None],
        *,
        max_concurrent: int = 2,
        max_concurrent_per_owner: int = 1,
        max_queued: int = 10,
        max_queued_per_owner: int = 2,
    ) -> None:
        self.process_func = process_func
        self.max_concurrent = max_concurrent
        self.max_concurrent_per_owner = max_concurrent_per_owner
        self.max_queued = max_queued
        self.max_queued_per_owner = max_queued_per_owner

        self._active_jobs: set[tuple[str, str]] = set()
        self._active_by_owner: dict[str, int] = defaultdict(int)
        self._active_total: int = 0

        self._queued_by_owner: dict[str, deque[str]] = defaultdict(deque)
        self._queued_counts: dict[str, int] = defaultdict(int)
        self._queued_total: int = 0
        self._owner_order: deque[str] = deque()
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._reserved_by_owner: dict[str, int] = defaultdict(int)
        self._reserved_total: int = 0

    def reserve(self, owner_id: str) -> None:
        if (
            self._active_by_owner[owner_id]
            + self._queued_counts[owner_id]
            + self._reserved_by_owner[owner_id]
            >= self.max_concurrent_per_owner + self.max_queued_per_owner
        ):
            raise HTTPException(
                status_code=429,
                detail="Превышен лимит очереди обработки датасетов пользователя",
            )
        if (
            self._active_total + self._queued_total + self._reserved_total
            >= self.max_concurrent + self.max_queued
        ):
            raise HTTPException(
                status_code=429,
                detail="Очередь обработки датасетов сервера перегружена",
            )
        self._reserved_by_owner[owner_id] += 1
        self._reserved_total += 1

    def release_reservation(self, owner_id: str) -> None:
        self._reserved_by_owner[owner_id] = max(0, self._reserved_by_owner[owner_id] - 1)
        self._reserved_total = max(0, self._reserved_total - 1)

    def enqueue(self, owner_id: str, dataset_id: str) -> None:
        key = (owner_id, dataset_id)
        if key in self._active_jobs or any(dataset_id in q for q in self._queued_by_owner.values()):
            return

        if (
            self._active_total < self.max_concurrent
            and self._active_by_owner[owner_id] < self.max_concurrent_per_owner
        ):
            self._start_task(owner_id, dataset_id)
            return

        if self._queued_counts[owner_id] >= self.max_queued_per_owner:
            raise HTTPException(
                status_code=429,
                detail="Превышен лимит очереди обработки датасетов пользователя",
            )
        if self._queued_total >= self.max_queued:
            raise HTTPException(
                status_code=429,
                detail="Очередь обработки датасетов сервера перегружена",
            )

        self._queued_by_owner[owner_id].append(dataset_id)
        self._queued_counts[owner_id] += 1
        self._queued_total += 1
        if owner_id not in self._owner_order:
            self._owner_order.append(owner_id)

        self._dispatch()

    def enqueue_recovery(self, owner_id: str, dataset_id: str) -> bool:
        """Постановка в очередь при рестарте; если очередь полна, возвращает False."""
        try:
            self.enqueue(owner_id, dataset_id)
            return True
        except HTTPException:
            return False

    def contains(self, owner_id: str, dataset_id: str) -> bool:
        return (
            owner_id,
            dataset_id,
        ) in self._active_jobs or dataset_id in self._queued_by_owner.get(owner_id, ())

    def _dispatch(self) -> None:
        while self._active_total < self.max_concurrent and self._queued_total > 0:
            dispatched = False
            attempts = len(self._owner_order)
            for _ in range(attempts):
                if not self._owner_order:
                    break
                candidate_owner = self._owner_order[0]
                self._owner_order.rotate(-1)

                if self._active_by_owner[candidate_owner] < self.max_concurrent_per_owner:
                    queue = self._queued_by_owner.get(candidate_owner)
                    if queue:
                        dataset_id = queue.popleft()
                        self._queued_counts[candidate_owner] = max(
                            0, self._queued_counts[candidate_owner] - 1
                        )
                        self._queued_total = max(0, self._queued_total - 1)
                        if not queue and candidate_owner in self._owner_order:
                            self._owner_order.remove(candidate_owner)

                        self._start_task(candidate_owner, dataset_id)
                        dispatched = True
                        break
            if not dispatched:
                break

    def _start_task(self, owner_id: str, dataset_id: str) -> None:
        key = (owner_id, dataset_id)
        self._active_jobs.add(key)
        self._active_by_owner[owner_id] += 1
        self._active_total += 1

        task = asyncio.create_task(asyncio.to_thread(self.process_func, owner_id, dataset_id))
        self._tasks[key] = task

        def on_done(_t: asyncio.Task[None]) -> None:
            self._tasks.pop(key, None)
            self._active_jobs.discard(key)
            self._active_by_owner[owner_id] = max(0, self._active_by_owner[owner_id] - 1)
            self._active_total = max(0, self._active_total - 1)
            if (
                self._owner_order
                and self._owner_order[0] == owner_id
                and len(self._owner_order) > 1
            ):
                self._owner_order.rotate(-1)
            self._dispatch()

        task.add_done_callback(on_done)

    async def close(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_dataset_jobs_total": self._active_total,
            "active_dataset_jobs_by_owner": dict(self._active_by_owner),
            "queued_dataset_jobs_total": self._queued_total,
            "queued_dataset_jobs_by_owner": dict(self._queued_counts),
            "reserved_dataset_jobs_total": self._reserved_total,
            "reserved_dataset_jobs_by_owner": dict(self._reserved_by_owner),
        }


class DatasetService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        data_dir: str | Path,
        *,
        source_encryption_key: str | None = None,
        description_client: Callable[[], ModelClient] | None = None,
        description_context: Callable[[], tuple[ModelClient, bool]] | None = None,
        clickhouse_limits: ClickHouseLimits | None = None,
        catalog_max_age_hours: float = 24,
        limits: LimitSettings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.data_dir = Path(data_dir).resolve()
        self.limits = limits or get_limits()
        self._secrets = SourceSecrets(source_encryption_key)
        # None disables LLM descriptions; the factory is called once per build.
        self._description_client = description_client
        self._description_context = description_context
        self._clickhouse_limits = clickhouse_limits or ClickHouseLimits()
        self._catalog_max_age = timedelta(hours=catalog_max_age_hours)
        self.quota_manager = StorageQuotaManager(
            self.get_owner_disk_usage, self.limits.max_owner_storage_bytes
        )
        self._job_queue = DatasetJobQueue(
            self._process_dataset_job,
            max_concurrent=self.limits.max_concurrent_dataset_jobs,
            max_concurrent_per_owner=self.limits.max_concurrent_dataset_jobs_per_owner,
            max_queued=self.limits.max_queued_dataset_jobs,
            max_queued_per_owner=self.limits.max_queued_dataset_jobs_per_owner,
        )

    def get_owner_disk_usage(self, owner_id: str) -> int:
        """Подсчитывает суммарный размер файлов датасетов на диске для владельца."""
        total = 0
        with self.session_factory() as db:
            dataset_ids = list(
                db.scalars(
                    select(DatasetModel.dataset_id).where(DatasetModel.owner_id == owner_id)
                ).all()
            )
        for did in dataset_ids:
            dir_path = self.data_dir / did
            if dir_path.is_dir():
                for p in dir_path.rglob("*"):
                    if p.is_file():
                        try:
                            total += p.stat().st_size
                        except OSError:
                            pass
        return total

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
        relative = model.duckdb_path or ""
        if _revision_of(model.dataset_id, relative) is None:
            raise ValueError("Некорректный путь базы датасета")
        return self._dataset_path(model.dataset_id, relative)

    def _remove_old_revisions(self, dataset_id: str, published: int) -> None:
        """Keep the published file and the previous one for contexts taken before it."""
        directory = self._dataset_path(dataset_id, _revision_file(dataset_id, 1)).parent
        for path in directory.glob("dataset*.duckdb"):
            number = _revision_of(dataset_id, f"{dataset_id}/{path.name}")
            if number is not None and number < published - 1:
                try:
                    # An open read-only connection keeps reading the unlinked file.
                    path.unlink()
                except OSError:
                    logger.warning("Old revision of owned dataset %s was not removed", dataset_id)

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
            revision=catalog.revision if catalog else None,
            freshness=_freshness(model, catalog),
            refresh=DatasetRefresh.model_validate(model.refresh) if model.refresh else None,
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
                self._catalog_max_age,
                lambda freshness: self._record_freshness(owner_id, dataset_id, freshness),
            )

    def _record_freshness(self, owner_id: str, dataset_id: str, freshness: SourceFreshness) -> None:
        try:
            with self.session_factory() as db:
                model = self._owned(db, owner_id, dataset_id)
                model.freshness = freshness.model_dump(mode="json")
                db.commit()
        except Exception:
            # The check itself already decided the read; a lost status update is not fatal.
            logger.warning("Freshness of owned dataset %s was not stored", dataset_id)

    def check_freshness(self, owner_id: str, dataset_id: str) -> SourceFreshness:
        """Compare the source with the catalog now; an incompatible result is returned."""
        context = self.get_context(owner_id, dataset_id)
        with context.open_reader(strict=False) as reader:
            return reader.freshness

    async def ingest_uploads(
        self, owner_id: str, files: Sequence[UploadFile]
    ) -> DatasetUploadAccepted:
        self._job_queue.reserve(owner_id)
        dataset_id: str | None = None
        try:
            dataset_id = await asyncio.to_thread(self._persist_upload, owner_id, files)
            self._start(owner_id, dataset_id)
            return DatasetUploadAccepted(dataset_id=dataset_id)
        except BaseException:
            if dataset_id is not None:
                await asyncio.to_thread(self._rollback_dataset, owner_id, dataset_id)
            raise
        finally:
            self._job_queue.release_reservation(owner_id)

    async def ingest_csv(
        self, owner_id: str, sources: Sequence[CsvSource]
    ) -> DatasetUploadAccepted:
        """Snapshot Python file sources into owned storage and queue processing.

        All source bytes are retained before returning; callers may then close,
        change or remove their originals. This method requires no UploadFile or
        HTTP request and shares processing/recovery with the multipart adapter.
        """
        self._job_queue.reserve(owner_id)
        dataset_id: str | None = None
        try:
            dataset_id = await asyncio.to_thread(self._persist_csv_sources, owner_id, sources)
            self._start(owner_id, dataset_id)
            return DatasetUploadAccepted(dataset_id=dataset_id)
        except BaseException:
            if dataset_id is not None:
                await asyncio.to_thread(self._rollback_dataset, owner_id, dataset_id)
            raise
        finally:
            self._job_queue.release_reservation(owner_id)

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
        self._job_queue.reserve(owner_id)
        dataset_id: str | None = None
        try:
            dataset_id = await asyncio.to_thread(
                self._persist_db_source, owner_id, kind, dsn, tables, name
            )
            self._start(owner_id, dataset_id)
            return DatasetUploadAccepted(dataset_id=dataset_id)
        except BaseException:
            if dataset_id is not None:
                await asyncio.to_thread(self._rollback_dataset, owner_id, dataset_id)
            raise
        finally:
            self._job_queue.release_reservation(owner_id)

    async def check_db(self, dsn: str, *, kind: str = "postgresql") -> SourceCheckResult:
        """Probe a read-only source and list its tables without storing anything.

        Errors are reduced to a few safe categories, so the check cannot be used to read
        driver messages of arbitrary servers.
        """
        if kind not in _DB_SOURCES:
            raise HTTPException(status_code=501, detail="Этот источник пока не поддерживается")
        try:
            self._secrets.require_key()
        except SourceSecretError:
            raise HTTPException(
                status_code=503, detail="Шифрование источников не настроено"
            ) from None
        try:
            info = parse_clickhouse_dsn(dsn) if kind == "clickhouse" else parse_dsn(dsn)
        except (PostgresError, ClickHouseError, ValueError):
            raise HTTPException(
                status_code=422, detail=f"Проверьте строку подключения {_DB_SOURCES[kind]}"
            ) from None
        try:
            if kind == "clickhouse":
                check = await asyncio.to_thread(
                    check_clickhouse_source, info, self._clickhouse_limits
                )
            else:
                check = await asyncio.to_thread(check_postgres_source, info)
        except (PostgresError, ClickHouseError) as error:
            raise HTTPException(status_code=422, detail=_check_message(error.reason)) from None
        return SourceCheckResult(
            kind=_SOURCE_KINDS[kind],
            source=check.source,
            tables=[
                SourceTable(name=name, row_count=rows)
                for name, rows in check.tables[:MAX_CHECKED_TABLES]
            ],
            truncated=len(check.tables) > MAX_CHECKED_TABLES,
        )

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

    def _stream_copy_file(
        self,
        stream: Any,
        target: Path,
        owner_id: str,
        name: str,
        current_request_bytes: int,
    ) -> tuple[int, int]:
        file_bytes = 0
        reserved_bytes = 0
        stream.seek(0)
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    chunk_len = len(chunk)
                    file_bytes += chunk_len
                    if file_bytes > self.limits.max_upload_file_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Размер файла '{name}' превышает допустимый "
                                f"лимит ({self.limits.max_upload_file_bytes} байт)"
                            ),
                        )
                    if current_request_bytes + file_bytes > self.limits.max_upload_request_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Суммарный размер файлов запроса превышает допустимый "
                                f"лимит ({self.limits.max_upload_request_bytes} байт)"
                            ),
                        )
                    self.quota_manager.reserve(owner_id, chunk_len)
                    reserved_bytes += chunk_len
                    handle.write(chunk)
            return file_bytes, reserved_bytes
        except Exception as exc:
            if reserved_bytes > 0:
                self.quota_manager.release(owner_id, reserved_bytes)
            if is_out_of_space(exc):
                raise HTTPException(
                    status_code=507,
                    detail="Недостаточно места для загрузки в хранилище датасетов",
                ) from exc
            raise

    def _persist_upload(self, owner_id: str, files: Sequence[UploadFile]) -> str:
        def copy_source(index: int, target: Path, current_request_bytes: int) -> tuple[int, int]:
            name = files[index].filename or "upload.csv"
            return self._stream_copy_file(
                files[index].file, target, owner_id, name, current_request_bytes
            )

        return self._persist_sources(
            owner_id, [file.filename or "upload.csv" for file in files], copy_source
        )

    def _persist_csv_sources(self, owner_id: str, sources: Sequence[CsvSource]) -> str:
        def copy_source(index: int, target: Path, current_request_bytes: int) -> tuple[int, int]:
            name = sources[index].original_name
            with Path(sources[index].path).open("rb") as stream:
                return self._stream_copy_file(stream, target, owner_id, name, current_request_bytes)

        return self._persist_sources(
            owner_id, [source.original_name for source in sources], copy_source
        )

    def _persist_sources(
        self,
        owner_id: str,
        original_names: Sequence[str],
        copy_source: Callable[[int, Path, int], tuple[int, int]],
    ) -> str:
        if not original_names:
            raise HTTPException(status_code=422, detail="Загрузите хотя бы один CSV-файл")
        names = [name.replace("\\", "/").rsplit("/", 1)[-1] for name in original_names]
        if any(not name or not name.lower().endswith(".csv") for name in names):
            raise HTTPException(status_code=422, detail="Поддерживаются только CSV-файлы")
        dataset_id = str(uuid4())
        directory = self._dataset_path(dataset_id, f"{dataset_id}/dataset.duckdb").parent
        directory.mkdir(parents=True)
        total_reserved = 0
        try:
            raw_files = []
            total_request_bytes = 0
            for index, name in enumerate(names):
                relative = f"{dataset_id}/raw/{index:04d}.csv"
                target = self._dataset_path(dataset_id, relative)
                target.parent.mkdir(exist_ok=True)
                file_bytes, reserved = copy_source(index, target, total_request_bytes)
                total_request_bytes += file_bytes
                total_reserved += reserved
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
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            if total_reserved > 0:
                self.quota_manager.release(owner_id, total_reserved)
        return dataset_id

    def _rollback_dataset(self, owner_id: str, dataset_id: str) -> None:
        """Откатывает сохраненные файлы и запись датасета при ошибке постановки в очередь (#172)."""
        directory = self.data_dir / dataset_id
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        try:
            with self.session_factory() as db:
                model = db.scalar(
                    select(DatasetModel).where(
                        DatasetModel.dataset_id == dataset_id,
                        DatasetModel.owner_id == owner_id,
                    )
                )
                if model is not None:
                    db.delete(model)
                    db.commit()
        except Exception:
            logger.exception("Failed to rollback dataset record %s", dataset_id)

    def _start(self, owner_id: str, dataset_id: str) -> None:
        try:
            self._job_queue.enqueue(owner_id, dataset_id)
        except Exception:
            self._rollback_dataset(owner_id, dataset_id)
            raise

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

    def _process_dataset_job(self, owner_id: str, dataset_id: str) -> None:
        """Imports and refreshes share the same global and per-owner permits."""
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            refreshing = (
                model.status == "ready" and (model.refresh or {}).get("status") == "processing"
            )
        if refreshing:
            self._refresh(owner_id, dataset_id)
        else:
            self._process(owner_id, dataset_id)

    def _process(self, owner_id: str, dataset_id: str) -> None:
        try:
            with self.session_factory() as db:
                if self._owned(db, owner_id, dataset_id).status == "ready":
                    return
            catalog, relative = self._build(owner_id, dataset_id, revision=1)
            with self.session_factory() as db:
                model = db.scalar(
                    select(DatasetModel).where(
                        DatasetModel.dataset_id == dataset_id, DatasetModel.owner_id == owner_id
                    )
                )
                if model is not None:
                    model.catalog = catalog.model_dump(mode="json")
                    model.duckdb_path = relative
                    model.status = "ready"
                    model.error_message = None
                    model.progress = {"stage": "ready", "progress": 1.0, "message": "Готово"}
                    model.freshness = _built_freshness(catalog)
                    db.commit()
        except Exception as error:
            # Driver exceptions can contain source credentials; never log their traceback.
            logger.warning("Dataset processing failed for owned dataset %s", dataset_id)
            with self.session_factory() as db:
                model = db.scalar(
                    select(DatasetModel).where(
                        DatasetModel.dataset_id == dataset_id, DatasetModel.owner_id == owner_id
                    )
                )
                if model is not None:
                    model.status = "error"
                    model.error_message = _build_error(error)
                    model.catalog = None
                    db.commit()

    def _build(
        self, owner_id: str, dataset_id: str, *, revision: int
    ) -> tuple[DatasetCatalog, str]:
        """Build the file of one catalog revision from the retained source.

        Every revision has its own immutable file: DuckDB reuses an open database
        instance by path, so a file replaced in place would keep serving the previous
        data. The caller publishes the returned catalog together with the relative path.
        """
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            sources = []
            for index, item in enumerate(model.raw_files or []):
                if item["path"] != f"{dataset_id}/raw/{index:04d}.csv":
                    raise ValueError("Некорректный путь исходного CSV")
                sources.append(
                    CsvSource(self._dataset_path(dataset_id, item["path"]), item["name"])
                )
            relative = _revision_file(dataset_id, revision)
            target = self._dataset_path(dataset_id, relative)
            name, created_at, source_kind = model.name, model.created_at, model.source_kind
            secret = model.source_secret
            refreshing = model.status == "ready"
        working = target.with_suffix(".building.duckdb")
        self._dataset_path(dataset_id, working.relative_to(self.data_dir).as_posix())
        # An interrupted build is discarded; retained raw files are authoritative.
        working.unlink(missing_ok=True)
        Path(str(working) + ".wal").unlink(missing_ok=True)

        def progress(stage: str, fraction: float, message: str) -> None:
            # A ready dataset keeps its public progress; re-profiling is reported separately.
            if not refreshing:
                self._update_progress(owner_id, dataset_id, stage, fraction, message)

        common = {"dataset_id": dataset_id, "name": name, "created_at": created_at}
        if source_kind == "postgres":
            if not secret:
                raise SourceSecretError("Источник не настроен")
            source = self._secrets.decrypt(owner_id, dataset_id, secret)
            catalog = connect_postgres_dataset(
                source.dsn, source.tables, working, **common, revision=revision, progress=progress
            )
        elif source_kind == "clickhouse":
            if not secret:
                raise SourceSecretError("Источник не настроен")
            source = self._secrets.decrypt(owner_id, dataset_id, secret, kind="clickhouse")
            catalog = connect_clickhouse_dataset(
                source.dsn,
                source.tables,
                working,
                **common,
                limits=self._clickhouse_limits,
                revision=revision,
                progress=progress,
            )
        elif source_kind in {"csv", "csv_upload"}:
            catalog = ingest_csv_dataset(
                sources, working, **common, revision=revision, progress=progress
            )
        else:
            raise ValueError("Неподдерживаемый источник")
        if self._description_client is not None or self._description_context is not None:
            # Keep the current UI stage: a zero fraction leaves stored progress unchanged.
            progress(
                "relations" if source_kind in {"postgres", "clickhouse"} else "profile",
                0.0,
                "Описание данных",
            )
            catalog = self._describe(dataset_id, working, catalog)
        working.replace(target)
        return catalog, relative

    async def refresh(self, owner_id: str, dataset_id: str) -> DatasetDetail:
        """Re-profile a ready dataset into the next catalog revision in the background.

        The dataset stays ready with its current revision until the new one is
        published; a failure keeps it and is reported in ``DatasetDetail.refresh``.
        """
        self._job_queue.reserve(owner_id)
        try:
            detail = await asyncio.to_thread(self._accept_refresh, owner_id, dataset_id)
            self._start_refresh(owner_id, dataset_id)
            return detail
        finally:
            self._job_queue.release_reservation(owner_id)

    def _accept_refresh(self, owner_id: str, dataset_id: str) -> DatasetDetail:
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            if model.status != "ready" or not model.catalog:
                raise HTTPException(status_code=409, detail="Датасет ещё не готов к запросам")
            if (model.refresh or {}).get("status") == "processing" or self._job_queue.contains(
                owner_id, dataset_id
            ):
                raise HTTPException(status_code=409, detail="Перепрофилировка уже идёт")
            if model.source_kind in {"postgres", "clickhouse"}:
                try:
                    self._secrets.require_key()
                except SourceSecretError:
                    raise HTTPException(
                        status_code=503, detail="Шифрование источников не настроено"
                    ) from None
            model.refresh = DatasetRefresh(
                status="processing", started_at=datetime.now(UTC)
            ).model_dump(mode="json")
            db.commit()
            return self._detail(model)

    def _start_refresh(self, owner_id: str, dataset_id: str) -> None:
        try:
            self._job_queue.enqueue(owner_id, dataset_id)
        except Exception:
            self._reject_refresh(owner_id, dataset_id)
            raise

    def _reject_refresh(self, owner_id: str, dataset_id: str) -> None:
        """Keep the published revision usable when a refresh cannot be scheduled."""
        with self.session_factory() as db:
            model = self._owned(db, owner_id, dataset_id)
            model.refresh = DatasetRefresh(
                status="error",
                started_at=(model.refresh or {}).get("started_at") or datetime.now(UTC),
                message=(
                    "Очередь обработки датасетов переполнена. Повторите перепрофилировку позже."
                ),
            ).model_dump(mode="json")
            db.commit()

    def _refresh(self, owner_id: str, dataset_id: str) -> None:
        started_at = None
        try:
            with self.session_factory() as db:
                model = self._owned(db, owner_id, dataset_id)
                started_at = (model.refresh or {}).get("started_at")
                current = DatasetCatalog.model_validate(model.catalog).revision
            # A catalog from before #169 was built into the r1 file.
            number = (current.number if current else 1) + 1
            catalog, relative = self._build(owner_id, dataset_id, revision=number)
            with self.session_factory() as db:
                model = self._owned(db, owner_id, dataset_id)
                # The catalog and its file change together: a context is always a pair.
                model.catalog = catalog.model_dump(mode="json")
                model.duckdb_path = relative
                model.freshness = _built_freshness(catalog)
                model.refresh = None
                db.commit()
            self._remove_old_revisions(dataset_id, number)
        except Exception as error:
            logger.warning("Dataset re-profiling failed for owned dataset %s", dataset_id)
            with self.session_factory() as db:
                model = db.scalar(
                    select(DatasetModel).where(
                        DatasetModel.dataset_id == dataset_id, DatasetModel.owner_id == owner_id
                    )
                )
                if model is not None:
                    # The previous revision stays published and usable.
                    model.refresh = DatasetRefresh(
                        status="error",
                        started_at=started_at or datetime.now(UTC),
                        message=_build_error(error),
                    ).model_dump(mode="json")
                    db.commit()

    def _describe(self, dataset_id: str, database: Path, catalog: DatasetCatalog) -> DatasetCatalog:
        """Descriptions are optional: any failure publishes the statistical catalog."""
        try:
            if self._description_context is not None:
                client, share = self._description_context()
            else:
                client, share = self._description_client(), True
            return describe_dataset(database, catalog, client, share_values=share)
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
                        select(DatasetModel.owner_id, DatasetModel.dataset_id)
                        .where(
                            DatasetModel.source_kind.in_(
                                ["csv", "csv_upload", "postgres", "clickhouse"]
                            ),
                            DatasetModel.status.in_(["processing", "uploading", "profiling"]),
                        )
                        .order_by(DatasetModel.created_at.asc())
                    )
                )

        def refreshing() -> list[tuple[str, str]]:
            with self.session_factory() as db:
                rows = db.execute(
                    select(DatasetModel.owner_id, DatasetModel.dataset_id, DatasetModel.refresh)
                    .where(DatasetModel.status == "ready")
                    .where(DatasetModel.refresh.is_not(None))
                )
                return [
                    (owner, dataset)
                    for owner, dataset, state in rows
                    if (state or {}).get("status") == "processing"
                ]

        items = await asyncio.to_thread(pending)
        overflow_ids = []
        for owner_id, dataset_id in items:
            if not self._job_queue.enqueue_recovery(owner_id, dataset_id):
                overflow_ids.append(dataset_id)

        if overflow_ids:
            with self.session_factory() as db:
                for did in overflow_ids:
                    model = db.get(DatasetModel, did)
                    if model is not None:
                        model.status = "error"
                        model.error_message = (
                            "Сервер был перезагружен; очередь задач переполнена. "
                            "Запустите обработку повторно."
                        )
                db.commit()

        for owner_id, dataset_id in await asyncio.to_thread(refreshing):
            if not self._job_queue.enqueue_recovery(owner_id, dataset_id):
                await asyncio.to_thread(self._reject_refresh, owner_id, dataset_id)

    async def close(self) -> None:
        await self._job_queue.close()

    def snapshot(self) -> dict[str, Any]:
        result = self._job_queue.snapshot()
        result["quota_reserved_by_owner"] = self.quota_manager.snapshot()
        return result

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


def _revision_file(dataset_id: str, number: int) -> str:
    """The first build keeps the historical name; later revisions get their own file."""
    return (
        f"{dataset_id}/dataset.duckdb" if number == 1 else f"{dataset_id}/dataset.r{number}.duckdb"
    )


def _revision_of(dataset_id: str, relative: str) -> int | None:
    if relative == f"{dataset_id}/dataset.duckdb":
        return 1
    prefix, suffix = f"{dataset_id}/dataset.r", ".duckdb"
    if relative.startswith(prefix) and relative.endswith(suffix):
        number = relative[len(prefix) : -len(suffix)]
        if number.isdigit() and not number.startswith("0") and int(number) > 1:
            return int(number)
    return None


def _build_error(error: Exception) -> str:
    # Internal paths/SQL/stack traces must not leak through the JSON/SSE API.
    if isinstance(error, CsvReadError | SourceTooLargeError):
        return str(error)
    return "Не удалось обработать источник. Проверьте подключение или файлы."


def _built_freshness(catalog: DatasetCatalog) -> dict:
    """A just-built catalog matches its source by construction."""
    revision = catalog.revision
    return SourceFreshness(
        state="fresh", profiled_at=revision.profiled_at, checked_at=revision.profiled_at
    ).model_dump(mode="json")


def _freshness(model: DatasetModel, catalog: DatasetCatalog | None) -> SourceFreshness | None:
    """The last live check for PostgreSQL; a snapshot is judged by its catalog alone."""
    if catalog is None:
        return None
    if model.source_kind == "postgres":
        return SourceFreshness.model_validate(model.freshness) if model.freshness else None
    return snapshot_freshness(catalog)


def _check_message(reason: str) -> str:
    for marker, message in _CHECK_ERRORS:
        if marker in reason:
            return message or reason[:1].upper() + reason[1:]
    return "Не удалось подключиться к источнику"
