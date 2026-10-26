"""Отпечатки и сравнение схемы источника для ревизии каталога (B1-11, #169).

Схема — это колонки и типы DuckDB каждой исходной таблицы в порядке колонок: для
PostgreSQL — как их видит подключённая БД, для CSV и ClickHouse — сырые таблицы в
файле датасета. Она записывается в реестр ``_djedai_source_schema`` при сборке, и
по ней считается ``schema_fingerprint``. Живой PostgreSQL сравнивается с этим
реестром при каждом открытии соединения. Отпечаток источника не содержит
пользователя, пароль и адрес в открытом виде.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence

import duckdb

from djedai.contracts.freshness import SchemaChange

SCHEMA_TABLE = "_djedai_source_schema"
Columns = tuple[tuple[str, str], ...]  # (колонка, тип DuckDB) в порядке колонок
Schema = Mapping[str, Columns]  # таблица источника → колонки


def table_columns(
    connection: duckdb.DuckDBPyConnection, database: str, schema: str, table: str
) -> Columns:
    rows = connection.execute(
        "SELECT column_name, data_type FROM duckdb_columns() "
        "WHERE database_name = ? AND schema_name = ? AND table_name = ? ORDER BY column_index",
        [database, schema, table],
    ).fetchall()
    return tuple((name, data_type) for name, data_type in rows)


def live_schema(
    connection: duckdb.DuckDBPyConnection, relations: Iterable[str], database: str
) -> dict[str, Columns]:
    """Колонки таблиц ``schema.table`` подключённой БД одним запросом; нет таблицы — нет ключа."""
    wanted = sorted(set(relations))
    if not wanted:
        return {}
    rows = connection.execute(
        "SELECT schema_name || '.' || table_name, column_name, data_type FROM duckdb_columns() "
        "WHERE database_name = ? AND list_contains(?, schema_name || '.' || table_name) "
        "ORDER BY 1, column_index",
        [database, wanted],
    ).fetchall()
    found: dict[str, list[tuple[str, str]]] = {}
    for relation, name, data_type in rows:
        found.setdefault(relation, []).append((name, data_type))
    return {relation: tuple(columns) for relation, columns in found.items()}


def record_source_schema(connection: duckdb.DuckDBPyConnection, schema: Schema) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (
            relation VARCHAR, position INTEGER, column_name VARCHAR, data_type VARCHAR,
            PRIMARY KEY (relation, position))"""
    )
    for relation, columns in schema.items():
        connection.execute(f"DELETE FROM {SCHEMA_TABLE} WHERE relation = ?", [relation])
        connection.executemany(
            f"INSERT INTO {SCHEMA_TABLE} VALUES (?, ?, ?, ?)",
            [[relation, index, name, data_type] for index, (name, data_type) in enumerate(columns)],
        )


def read_source_schema(connection: duckdb.DuckDBPyConnection) -> dict[str, Columns] | None:
    """Схема, на которой собран каталог; ``None`` — файл собран до #169."""
    exists = connection.execute(
        "SELECT 1 FROM duckdb_tables() WHERE database_name = current_database() "
        "AND schema_name = current_schema() AND table_name = ?",
        [SCHEMA_TABLE],
    ).fetchone()
    if exists is None:
        return None
    rows = connection.execute(
        f"SELECT relation, column_name, data_type FROM {SCHEMA_TABLE} ORDER BY relation, position"
    ).fetchall()
    schema: dict[str, list[tuple[str, str]]] = {}
    for relation, name, data_type in rows:
        schema.setdefault(relation, []).append((name, data_type))
    return {relation: tuple(columns) for relation, columns in schema.items()}


def schema_fingerprint(schema: Schema) -> str:
    """Не зависит от порядка таблиц; порядок колонок значим, как и для ``SELECT *``."""
    return _digest(
        sorted([relation, [list(c) for c in columns]] for relation, columns in schema.items())
    )


def source_fingerprint(kind: str, *identity: object) -> str:
    return _digest([kind, *identity])


def diff_schema(saved: Schema, live: Schema) -> list[SchemaChange]:
    """Изменения живой схемы относительно сохранённой; новые таблицы источника не важны."""
    changes: list[SchemaChange] = []
    for relation in sorted(saved):
        if relation not in live:
            changes.append(SchemaChange(kind="dropped_table", table=relation))
            continue
        before, after = dict(saved[relation]), dict(live[relation])
        for name, data_type in saved[relation]:
            if name not in after:
                changes.append(SchemaChange(kind="dropped_column", table=relation, column=name))
            elif after[name] != data_type:
                changes.append(
                    SchemaChange(
                        kind="type_changed",
                        table=relation,
                        column=name,
                        before=data_type,
                        after=after[name],
                    )
                )
        changes += [
            SchemaChange(kind="added_column", table=relation, column=name, after=data_type)
            for name, data_type in live[relation]
            if name not in before
        ]
    return changes


def _digest(value: Sequence[object]) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()
