"""Типизированные представления над сырыми таблицами и журнал преобразований (B1-2).

Для каждой ``raw_<имя>`` создаётся представление ``<имя>``: колонки получают
имена в snake_case латиницей, а значения приводятся к числам, деньгам, процентам,
датам и булевым. Сырая таблица не меняется, непонятные значения в представлении
становятся NULL, а в журнале ``_djedai_transformations`` остаются решение по колонке
и примеры не распознанных значений. Соответствие имён оригиналам хранится в
``_djedai_columns``.

Все правила разбора записаны в SQL (регулярные выражения и ``try_strptime`` DuckDB),
и одно и то же выражение определяет тип, считает долю распознанных значений и
попадает в представление: журнал не может разойтись с тем, что увидит запрос.

Колонка получает тип, если распознана доля ``TYPE_PARSE_THRESHOLD`` непустых значений;
иначе она остаётся текстом. ID с ведущими нулями (``00123``) и колонки ``*_id``
с целыми числами остаются текстом.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import duckdb

from djedai.config import get_limits
from djedai.data.csv_ingest import RawTable, read_sources, slugify

COLUMNS_TABLE = "_djedai_columns"
JOURNAL_TABLE = "_djedai_transformations"
EXAMPLES = 5
MAX_SCALE = 8

_SPACES = "  "  # неразрывные пробелы как разделители тысяч
_WS = rf"[\s{_SPACES}]*"
_ID_NAME = re.compile(r"(?:^|_)(?:id|kod)(?:_|$)")

# Валюты: коды ISO и написания в данных. Регистр не важен (флаг ``(?i)``).
CURRENCIES = {
    "RUB": ("₽", r"руб\.?", r"р\.", "rub", "rur"),
    "USD": (r"\$", "usd"),
    "EUR": ("€", "eur"),
    "GBP": ("£", "gbp"),
    "UAH": ("₴", r"грн\.?", "uah"),
    "KZT": ("₸", "тенге", "kzt"),
    "BYN": ("byn",),
    "CNY": ("cny",),
    "JPY": ("jpy",),
    "CHF": ("chf",),
}
_CURRENCY_ANY = "|".join(token for tokens in CURRENCIES.values() for token in tokens)

TRUE_WORDS = ("true", "yes", "да")
FALSE_WORDS = ("false", "no", "нет")

# (формат strptime, форма значения): форма отсекает «нестрогие» совпадения strptime.
_DATE_FORMATS = (
    ("%d.%m.%Y", r"\d{1,2}\.\d{1,2}\.\d{4}"),
    ("%Y-%m-%d", r"\d{4}-\d{1,2}-\d{1,2}"),
    ("%Y/%m/%d", r"\d{4}/\d{1,2}/\d{1,2}"),
    ("%d/%m/%Y", r"\d{1,2}/\d{1,2}/\d{4}"),
    ("%m/%d/%Y", r"\d{1,2}/\d{1,2}/\d{4}"),
    ("%d-%m-%Y", r"\d{1,2}-\d{1,2}-\d{4}"),
)
_TIME_SUFFIXES = ((" %H:%M:%S", r" \d{1,2}:\d{2}:\d{2}"), (" %H:%M", r" \d{1,2}:\d{2}"))
_DATETIME_FORMATS = tuple(
    (fmt + time_fmt, shape + time_shape)
    for fmt, shape in _DATE_FORMATS
    for time_fmt, time_shape in _TIME_SUFFIXES
) + (("%Y-%m-%dT%H:%M:%S", r"\d{4}-\d{1,2}-\d{1,2}T\d{2}:\d{2}:\d{2}"),)
_TEMPORAL = tuple((fmt, shape, "date") for fmt, shape in _DATE_FORMATS) + tuple(
    (fmt, shape, "datetime") for fmt, shape in _DATETIME_FORMATS
)


class TypedViewError(Exception):
    """Представление не удалось построить; ``str(error)`` можно показывать пользователю."""

    def __init__(self, table: str, reason: str) -> None:
        super().__init__(f"Таблица «{table}»: {reason}")
        self.table = table
        self.reason = reason


@dataclass(frozen=True)
class TypedColumn:  # колонка представления: оригинал, нормализованное имя, тип и доля разбора
    original_name: str
    name: str
    physical_type: str
    semantic_type: str
    format: str | None
    # None — живой источник (подключённая БД): сохранённые счётчики устарели бы при
    # изменении исходной таблицы, профайлер считает их сам в одном снимке с числом строк
    non_empty: int | None
    parsed: int | None

    @property
    def success_ratio(self) -> float:
        if self.non_empty is None or self.parsed is None:
            return 1.0
        return self.parsed / self.non_empty if self.non_empty else 1.0


@dataclass(frozen=True)
class TypedView:
    raw_table: str
    view_name: str
    columns: tuple[TypedColumn, ...]


@dataclass(frozen=True)
class TypedViewReport:  # итог пакетного построения: успешные + ошибки
    views: tuple[TypedView, ...]
    errors: tuple[TypedViewError, ...]


@dataclass(frozen=True)
class JournalEntry:  # запись журнала; detail — решение или примеры не распознанных значений
    raw_table: str
    table_name: str
    column_name: str | None
    action: str
    detail: dict


@dataclass(frozen=True)
class _Candidate:  # тип найден: SQL-выражение приведения и описание решения
    action: str
    physical_type: str
    semantic_type: str
    format: str | None
    expr: str
    detail: dict


@dataclass(frozen=True)
class _Reject:  # колонка похожа на тип, но остаётся текстом (с причиной для журнала)
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Plan:
    column: TypedColumn
    expr: str
    entries: tuple[tuple[str, dict], ...]  # (action, detail) без привязки к таблице


def normalize_column_names(columns: Sequence[str]) -> tuple[str, ...]:
    """Имена для SQL: snake_case латиницей; пустые — ``column<N>``, повторы — ``_2``, ``_3``."""
    names: list[str] = []
    for index, original in enumerate(columns, start=1):
        base = slugify(original, default=f"column{index}")
        if base[0].isdigit():
            base = f"col_{base}"
        name, number = base, 2
        while name in names:
            name, number = f"{base}_{number}", number + 1
        names.append(name)
    return tuple(names)


def create_typed_view(connection: duckdb.DuckDBPyConnection, raw: RawTable) -> TypedView:
    """Строит представление над ``raw.raw_table`` и записывает журнал; операция атомарна.

    Повторный вызов пересоздаёт представление и заменяет записи журнала этой таблицы.
    """
    try:
        _ensure_tables(connection)
        names = normalize_column_names(raw.columns)
        plans = [
            _infer_column(connection, raw.raw_table, original, name)
            for original, name in zip(raw.columns, names, strict=True)
        ]
        view_name = _view_name(connection, raw.raw_table)
        select = ", ".join(f"{plan.expr} AS {_ident(plan.column.name)}" for plan in plans)
        connection.begin()
        try:
            connection.execute(
                f"CREATE OR REPLACE VIEW {_ident(view_name)} AS "
                f"SELECT {select} FROM {_ident(raw.raw_table)}"
            )
            _write_registry(connection, raw.raw_table, view_name, plans)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    except duckdb.Error as error:
        raise TypedViewError(
            raw.raw_table, f"не удалось построить представление ({error})"
        ) from error
    return TypedView(raw.raw_table, view_name, tuple(plan.column for plan in plans))


def create_typed_views(connection: duckdb.DuckDBPyConnection) -> TypedViewReport:
    """Строит представления для всех загруженных файлов; ошибка одной таблицы не мешает другим."""
    views: list[TypedView] = []
    errors: list[TypedViewError] = []
    for raw in read_sources(connection):
        try:
            views.append(create_typed_view(connection, raw))
        except TypedViewError as error:
            errors.append(error)
    return TypedViewReport(views=tuple(views), errors=tuple(errors))


def read_typed_views(connection: duckdb.DuckDBPyConnection) -> list[TypedView]:
    """Читает построенные представления из реестра ``_djedai_columns`` (по имени raw-таблицы)."""
    _ensure_tables(connection)
    cursor = connection.execute(
        f"SELECT raw_table, view_name, original_name, normalized_name, physical_type, "
        f"semantic_type, format, non_empty, parsed FROM {COLUMNS_TABLE} "
        f"ORDER BY raw_table, position"
    )
    views: dict[tuple[str, str], list[TypedColumn]] = {}
    for raw_table, view_name, *column in cursor.fetchall():
        views.setdefault((raw_table, view_name), []).append(TypedColumn(*column))
    return [TypedView(raw, view, tuple(columns)) for (raw, view), columns in views.items()]


def read_journal(
    connection: duckdb.DuckDBPyConnection, raw_table: str | None = None
) -> list[JournalEntry]:
    """Читает журнал преобразований (в порядке записи), при ``raw_table`` — одной таблицы."""
    _ensure_tables(connection)
    where, params = ("WHERE raw_table = ?", [raw_table]) if raw_table else ("", [])
    cursor = connection.execute(
        f"SELECT raw_table, table_name, column_name, action, detail FROM {JOURNAL_TABLE} "
        f"{where} ORDER BY created_at, raw_table, entry_no",
        params,
    )
    return [JournalEntry(*row[:4], json.loads(row[4])) for row in cursor.fetchall()]


def read_currencies(
    connection: duckdb.DuckDBPyConnection, raw_table: str | None = None
) -> dict[tuple[str, str], str]:
    """Валюта денежных колонок из журнала: ``(имя представления, имя колонки) → код ISO``.

    Валюта есть только у колонок, разобранных как ``money``. Колонка со смешанными или
    неизвестными валютами остаётся текстом (``keep_text``) и в результат не попадает.
    """
    return {
        (entry.table_name, entry.column_name): entry.detail["currency"]
        for entry in read_journal(connection, raw_table)
        if entry.action == "parse_money" and entry.column_name and entry.detail.get("currency")
    }


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ensure_tables(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {COLUMNS_TABLE} (
            raw_table VARCHAR, view_name VARCHAR, position INTEGER, original_name VARCHAR,
            normalized_name VARCHAR, physical_type VARCHAR, semantic_type VARCHAR,
            format VARCHAR, non_empty BIGINT, parsed BIGINT,
            PRIMARY KEY (raw_table, position))"""
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {JOURNAL_TABLE} (
            raw_table VARCHAR, entry_no INTEGER, table_name VARCHAR, column_name VARCHAR,
            action VARCHAR, detail VARCHAR, created_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (raw_table, entry_no))"""
    )


def _view_name(
    connection: duckdb.DuckDBPyConnection, raw_table: str, base: str | None = None
) -> str:
    """Имя представления — ``base`` или имя сырой таблицы без ``raw_``; при коллизии ``_typed``.

    Занятыми считаются таблицы, чужие представления текущей БД DuckDB и представления
    других raw-таблиц из реестра; таблицы подключённых БД (``ATTACH``) имя не занимают.
    Своё прежнее представление можно пересоздать под тем же именем.
    """
    base = base or raw_table.removeprefix("raw_") or raw_table
    own = {
        r[0]
        for r in connection.execute(
            f"SELECT view_name FROM {COLUMNS_TABLE} WHERE raw_table = ?", [raw_table]
        ).fetchall()
    }
    here = "database_name = current_database()"
    taken = {
        r[0]
        for r in connection.execute(
            f"SELECT table_name FROM duckdb_tables() WHERE {here}"
        ).fetchall()
    }
    taken |= {
        r[0]
        for r in connection.execute(
            f"SELECT view_name FROM duckdb_views() WHERE NOT internal AND {here}"
        ).fetchall()
    } - own
    taken |= {
        r[0]
        for r in connection.execute(
            f"SELECT view_name FROM {COLUMNS_TABLE} WHERE raw_table <> ?", [raw_table]
        ).fetchall()
    }
    if base not in taken:
        return base
    candidate, number = f"{base}_typed", 2
    while candidate in taken:
        candidate, number = f"{base}_typed_{number}", number + 1
    return candidate


def _write_registry(
    connection: duckdb.DuckDBPyConnection, raw_table: str, view_name: str, plans: list[_Plan]
) -> None:
    for table in (COLUMNS_TABLE, JOURNAL_TABLE):
        connection.execute(f"DELETE FROM {table} WHERE raw_table = ?", [raw_table])
    entry_no = 0
    for position, plan in enumerate(plans, start=1):
        column = plan.column
        connection.execute(
            f"INSERT INTO {COLUMNS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                raw_table,
                view_name,
                position,
                column.original_name,
                column.name,
                column.physical_type,
                column.semantic_type,
                column.format,
                column.non_empty,
                column.parsed,
            ],
        )
        for action, detail in plan.entries:
            entry_no += 1
            connection.execute(
                f"INSERT INTO {JOURNAL_TABLE} (raw_table, entry_no, table_name, column_name, "
                "action, detail) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    raw_table,
                    entry_no,
                    view_name,
                    column.name,
                    action,
                    json.dumps(detail, ensure_ascii=False),
                ],
            )


def _number_pattern(decimal: str, *, integer: bool = False) -> str:
    """Регулярное выражение числа с необязательными валютой и ``%``; ``decimal`` — ``,`` или ``.``.

    Для ``,`` разряды отделяются пробелом, а точка допустима только вместе с дробной
    частью (``1.234,56``); для ``.`` — пробелом, запятой или апострофом. Одиночное
    ``1,234`` подходит под оба шаблона: выбор между ними делает ``_try_number``.
    """
    fraction = "" if integer else (r"(?:,\d+)?" if decimal == "," else r"(?:\.\d+)?")
    if decimal == ",":
        # точка как разделитель разрядов (1.234,56) бывает только с дробной частью
        dotted = "" if integer else r"|\d{1,3}(?:\.\d{3})+,\d+"
        core = rf"[-+]?(?:\d{{1,3}}(?:[ {_SPACES}]\d{{3}})+{fraction}{dotted}|\d+{fraction})"
    else:
        core = rf"[-+]?(?:\d{{1,3}}(?:[ {_SPACES}',]\d{{3}})+{fraction}|\d+{fraction})"
    currency = f"(?:{_CURRENCY_ANY})"
    return rf"(?i){_WS}(?:{currency}{_WS})?{core}(?:{_WS}{currency})?(?:{_WS}%)?{_WS}"


def _clean_number(value: str, decimal: str) -> str:
    """SQL: убирает валюту, пробелы и разделители разрядов, оставляя ``12345.67``."""
    stripped = f"regexp_replace({value}, {_lit(f'(?i)(?:{_CURRENCY_ANY})')}, '', 'g')"
    if decimal == ",":
        return f"replace(regexp_replace({stripped}, '[^0-9,+-]', '', 'g'), ',', '.')"
    return f"regexp_replace({stripped}, '[^0-9.+-]', '', 'g')"


def _stats(connection: duckdb.DuckDBPyConnection, table: str, column: str) -> dict[str, int]:
    """Один проход по колонке: сколько значений подходит под каждый кандидат."""
    value = f"trim({column})"
    checks = {
        "ne": f"{value} <> ''",
        "lead_zero": f"regexp_full_match({value}, '[-+]?0[0-9]+')",
        "bool": f"lower({value}) IN ({', '.join(map(_lit, TRUE_WORDS + FALSE_WORDS))})",
        "sep": f"{value} <> '' AND regexp_matches({value}, '[.,]')",
    }
    for decimal in (",", "."):
        checks[f"num{decimal}"] = f"regexp_full_match({value}, {_lit(_number_pattern(decimal))})"
        checks[f"int{decimal}"] = (
            f"regexp_full_match({value}, {_lit(_number_pattern(decimal, integer=True))})"
        )
    for index, (fmt, shape, _) in enumerate(_TEMPORAL):
        checks[f"fmt{index}"] = (
            f"regexp_full_match({value}, {_lit(shape)}) "
            f"AND try_strptime({value}, {_lit(fmt)}) IS NOT NULL"
        )
    select = ", ".join(f"count(*) FILTER (WHERE {check})" for check in checks.values())
    row = connection.execute(f"SELECT {select} FROM {_ident(table)}").fetchone()
    return dict(zip(checks, row, strict=True))


def _infer_column(
    connection: duckdb.DuckDBPyConnection, table: str, original: str, name: str
) -> _Plan:
    column = _ident(original)
    stats = _stats(connection, table, column)
    non_empty = stats["ne"]
    outcome: _Candidate | _Reject | None = None
    if non_empty == 0:
        outcome = _Reject("empty_column")
    else:
        for step in (_try_bool, _try_number, _try_date):
            outcome = step(connection, table, column, name, stats)
            if outcome is not None:
                break
    value = f"trim({column})"
    if isinstance(outcome, _Candidate):
        parsed = connection.execute(
            f"SELECT count(*) FILTER (WHERE {value} <> '' AND ({outcome.expr}) IS NOT NULL) "
            f"FROM {_ident(table)}"
        ).fetchone()[0]
        if parsed < get_limits().type_parse_threshold * non_empty:
            outcome = _Reject(
                "below_threshold",
                {"candidate": outcome.semantic_type, "success_ratio": _ratio(parsed, non_empty)},
            )
    if not isinstance(outcome, _Candidate):
        reason = outcome or _Reject("text")
        semantic = "id" if reason.reason in ("id_leading_zeros", "id_name") else "text"
        typed = TypedColumn(original, name, "VARCHAR", semantic, None, non_empty, non_empty)
        entries = _rename_entries(original, name) + (
            ("keep_text", {"reason": reason.reason, **reason.detail}),
        )
        return _Plan(typed, column, entries)

    typed = TypedColumn(
        original, name, outcome.physical_type, outcome.semantic_type, outcome.format,
        non_empty, parsed,
    )  # fmt: skip
    entries = _rename_entries(original, name) + (
        (
            outcome.action,
            {
                **outcome.detail,
                "type": outcome.physical_type,
                "non_empty": non_empty,
                "parsed": parsed,
                "success_ratio": _ratio(parsed, non_empty),
            },
        ),
    )
    if parsed < non_empty:
        examples = [
            row[0]
            for row in connection.execute(
                f"SELECT DISTINCT {column} FROM {_ident(table)} "
                f"WHERE {value} <> '' AND ({outcome.expr}) IS NULL ORDER BY 1 LIMIT {EXAMPLES}"
            ).fetchall()
        ]
        entries += (
            (
                "unparsed_values",
                {
                    "count": non_empty - parsed,
                    "examples": examples,
                    "target": outcome.semantic_type,
                },
            ),
        )
    return _Plan(typed, outcome.expr, entries)


def _ratio(parsed: int, total: int) -> float:
    return round(parsed / total, 4) if total else 1.0


def _rename_entries(original: str, name: str) -> tuple[tuple[str, dict], ...]:
    if original == name:
        return ()
    return (("rename_column", {"from": original, "to": name}),)


def _try_bool(connection, table, column, name, stats) -> _Candidate | None:
    if stats["bool"] < get_limits().type_parse_threshold * stats["ne"]:
        return None
    value = f"lower(trim({column}))"
    true_list = ", ".join(map(_lit, TRUE_WORDS))
    false_list = ", ".join(map(_lit, FALSE_WORDS))
    expr = (
        f"CASE WHEN {value} IN ({true_list}) THEN TRUE "
        f"WHEN {value} IN ({false_list}) THEN FALSE END"
    )
    return _Candidate("parse_bool", "BOOLEAN", "bool", None, expr, {})


def _try_number(connection, table, column, name, stats) -> _Candidate | _Reject | None:
    need = get_limits().type_parse_threshold * stats["ne"]
    fits = {d: stats[f"num{d}"] >= need for d in (",", ".")}
    if not any(fits.values()):
        return None
    if fits[","] and fits["."]:
        decimal = "," if stats["num,"] >= stats["num."] else "."
    else:
        decimal = "," if fits[","] else "."
    ambiguous = fits[","] and fits["."] and stats["num,"] == stats["num."] and stats["sep"] > 0
    is_int = stats[f"int{decimal}"] == stats[f"num{decimal}"]

    if stats["lead_zero"]:
        return _Reject("id_leading_zeros", {"examples_with_zeros": stats["lead_zero"]})
    if is_int and _ID_NAME.search(name):
        return _Reject("id_name")

    value = f"trim({column})"
    guard = f"regexp_full_match({value}, {_lit(_number_pattern(decimal))})"
    clean = _clean_number(value, decimal)
    currency_counts = ", ".join(
        f"count(*) FILTER (WHERE {guard} AND regexp_matches({value}, "
        f"{_lit('(?i)(?:' + '|'.join(tokens) + ')')}))"
        for tokens in CURRENCIES.values()
    )
    row = connection.execute(
        f"SELECT count(*) FILTER (WHERE {guard} AND contains({value}, '%')), "
        f"coalesce(max(length(regexp_extract({clean}, '\\.([0-9]+)$', 1))) "
        f"FILTER (WHERE {guard}), 0), "
        f"{currency_counts} FROM {_ident(table)}"
    ).fetchone()
    percent, scale, per_currency = row[0], row[1], row[2:]
    currencies = [code for code, n in zip(CURRENCIES, per_currency, strict=True) if n]
    if len(currencies) > 1:
        return _Reject("mixed_currencies", {"currencies": currencies})
    if currencies and percent:
        return _Reject("mixed_money_percent", {"currencies": currencies})

    separator = "decimal_comma" if decimal == "," else "decimal_point"
    detail: dict = {"decimal_separator": decimal}
    if ambiguous:
        detail["note"] = "ambiguous_separator: одиночный разделитель принят за десятичный"
    scale = min(max(scale, 2), MAX_SCALE)
    if currencies or percent:
        semantic = "money" if currencies else "percent"
        physical, action = f"DECIMAL(18,{scale})", f"parse_{semantic}"
        if currencies:
            detail["currency"] = currencies[0]
        fmt = f"{separator}, {currencies[0]}" if currencies else f"{separator}, percent"
    elif is_int:
        semantic, physical, action, fmt = "integer", "BIGINT", "parse_integer", separator
    else:
        semantic, physical, action, fmt = "float", "DOUBLE", "parse_number", separator
    expr = f"CASE WHEN {guard} THEN TRY_CAST({clean} AS {physical}) END"
    return _Candidate(action, physical, semantic, fmt, expr, detail)


def _try_date(connection, table, column, name, stats) -> _Candidate | _Reject | None:
    counts = [(stats[f"fmt{i}"], i) for i in range(len(_TEMPORAL))]
    best = max(count for count, _ in counts)
    if best < get_limits().type_parse_threshold * stats["ne"]:
        return None
    leaders = [i for count, i in counts if count == best]
    if len(leaders) > 1:
        # все значения подходят под несколько форматов (например, 03/04/2026): день и месяц
        # не различить, поэтому ничего не угадываем
        return _Reject("ambiguous_date", {"candidates": [_TEMPORAL[i][0] for i in leaders]})
    fmt, shape, kind = _TEMPORAL[leaders[0]]
    value = f"trim({column})"
    parsed = f"try_strptime({value}, {_lit(fmt)})"
    if kind == "date":
        expr, physical = (
            f"CASE WHEN regexp_full_match({value}, {_lit(shape)}) THEN CAST({parsed} AS DATE) END",
            "DATE",
        )
    else:
        expr, physical = (
            f"CASE WHEN regexp_full_match({value}, {_lit(shape)}) THEN {parsed} END",
            "TIMESTAMP",
        )
    return _Candidate(f"parse_{kind}", physical, kind, fmt, expr, {"format": fmt})
