"""Профиль таблиц и колонок по типизированным представлениям (B1-4).

Для каждой колонки считаются доля пропусков, число различных значений, признак
уникальности, top-10 значений с частотами и 5 примеров; для чисел — min/max/mean/
median/std, для дат — диапазон и гранулярность. Для таблицы — число строк, полные
дубли, временные колонки и кандидаты в первичный ключ.

Вся статистика считается в DuckDB, в Python приходят только агрегаты. Профиль
сохраняется в ``_djedai_table_profiles`` и ``_djedai_column_profiles``; имена полей
совпадают с ``DatasetCatalog`` (спецификация, §3.4).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal

import duckdb

from djedai.data.typed_views import _ID_NAME, TypedColumn, TypedView, _ident, read_typed_views

TABLE_PROFILES = "_djedai_table_profiles"
COLUMN_PROFILES = "_djedai_column_profiles"
TOP_VALUES = 10
SAMPLE_VALUES = 5

NUMERIC = ("integer", "float", "money", "percent")
TEMPORAL = ("date", "datetime")
NOT_KEYS = ("float", "money", "percent", "bool")  # уникальность таких колонок — не признак ключа


class ProfileError(Exception):
    """Профиль не удалось посчитать; ``str(error)`` можно показывать пользователю."""

    def __init__(self, table: str, reason: str) -> None:
        super().__init__(f"Таблица «{table}»: {reason}")
        self.table = table
        self.reason = reason


@dataclass(frozen=True)
class TopValue:
    value: object
    count: int


@dataclass(frozen=True)
class ColumnProfile:  # значения в top_values/sample_values/stats уже приведены к JSON-типам
    name: str
    physical_type: str
    semantic_type: str
    null_count: int
    null_ratio: float
    distinct_count: int
    is_unique: bool
    top_values: tuple[TopValue, ...]
    sample_values: tuple[object, ...]
    stats: dict


@dataclass(frozen=True)
class TableProfile:
    raw_table: str
    view_name: str
    row_count: int
    duplicate_rows: int
    primary_key_candidates: tuple[str, ...]
    time_columns: tuple[str, ...]
    columns: tuple[ColumnProfile, ...]


@dataclass(frozen=True)
class ProfileReport:  # итог пакетного профилирования: успешные + ошибки
    profiles: tuple[TableProfile, ...]
    errors: tuple[ProfileError, ...]


def profile_table(connection: duckdb.DuckDBPyConnection, view: TypedView) -> TableProfile:
    """Считает профиль представления и записывает его; повторный вызов заменяет запись."""
    try:
        _ensure_tables(connection)
        table = _ident(view.view_name)
        row_count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        duplicates = connection.execute(
            f"SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM "
            f"{_ident(view.raw_table)})) FROM {_ident(view.raw_table)}"
        ).fetchone()[0]
        columns = tuple(
            _profile_column(connection, table, column.name, column, row_count)
            for column in view.columns
        )
        profile = TableProfile(
            raw_table=view.raw_table,
            view_name=view.view_name,
            row_count=row_count,
            duplicate_rows=duplicates,
            primary_key_candidates=_key_candidates(columns, row_count),
            time_columns=tuple(c.name for c in columns if c.semantic_type in TEMPORAL),
            columns=columns,
        )
        connection.begin()
        try:
            _write_profile(connection, profile)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except duckdb.Error as error:
        raise ProfileError(view.view_name, f"не удалось посчитать профиль ({error})") from error
    return profile


def profile_tables(connection: duckdb.DuckDBPyConnection) -> ProfileReport:
    """Профилирует все построенные представления; ошибка одной таблицы не мешает другим."""
    profiles: list[TableProfile] = []
    errors: list[ProfileError] = []
    for view in read_typed_views(connection):
        try:
            profiles.append(profile_table(connection, view))
        except ProfileError as error:
            errors.append(error)
    return ProfileReport(profiles=tuple(profiles), errors=tuple(errors))


def read_profiles(
    connection: duckdb.DuckDBPyConnection, view_name: str | None = None
) -> list[TableProfile]:
    """Читает сохранённые профили (по имени raw-таблицы), при ``view_name`` — одной таблицы."""
    _ensure_tables(connection)
    where, params = ("WHERE view_name = ?", [view_name]) if view_name else ("", [])
    tables = connection.execute(
        f"SELECT raw_table, view_name, row_count, duplicate_rows, primary_key_candidates, "
        f"time_columns FROM {TABLE_PROFILES} {where} ORDER BY raw_table",
        params,
    ).fetchall()
    result = []
    for raw_table, view, row_count, duplicates, keys, times in tables:
        rows = connection.execute(
            f"SELECT profile FROM {COLUMN_PROFILES} WHERE raw_table = ? ORDER BY position",
            [raw_table],
        ).fetchall()
        result.append(
            TableProfile(
                raw_table,
                view,
                row_count,
                duplicates,
                tuple(json.loads(keys)),
                tuple(json.loads(times)),
                tuple(_column_from_json(json.loads(row[0])) for row in rows),
            )  # fmt: skip
        )
    return result


def _ensure_tables(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE_PROFILES} (
            raw_table VARCHAR PRIMARY KEY, view_name VARCHAR, row_count BIGINT,
            duplicate_rows BIGINT, primary_key_candidates VARCHAR, time_columns VARCHAR,
            profiled_at TIMESTAMP DEFAULT current_timestamp)"""
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {COLUMN_PROFILES} (
            raw_table VARCHAR, position INTEGER, name VARCHAR, profile VARCHAR,
            PRIMARY KEY (raw_table, position))"""
    )


def _write_profile(connection: duckdb.DuckDBPyConnection, profile: TableProfile) -> None:
    for table in (TABLE_PROFILES, COLUMN_PROFILES):
        connection.execute(f"DELETE FROM {table} WHERE raw_table = ?", [profile.raw_table])
    connection.execute(
        f"INSERT INTO {TABLE_PROFILES} (raw_table, view_name, row_count, duplicate_rows, "
        "primary_key_candidates, time_columns) VALUES (?, ?, ?, ?, ?, ?)",
        [
            profile.raw_table,
            profile.view_name,
            profile.row_count,
            profile.duplicate_rows,
            json.dumps(profile.primary_key_candidates, ensure_ascii=False),
            json.dumps(profile.time_columns, ensure_ascii=False),
        ],
    )
    for position, column in enumerate(profile.columns, start=1):
        connection.execute(
            f"INSERT INTO {COLUMN_PROFILES} VALUES (?, ?, ?, ?)",
            [
                profile.raw_table,
                position,
                column.name,
                json.dumps(asdict(column), ensure_ascii=False),
            ],
        )


def _column_from_json(data: dict) -> ColumnProfile:
    return ColumnProfile(
        **{
            **data,
            "top_values": tuple(TopValue(**item) for item in data["top_values"]),
            "sample_values": tuple(data["sample_values"]),
        }
    )


def _json_value(value: object) -> object:
    """Значение DuckDB → JSON-совместимое: DECIMAL → float, даты и время → ISO-строка."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _profile_column(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    name: str,
    column: TypedColumn,
    row_count: int,
) -> ColumnProfile:
    value = _ident(name)
    # пропуск — NULL, а в текстовых колонках ещё и строка из пробелов
    present = (
        f"{value} IS NOT NULL AND trim({value}) <> ''"
        if column.physical_type == "VARCHAR"
        else f"{value} IS NOT NULL"
    )
    checks = {
        "present": f"count(*) FILTER (WHERE {present})",
        "distinct": f"count(DISTINCT {value}) FILTER (WHERE {present})",
    }
    semantic = column.semantic_type
    if semantic in NUMERIC:
        number = f"CAST({value} AS DOUBLE)"
        checks |= {
            "min": f"min({value})",
            "max": f"max({value})",
            "mean": f"avg({number})",
            "median": f"median({number})",
            "std": f"stddev_samp({number})",
        }
    elif semantic in TEMPORAL:
        day = f"CAST({value} AS DATE)"
        checks |= {
            "min": f"min({value})",
            "max": f"max({value})",
            "year": f"bool_and({day} = date_trunc('year', {day}))",
            "quarter": f"bool_and({day} = date_trunc('quarter', {day}))",
            "month": f"bool_and({day} = date_trunc('month', {day}))",
            "weekdays": f"count(DISTINCT dayofweek({day}))",
            "days": f"count(DISTINCT {day})",
        }
        if semantic == "datetime":
            checks |= {
                "second": f"bool_or({value} <> date_trunc('minute', {value}))",
                "minute": f"bool_or({value} <> date_trunc('hour', {value}))",
                "hour": f"bool_or({value} <> date_trunc('day', {value}))",
            }
    select = ", ".join(checks.values())
    row = dict(
        zip(checks, connection.execute(f"SELECT {select} FROM {table}").fetchone(), strict=True)
    )

    top = connection.execute(
        f"SELECT {value}, count(*) AS n FROM {table} WHERE {present} "
        f"GROUP BY {value} ORDER BY n DESC, {value} LIMIT {TOP_VALUES}"
    ).fetchall()
    # порядок строк берём из скана представления; на больших таблицах он не гарантирован
    samples = connection.execute(
        f"SELECT {value} FROM (SELECT {value}, row_number() OVER () AS rn FROM {table}) "
        f"WHERE {present} GROUP BY {value} ORDER BY min(rn) LIMIT {SAMPLE_VALUES}"
    ).fetchall()

    present_count = row["present"]
    null_count = row_count - present_count
    return ColumnProfile(
        name=name,
        physical_type=column.physical_type,
        semantic_type=semantic,
        null_count=null_count,
        null_ratio=null_count / row_count if row_count else 0.0,
        distinct_count=row["distinct"],
        is_unique=present_count > 0 and row["distinct"] == present_count,
        top_values=tuple(TopValue(_json_value(v), n) for v, n in top),
        sample_values=tuple(_json_value(r[0]) for r in samples),
        stats=_stats(semantic, row),
    )


def _stats(semantic: str, row: dict) -> dict:
    if semantic in NUMERIC:
        return {key: _json_value(row[key]) for key in ("min", "max", "mean", "median", "std")}
    if semantic in TEMPORAL:
        return {
            "min": _json_value(row["min"]),
            "max": _json_value(row["max"]),
            "granularity": _granularity(row) if row["min"] is not None else None,
        }
    return {}


def _granularity(row: dict) -> str:
    """Самая крупная единица, которой кратны все значения колонки."""
    for unit in ("second", "minute", "hour"):
        if row.get(unit):
            return unit
    for unit in ("year", "quarter", "month"):
        if row[unit]:
            return unit
    if row["weekdays"] == 1 and row["days"] >= 2:
        return "week"
    return "day"


def _key_candidates(columns: tuple[ColumnProfile, ...], row_count: int) -> tuple[str, ...]:
    """Уникальные колонки без пропусков; ID-подобные (по типу или имени) идут первыми."""
    if not row_count:
        return ()
    found = [
        c for c in columns if c.null_count == 0 and c.is_unique and c.semantic_type not in NOT_KEYS
    ]
    found.sort(key=lambda c: not (c.semantic_type == "id" or _ID_NAME.search(c.name)))
    return tuple(c.name for c in found)
