"""Dataset pipelines shared by the HTTP adapter and local integrations.

The file database is published only after every table has a typed view and profile.
CSV uploads and ClickHouse tables are copied into DuckDB (ClickHouse up to a volume
limit); PostgreSQL tables stay in the source database and are read live through read-only
views. All find table relations; LLM descriptions are an optional step before publication.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from djedai.contracts.catalog import CatalogRevision, DatasetCatalog
from djedai.data.catalog_builder import build_csv_catalog, build_db_catalog
from djedai.data.clickhouse import (
    ClickHouseHttp,
    ClickHouseLimits,
    connect_clickhouse,
    open_session,
)
from djedai.data.clickhouse import list_tables as list_clickhouse_tables
from djedai.data.clickhouse import parse_dsn as parse_clickhouse_dsn
from djedai.data.csv_ingest import load_raw_csv, read_sources
from djedai.data.descriptions import ModelClient, describe_catalog
from djedai.data.fingerprint import (
    Schema,
    live_schema,
    record_source_schema,
    schema_fingerprint,
    source_fingerprint,
    table_columns,
)
from djedai.data.postgres import (
    ALIAS,
    PgConnectionInfo,
    attach_postgres,
    connect_postgres,
    detach_postgres,
    list_tables,
    parse_dsn,
    read_db_sources,
)
from djedai.data.profiler import profile_table
from djedai.data.relations import find_relations
from djedai.data.typed_views import TRANSFORM_VERSION, create_typed_view


@dataclass(frozen=True)
class CsvSource:
    path: Path
    original_name: str


@dataclass(frozen=True)
class SourceCheck:
    """A reachable source without its password and the tables it offers for connection."""

    source: str
    tables: tuple[tuple[str, int | None], ...]  # name accepted by connect, rows if known


def ingest_csv_dataset(
    sources: Sequence[CsvSource],
    database_path: Path,
    *,
    dataset_id: str,
    name: str,
    created_at: datetime,
    revision: int = 1,
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
        if progress:
            # The UI knows read/types/profile; relations finish the profile stage.
            progress("profile", (len(sources) * 3 - 1) / (len(sources) * 3), "Поиск связей")
        relations = find_relations(connection)
        raw = read_sources(connection)
        database, schema = _file_location(connection)
        built = _revision(
            connection,
            {
                table.raw_table: table_columns(connection, database, schema, table.raw_table)
                for table in raw
            },
            revision,
            source_fingerprint("csv_upload", [[table.sha256, table.size_bytes] for table in raw]),
        )
        connection.execute("CHECKPOINT")
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=name,
        created_at=created_at,
        source={"kind": "csv_upload"},
        revision=built,
        tables=tables,
        relations=[relation.to_contract() for relation in relations.relations],
        warnings=list(relations.warnings),
        transformations=transformations,
    )


def describe_dataset(
    database_path: Path, catalog: DatasetCatalog, client: ModelClient
) -> DatasetCatalog:
    """Add LLM descriptions to a built, unpublished dataset; model failures become warnings.

    Only the description cache in the dataset file is touched, so a PostgreSQL dataset
    needs no live source here. The statistical catalog stays valid without the model.
    """
    with duckdb.connect(str(database_path)) as connection:
        report = describe_catalog(connection, catalog, client)
        connection.execute("CHECKPOINT")
    return report.catalog


def connect_postgres_dataset(
    dsn: str | PgConnectionInfo,
    tables: Sequence[str] | None,
    database_path: Path,
    *,
    dataset_id: str,
    name: str,
    created_at: datetime,
    revision: int = 1,
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
            info = parse_dsn(dsn) if isinstance(dsn, str) else dsn
            selected = sorted(source.relation for source in sources.values())
            built = _revision(
                connection,
                live_schema(connection, selected, ALIAS),
                revision,
                source_fingerprint("postgres", info.host, info.port, info.dbname, selected),
            )
        finally:
            detach_postgres(connection)
        connection.execute("CHECKPOINT")
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=name,
        created_at=created_at,
        source={"kind": "postgres"},
        revision=built,
        tables=catalog_tables,
        relations=[relation.to_contract() for relation in relations.relations],
        warnings=list(relations.warnings),
        transformations=transformations,
    )


def connect_clickhouse_dataset(
    dsn: str | ClickHouseHttp,
    tables: Sequence[str] | None,
    database_path: Path,
    *,
    dataset_id: str,
    name: str,
    created_at: datetime,
    limits: ClickHouseLimits | None = None,
    revision: int = 1,
    progress: Callable[[str, float, str], None] | None = None,
) -> DatasetCatalog:
    """Copy selected ClickHouse tables into a caller-owned database file and profile them.

    The volume is checked before any rows are read; a larger source fails the build. The
    file keeps a snapshot, not the connection, so later sessions need no source access.
    Any failed table fails the whole build: the caller must not publish the partial database.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database_path)) as connection:
        if progress:
            progress("connect", 0.0, "Подключение к ClickHouse")
        report = connect_clickhouse(connection, dsn, tables, database_path.parent, limits)
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
        client = parse_clickhouse_dsn(dsn) if isinstance(dsn, str) else dsn
        database, schema = _file_location(connection)
        built = _revision(
            connection,
            {
                source.relation: table_columns(connection, database, schema, source.raw_table)
                for source in sources.values()
            },
            revision,
            source_fingerprint(
                "clickhouse",
                client.host,
                client.port,
                client.database,
                sorted(source.relation for source in sources.values()),
            ),
        )
        connection.execute("CHECKPOINT")
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=name,
        created_at=created_at,
        source={"kind": "clickhouse"},
        revision=built,
        tables=catalog_tables,
        relations=[relation.to_contract() for relation in relations.relations],
        warnings=list(relations.warnings),
        transformations=transformations,
    )


def _file_location(connection: duckdb.DuckDBPyConnection) -> tuple[str, str]:
    database, schema = connection.execute("SELECT current_database(), current_schema()").fetchone()
    return database, schema


def _revision(
    connection: duckdb.DuckDBPyConnection, schema: Schema, number: int, source: str
) -> CatalogRevision:
    """Record the source schema in the dataset file and stamp the finished profile."""
    record_source_schema(connection, schema)
    return CatalogRevision(
        number=number,
        profiled_at=datetime.now(UTC),
        schema_fingerprint=schema_fingerprint(schema),
        source_fingerprint=source,
        transform_version=TRANSFORM_VERSION,
    )


def check_postgres_source(dsn: str | PgConnectionInfo) -> SourceCheck:
    """Attach read-only in a private memory database, list tables and detach; nothing is kept.

    Tables in ``public`` are named without the schema, as ``connect_postgres`` accepts them.
    """
    with duckdb.connect() as connection:
        info = attach_postgres(connection, dsn)
        try:
            tables = list_tables(connection)
        finally:
            detach_postgres(connection)
    return SourceCheck(
        info.describe(),
        tuple(
            (table if schema == "public" else f"{schema}.{table}", None) for schema, table in tables
        ),
    )


def check_clickhouse_source(
    dsn: str | ClickHouseHttp, limits: ClickHouseLimits | None = None
) -> SourceCheck:
    """Open a read-only ClickHouse session and list its tables with server row counts."""
    session = open_session(dsn, limits)
    return SourceCheck(
        session.client.describe(),
        tuple(
            (table.name, table.rows if table.rows >= 0 else None)
            for table in list_clickhouse_tables(session)
        ),
    )
