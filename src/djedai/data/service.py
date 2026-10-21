"""Dataset pipelines shared by the HTTP adapter and local integrations.

The file database is published only after every table has a typed view and profile.
CSV uploads are copied into DuckDB; PostgreSQL tables stay in the source database and
are read live through read-only views. ClickHouse remains a separate stage.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from djedai.contracts.catalog import DatasetCatalog
from djedai.data.catalog_builder import build_csv_catalog, build_db_catalog
from djedai.data.csv_ingest import load_raw_csv
from djedai.data.postgres import (
    PgConnectionInfo,
    connect_postgres,
    detach_postgres,
    read_db_sources,
)
from djedai.data.profiler import profile_table
from djedai.data.relations import find_relations
from djedai.data.typed_views import create_typed_view


@dataclass(frozen=True)
class CsvSource:
    path: Path
    original_name: str


def ingest_csv_dataset(
    sources: Sequence[CsvSource],
    database_path: Path,
    *,
    dataset_id: str,
    name: str,
    created_at: datetime,
    progress: Callable[[str, float, str], None] | None = None,
) -> DatasetCatalog:
    """Build a complete multiple-table dataset in a caller-owned database file.

    Failures propagate to the caller; it must not publish the partial database.
    No connection is shared between workers or retained after this call.
    """
    if not sources:
        raise ValueError("Загрузите хотя бы один CSV-файл")
    tables = []
    transformations = []
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database_path)) as connection:
        for index, source in enumerate(sources):
            if progress:
                progress("read", index / len(sources), "Чтение CSV")
            raw = load_raw_csv(connection, source.path, source.original_name)
            if progress:
                progress("types", (index * 3 + 1) / (len(sources) * 3), "Определение типов")
            view = create_typed_view(connection, raw)
            if progress:
                progress("profile", (index * 3 + 2) / (len(sources) * 3), "Профилирование таблицы")
            profile = profile_table(connection, view)
            partial = build_csv_catalog(connection, raw, view, profile, dataset_id=dataset_id)
            tables.extend(partial.tables)
            transformations.extend(partial.transformations)
        connection.execute("CHECKPOINT")
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=name,
        created_at=created_at,
        source={"kind": "csv_upload"},
        tables=tables,
        transformations=transformations,
    )


def connect_postgres_dataset(
    dsn: str | PgConnectionInfo,
    tables: Sequence[str] | None,
    database_path: Path,
    *,
    dataset_id: str,
    name: str,
    created_at: datetime,
    progress: Callable[[str, float, str], None] | None = None,
) -> DatasetCatalog:
    """Build a PostgreSQL dataset in a caller-owned database file without copying data.

    The file keeps read-only views, registries and profiles; the connection and its
    password are not stored there, so every later session must call ``attach_postgres``
    with the same DSN before querying. Any failed table fails the whole build: the caller
    must not publish the partial database.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database_path)) as connection:
        if progress:
            progress("connect", 0.0, "Подключение к PostgreSQL")
        report = connect_postgres(connection, dsn, tables)
        try:
            if report.errors:
                raise report.errors[0]
            sources = {source.raw_table: source for source in read_db_sources(connection)}
            catalog_tables = []
            transformations = []
            for index, view in enumerate(report.views):
                if progress:
                    progress("profile", index / len(report.views), "Профилирование таблицы")
                profile = profile_table(connection, view)
                partial = build_db_catalog(
                    connection, sources[view.raw_table], view, profile, dataset_id=dataset_id
                )
                catalog_tables.extend(partial.tables)
                transformations.extend(partial.transformations)
            if progress:
                progress("relations", 1.0, "Поиск связей")
            relations = find_relations(connection)
        finally:
            detach_postgres(connection)
        connection.execute("CHECKPOINT")
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=name,
        created_at=created_at,
        source={"kind": "postgres"},
        tables=catalog_tables,
        relations=[relation.to_contract() for relation in relations.relations],
        warnings=list(relations.warnings),
        transformations=transformations,
    )
