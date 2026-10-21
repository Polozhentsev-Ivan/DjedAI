"""Профиль таблиц и колонок по типизированным представлениям (B1-4).

Для каждой колонки считаются доля пропусков, число нераспознанных значений, число
различных значений, признак уникальности, top-10 значений с частотами и 5 примеров;
для чисел — min/max/mean/median/std, для дат — диапазон и гранулярность. Для таблицы —
число строк, полные дубли, временные колонки и кандидаты в первичный ключ.

Пропуск — пустое значение исходных данных (NULL или строка из пробелов). Значение,
которое не удалось привести к типу колонки, в представлении тоже NULL, но пропуском не
считается: оно учитывается отдельно в ``unparsed_count``. Вместе они дают все NULL
представления. Число различных значений и уникальность считаются по распознанным.

Вся статистика считается в DuckDB, в Python приходят только агрегаты. Профиль
сохраняется в ``_djedai_table_profiles`` и ``_djedai_column_profiles``; имена полей
совпадают с ``DatasetCatalog`` (спецификация, §3.4).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext

import duckdb

from djedai.data.typed_views import _ID_NAME, TypedColumn, TypedView, _ident, read_typed_views

TABLE_PROFILES = "_djedai_table_profiles"
COLUMN_PROFILES = "_djedai_column_profiles"
TOP_VALUES = 10
SAMPLE_VALUES = 5
EXACT_DIGITS = 60  # точность Decimal для среднего и медианы: сумма HUGEINT — до 39 цифр
_DECIMAL = re.compile(r"DECIMAL\(\d+,\s*(\d+)\)")

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
    null_count: int  # пустые значения исходных данных (NULL и пробелы), без нераспознанных
    null_ratio: float
    distinct_count: int  # по распознанным значениям: нераспознанные в представлении NULL
    is_unique: bool  # то же: нераспознанные значения не учитываются
    top_values: tuple[TopValue, ...]
    sample_values: tuple[object, ...]
    stats: dict
    unparsed_count: int = 0  # непустые значения, не приведённые к типу (в представлении NULL)


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
        # Вся статистика — в одной транзакции: у подключённой БД (ATTACH postgres) это один
        # снимок REPEATABLE READ, поэтому число строк и счётчики колонок не расходятся,
        # даже если исходную таблицу меняют во время профилирования.
        connection.begin()
        try:
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
    """Значение DuckDB → JSON-совместимое без потери точности.

    DECIMAL (деньги, проценты) → точная десятичная строка: float не различает, например,
    ``100000000000000.01`` и ``100000000000000.02``. Даты и время → ISO-строка.
    """
    if isinstance(value, Decimal):
        return format(value, "f")
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
        # Предварительный CAST в DOUBLE сливает соседние большие значения (2**53 и 2**53 + 1,
        # 100000000000000.01 и .02). Поэтому сумма и середины берутся в точном типе (HUGEINT
        # для целых, DECIMAL для денег и процентов), а std — по сдвигу от минимума: в DOUBLE
        # попадают уже малые разности. Среднее и медиана собираются в Python через Decimal.
        # Тип расширяется до вычитания: разность max - min может не поместиться в исходный
        # (BIGINT, DECIMAL(18,s)), и DuckDB падает с переполнением.
        wide = _wide_type(column.physical_type, semantic)
        exact = f"CAST({value} AS {wide})" if wide else value
        delta = f"CAST({exact} - (SELECT min({exact}) FROM {table}) AS DOUBLE)"
        checks |= {
            "min": f"min({value})",
            "max": f"max({value})",
            "sum": f"sum({exact})",
            "mid_low": f"quantile_disc({exact}, 0.5)",  # нижний из средних элементов
            "mid_high": f"-quantile_disc(-{exact}, 0.5)",  # верхний из средних элементов
            "std": f"stddev_samp({delta})",
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
    # пустые считаем по исходным данным: в представлении нераспознанные значения тоже NULL.
    # У живого источника счётчиков нет: типы заданы БД, нераспознанных значений не бывает,
    # и непустые считаются здесь же, в одном снимке с row_count
    non_empty = present_count if column.non_empty is None else column.non_empty
    parsed = non_empty if column.parsed is None else column.parsed
    null_count = row_count - non_empty
    return ColumnProfile(
        name=name,
        physical_type=column.physical_type,
        semantic_type=semantic,
        null_count=null_count,
        null_ratio=null_count / row_count if row_count else 0.0,
        unparsed_count=non_empty - parsed,
        distinct_count=row["distinct"],
        is_unique=present_count > 0 and row["distinct"] == present_count,
        top_values=tuple(TopValue(_json_value(v), n) for v, n in top),
        sample_values=tuple(_json_value(r[0]) for r in samples),
        stats=_stats(semantic, row),
    )


def _wide_type(physical_type: str, semantic: str) -> str | None:
    """Точный тип, в котором разность и сумма значений колонки не переполняются.

    Целые → HUGEINT, ``DECIMAL(p,s)`` → ``DECIMAL(38,s)`` (максимальная точность DuckDB
    с тем же масштабом); у DOUBLE переполнения нет — ``None``.
    """
    if semantic == "integer":
        return "HUGEINT"
    scale = _DECIMAL.fullmatch(physical_type)
    return f"DECIMAL(38,{scale[1]})" if scale else None


def _stats(semantic: str, row: dict) -> dict:
    if semantic in NUMERIC:
        mean = median = None
        if row["present"]:
            with localcontext(prec=EXACT_DIGITS):
                mean = float(Decimal(row["sum"]) / row["present"])
                median = float((Decimal(row["mid_low"]) + Decimal(row["mid_high"])) / 2)
        return {
            "min": _json_value(row["min"]),
            "max": _json_value(row["max"]),
            "mean": mean,
            "median": median,
            "std": row["std"],
        }
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
    """Уникальные колонки без пропусков и нераспознанных; ID-подобные (по типу или имени) первые."""
    if not row_count:
        return ()
    found = [
        c
        for c in columns
        if c.null_count == 0
        and c.unparsed_count == 0
        and c.is_unique
        and c.semantic_type not in NOT_KEYS
    ]
    found.sort(key=lambda c: not (c.semantic_type == "id" or _ID_NAME.search(c.name)))
    return tuple(c.name for c in found)
