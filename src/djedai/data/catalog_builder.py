"""Сборка минимального каталога CSV и подключённых БД из результатов загрузчика и профайлера."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import duckdb

from djedai.contracts.catalog import Column, DatasetCatalog, Table
from djedai.data.csv_ingest import RawTable
from djedai.data.postgres import DbSource
from djedai.data.profiler import TableProfile
from djedai.data.typed_views import TypedView, read_currencies, read_journal


def build_csv_catalog(
    connection: duckdb.DuckDBPyConnection,
    raw: RawTable,
    view: TypedView,
    profile: TableProfile,
    *,
    dataset_id: str,
) -> DatasetCatalog:
    """Собирает каталог одной CSV-таблицы, сохраняя имена, валюты и журнал."""
    currencies = read_currencies(connection, raw.raw_table)
    journal = read_journal(connection, raw.raw_table)
    typed_columns = {column.name: column for column in view.columns}
    columns = []
    for column in profile.columns:
        typed = typed_columns[column.name]
        semantic = column.semantic_type
        role = _role(semantic)
        columns.append(
            Column.model_validate(
                {
                    "name": column.name,
                    "original_name": typed.original_name,
                    "physical_type": column.physical_type,
                    "semantic_type": semantic,
                    "role": role,
                    "null_ratio": column.null_ratio,
                    "distinct_count": column.distinct_count,
                    "is_unique": column.is_unique,
                    "sample_values": list(column.sample_values),
                    "top_values": [asdict(value) for value in column.top_values],
                    "stats": column.stats,
                    "parse": {"format": typed.format, "success_ratio": typed.success_ratio},
                    "currency": currencies.get((view.view_name, column.name)),
                }
            )
        )
    table = Table(
        table_id=view.view_name,
        raw_table=raw.raw_table,
        original_name=raw.original_name,
        row_count=profile.row_count,
        duplicate_rows=profile.duplicate_rows,
        primary_key_candidates=list(profile.primary_key_candidates),
        time_columns=list(profile.time_columns),
        columns=columns,
        source={
            "file": raw.original_name,
            "encoding": raw.encoding,
            "delimiter": raw.delimiter,
            "header": raw.header,
        },
    )
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=raw.original_name,
        created_at=datetime.now(UTC),
        source={"kind": "csv_upload"},
        tables=[table],
        transformations=[
            {
                "table": entry.table_name,
                "column": entry.column_name,
                "action": entry.action,
                "detail": entry.detail,
            }
            for entry in journal
        ],
    )


def build_db_catalog(
    connection: duckdb.DuckDBPyConnection,
    source: DbSource,
    view: TypedView,
    profile: TableProfile,
    *,
    dataset_id: str,
) -> DatasetCatalog:
    """Собирает каталог одной таблицы подключённой БД.

    Сырой таблицы нет (``raw_table`` — ``None``), исходная таблица — ``source.relation``.
    Типы заданы БД, поэтому ``parse`` не заполняется; журнал — переименования и приведения.
    """
    typed_columns = {column.name: column for column in view.columns}
    columns = [
        Column.model_validate(
            {
                "name": column.name,
                "original_name": typed_columns[column.name].original_name,
                "physical_type": column.physical_type,
                "semantic_type": column.semantic_type,
                "role": _role(column.semantic_type),
                "null_ratio": column.null_ratio,
                "distinct_count": column.distinct_count,
                "is_unique": column.is_unique,
                "sample_values": list(column.sample_values),
                "top_values": [asdict(value) for value in column.top_values],
                "stats": column.stats,
            }
        )
        for column in profile.columns
    ]
    table = Table(
        table_id=view.view_name,
        raw_table=None,
        original_name=source.relation,
        row_count=profile.row_count,
        duplicate_rows=profile.duplicate_rows,
        primary_key_candidates=list(profile.primary_key_candidates),
        time_columns=list(profile.time_columns),
        columns=columns,
        source={"relation": source.relation},
    )
    return DatasetCatalog(
        dataset_id=dataset_id,
        name=source.relation,
        created_at=datetime.now(UTC),
        source={"kind": source.source_kind},
        tables=[table],
        transformations=[
            {
                "table": entry.table_name,
                "column": entry.column_name,
                "action": entry.action,
                "detail": entry.detail,
            }
            for entry in read_journal(connection, source.raw_table)
        ],
    )


def _role(semantic: str) -> str:
    if semantic == "id":
        return "id"
    if semantic in ("integer", "float", "money", "percent"):
        return "measure"
    if semantic in ("date", "datetime"):
        return "time"
    if semantic in ("category", "bool"):
        return "dimension"
    return "text"
