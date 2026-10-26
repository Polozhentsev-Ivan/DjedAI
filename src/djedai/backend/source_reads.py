"""Свежесть источника при чтении и происхождение каждого чтения (B1-11, #169).

PostgreSQL читается вживую, поэтому при открытии соединения его схема сравнивается
со схемой, на которой собран каталог. Несовместимое изменение останавливает расчёт
(``CatalogStaleError``), совместимое и устаревший профиль становятся ограничением.
CSV и ClickHouse — неизменяемые снимки в файле датасета.

Ход чата читает PostgreSQL в одной транзакции DuckDB: расширение ``postgres`` держит в
ней транзакцию ``REPEATABLE READ``, и все запросы хода видят один снимок. Ошибка
выполнения прерывает транзакцию; следующее чтение открывает новый снимок, и его
``snapshot_id`` меняется. Правила — ``docs/decisions/169-catalog-revision.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import duckdb
from fastapi import HTTPException

from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.freshness import ReadProvenance, SchemaChange, SourceFreshness
from djedai.data.fingerprint import diff_schema, live_schema, read_source_schema

T = TypeVar("T")
MAX_LISTED_CHANGES = 5
UNKNOWN_CATALOG = (
    "Каталог собран до версионирования: ревизия и схема источника неизвестны. "
    "Перепрофилируйте датасет, чтобы их зафиксировать"
)


class CatalogStaleError(HTTPException):
    """Схема источника несовместима с каталогом; расчёт по нему не выполняется."""

    code = "catalog_stale"

    def __init__(self, freshness: SourceFreshness) -> None:
        super().__init__(status_code=409, detail=freshness.reason)
        self.freshness = freshness


def snapshot_freshness(catalog: DatasetCatalog) -> SourceFreshness:
    """Файл датасета не меняется после сборки: снимок свеж для своей ревизии."""
    if catalog.revision is None:
        return SourceFreshness(state="unknown", reason=UNKNOWN_CATALOG)
    return SourceFreshness(state="fresh", profiled_at=catalog.revision.profiled_at)


def check_live_freshness(
    connection: duckdb.DuckDBPyConnection,
    catalog: DatasetCatalog,
    database: str,
    max_age: timedelta,
    now: datetime | None = None,
) -> SourceFreshness:
    """Сравнить подключённый источник ``database`` со схемой из файла датасета."""
    now = now or datetime.now(UTC)
    saved = read_source_schema(connection)
    if catalog.revision is None or saved is None:
        return SourceFreshness(state="unknown", reason=UNKNOWN_CATALOG, checked_at=now)
    profiled_at = catalog.revision.profiled_at
    changes = diff_schema(saved, live_schema(connection, saved, database))
    if changes:
        compatible = all(change.compatible for change in changes)
        return SourceFreshness(
            state="drifted" if compatible else "incompatible",
            profiled_at=profiled_at,
            checked_at=now,
            changes=changes,
            reason=_drift_reason(changes, compatible),
        )
    if now - profiled_at > max_age:
        hours = int((now - profiled_at).total_seconds() // 3600)
        return SourceFreshness(
            state="aged",
            profiled_at=profiled_at,
            checked_at=now,
            reason=(
                f"Профиль каталога построен {hours} ч назад: число строк и диапазоны в "
                "каталоге могут не совпадать с текущими данными"
            ),
        )
    return SourceFreshness(state="fresh", profiled_at=profiled_at, checked_at=now)


def _drift_reason(changes: list[SchemaChange], compatible: bool) -> str:
    listed = "; ".join(change.describe() for change in changes[:MAX_LISTED_CHANGES])
    if len(changes) > MAX_LISTED_CHANGES:
        listed += f" и ещё {len(changes) - MAX_LISTED_CHANGES}"
    if compatible:
        return (
            f"Схема источника изменилась после профилирования ({listed}). Новые колонки не "
            "входят в каталог; перепрофилируйте датасет, чтобы их использовать"
        )
    return (
        f"Схема источника изменилась после профилирования ({listed}). Каталог не "
        "используется: перепрофилируйте датасет"
    )


@dataclass
class SourceReader:
    """Соединение датасета, его свежесть и учёт снимков для ``ReadProvenance``."""

    connection: duckdb.DuckDBPyConnection
    catalog: DatasetCatalog
    freshness: SourceFreshness
    live: bool
    _group: str | None = field(default=None, repr=False)
    _snapshot: int = field(default=0, repr=False)
    _in_transaction: bool = field(default=False, repr=False)

    def share_snapshot(self, group: str) -> None:
        """Все следующие чтения живого источника — в одном снимке ``<group>:<n>``."""
        if self.live:
            self._group = group

    def run(self, call: Callable[[], T]) -> tuple[T, ReadProvenance]:
        """Выполнить чтение и вернуть его происхождение; исключение пробрасывается."""
        self._ensure_snapshot()
        started = datetime.now(UTC)
        try:
            value = call()
        finally:
            finished = datetime.now(UTC)
        return value, self.provenance(started, finished)

    def provenance(self, started: datetime, finished: datetime) -> ReadProvenance:
        revision = self.catalog.revision
        if not self.live:
            consistency, snapshot = "immutable", f"r{revision.number if revision else 0}"
        elif self._group is not None:
            consistency, snapshot = "turn_snapshot", f"{self._group}:{self._snapshot}"
        else:
            consistency, snapshot = "independent", None
        return ReadProvenance(
            revision_known=revision is not None,
            dataset_id=self.catalog.dataset_id,
            catalog_revision=revision.number if revision else None,
            schema_fingerprint=revision.schema_fingerprint if revision else None,
            source_fingerprint=revision.source_fingerprint if revision else None,
            transform_version=revision.transform_version if revision else None,
            read_mode="live" if self.live else "snapshot",
            consistency=consistency,
            snapshot_id=snapshot,
            started_at=started,
            finished_at=finished,
            freshness=self.freshness,
        )

    def _ensure_snapshot(self) -> None:
        if self._group is None:
            return
        if self._in_transaction:
            try:
                self.connection.execute("SELECT 1")
                return
            except duckdb.TransactionException:
                # Ошибка выполнения или прерывание прервали снимок: открываем новый.
                self.connection.execute("ROLLBACK")
                self._in_transaction = False
        self.connection.execute("BEGIN TRANSACTION")
        self._in_transaction = True
        self._snapshot += 1
