import math
import time
import uuid
from collections.abc import Collection
from functools import partial

import duckdb
import numpy as np
import sqlglot
from sqlglot import exp

from djedai.analytics.metrics import MetricContext, MetricError, metric_guard
from djedai.analytics.queries import check_budget, check_dataset_table, execute_dataset_query
from djedai.analytics.scope import DatasetScopeError, parse_read_query, validate_dataset_query
from djedai.config import get_limits
from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultData,
)
from djedai.execution import TurnCancelled, TurnTimeout, query_timer


def _quote_id(identifier: str) -> str:
    """Безопасно оборачивает идентификатор в кавычки (защита от инъекций)."""
    return '"' + identifier.replace('"', '""') + '"'


def _parse_filter_sql(filter_str: str | None) -> exp.Expression | None:
    """Парсит SQL-фрагмент WHERE (без инъекций). Бросает ValueError при ошибке."""
    if not filter_str:
        return None
    try:
        statements = sqlglot.parse(f"SELECT * FROM t WHERE (\n{filter_str}\n)", read="duckdb")
    except Exception as e:
        raise ValueError(f"Не удалось разобрать фильтр: {e}") from e
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Фильтр должен содержать одно выражение")
    parsed = statements[0]
    if any(
        value for key, value in parsed.args.items() if key not in {"expressions", "from_", "where"}
    ):
        raise ValueError("Фильтр не должен содержать посторонних SQL-конструкций")
    where_clause = parsed.args.get("where")
    if not isinstance(where_clause, exp.Where):
        raise ValueError("Не удалось извлечь WHERE-выражение")
    return where_clause.this


def _column_type(con: duckdb.DuckDBPyConnection, table: str, column: str) -> str | None:
    row = con.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
        [table, column],
    ).fetchone()
    return row[0] if row else None


def _list_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def _err(result_id: str | None = None, **kwargs) -> ToolResult:
    """Сахар для быстрого возврата ошибки."""
    return ToolResult(result_id=result_id or str(uuid.uuid4()), status="error", **kwargs)


_FREQ_UNITS = {"minute", "hour", "day", "week", "month", "quarter", "year"}
_AGG_FUNCS = {
    "count": "COUNT",
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
    "stddev": "STDDEV_POP",
}
_FREQ_MIN_SECONDS = {
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
    "week": 7 * 86_400,
    "month": 28 * 86_400,
    "quarter": 89 * 86_400,
    "year": 365 * 86_400,
}
_MAX_CALENDAR_BUCKETS = 200_000
_ANOMALY_METHODS = {"iqr", "zscore", "mad", "stl-residual"}
_DEFAULT_THRESHOLDS = {
    "iqr": 1.5,
    "zscore": 3.0,
    "mad": 3.5,
    "stl-residual": 3.0,
}
_NUMERIC_TYPES = (
    "INT",
    "DECIMAL",
    "DOUBLE",
    "FLOAT",
    "REAL",
    "NUMERIC",
    "HUGEINT",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "UBIGINT",
)
_STL_PERIOD_BY_FREQ = {
    "minute": 60,
    "hour": 24,
    "day": 7,
    "week": 52,
    "month": 12,
    "quarter": 4,
    "year": 1,
}


def _to_python(v):
    """numpy/pandas scalar → Python-native значение для JSON/Parquet."""
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    return v


_RU_MONTHS = [
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
]
_RU_WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]
_STL_PERIOD_BY_FREQ = {
    "minute": 60,
    "hour": 24,
    "day": 7,
    "week": 52,
    "month": 12,
    "quarter": 4,
    "year": 1,
}
_PANDAS_FREQ = {
    "minute": "min",
    "hour": "h",
    "day": "D",
    "week": "W-MON",
    "month": "MS",
    "quarter": "QS",
    "year": "YS",
}


def _cohens_d_label(d: float) -> str:
    ad = abs(d)
    if ad < 0.2:
        return "negligible"
    if ad < 0.5:
        return "small"
    if ad < 0.8:
        return "medium"
    return "large"


def run_sql(
    sql: str,
    max_rows: int | None = None,
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
    semantics: MetricContext | None = None,
) -> ToolResult:
    """Безопасно выполняет SQL-запрос (только SELECT/CTE) в DuckDB.

    С ``semantics`` запрос, агрегирующий колонку подтверждённой метрики через JOIN
    с размножением или потерей строк, отклоняется до выполнения (B2-9).
    """
    con = None
    try:
        limits = get_limits()
        if max_rows is not None and (
            not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1
        ):
            raise ValueError("max_rows должен быть положительным целым числом")
        max_rows = (
            min(max_rows, limits.sql_max_rows) if max_rows is not None else limits.sql_max_rows
        )
        # 1. ПАРСИМ SQL
        try:
            parsed = parse_read_query(sql)
        except DatasetScopeError:
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(code="forbidden_operation", message="Разрешён один запрос SELECT."),
            )
        except Exception as e:
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(
                    code="sql_parse_error",
                    message=f"Синтаксическая ошибка в SQL: {str(e)}",
                    suggestion="Проверьте правильность написания SQL-запроса.",
                ),
            )

        # 2. РАЗРЕШАЕМ ТОЛЬКО SELECT/CTE
        if not isinstance(parsed, (exp.Select, exp.Union)):
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(
                    code="forbidden_operation",
                    message="Разрешены только запросы SELECT.",
                    suggestion="Уберите команды изменения данных (INSERT, UPDATE, DROP и т.д.).",
                ),
            )

        if allowed_tables is not None:
            try:
                validate_dataset_query(parsed, allowed_tables)
            except (DatasetScopeError, sqlglot.errors.OptimizeError):
                return ToolResult(
                    result_id=str(uuid.uuid4()),
                    status="error",
                    error=ToolError(
                        code="dataset_scope_violation",
                        message="Запрос выходит за область выбранного датасета.",
                    ),
                )

        try:
            problem = metric_guard(parsed, semantics) if semantics is not None else None
        except Exception:
            # Непроверенный запрос к мере подтверждённой метрики не выполняется.
            problem = MetricError(
                "metric_guard_failed",
                "Не удалось проверить, сохраняет ли запрос grain подтверждённых метрик.",
                "Используйте compute_metric или упростите запрос.",
            )
        if problem is not None:
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(
                    code=problem.code, message=problem.message, suggestion=problem.suggestion
                ),
            )

        # 3. ВЫПОЛНЯЕМ С ТАЙМАУТОМ
        con = connection if connection is not None else duckdb.connect(database=":memory:")
        con.execute("SET enable_external_access = false;")

        if connection is None:
            con.execute("CREATE TABLE test (id INT, name VARCHAR)")
            con.execute("INSERT INTO test VALUES (1, 'Alice'), (2, 'Bob')")

        timer = query_timer(limits.tool_timeout_seconds, con.interrupt)
        timer.start()

        try:
            # Лимит на уровне relation ограничивает работу движка, сохраняя
            # исходные LIMIT/FETCH, ORDER BY, OFFSET и повторяющиеся имена колонок.
            cursor = con.sql(sql).limit(max_rows).execute()
            result = cursor.fetchmany(max_rows)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
        finally:
            timer.cancel()

        # 4. УПАКОВЫВАЕМ ПОД НОВЫЙ КОНТРАКТ
        rows = [list(row) for row in result]  # tuple -> list для JSON
        row_count = len(rows)

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="run_sql",
            sql=sql,
            params={"sql": sql, "max_rows": max_rows},
            data=ToolResultData(
                columns=columns,
                rows=rows,
                row_count=row_count,
                truncated=row_count >= max_rows,
            ),
            facts=[
                Fact(
                    text=f"Запрос выполнен успешно, получено строк: {row_count}",
                    kind="summary",
                    values={"row_count": row_count},
                )
            ],
        )

    except Exception as e:
        error_msg = str(e)
        suggestion = "Проверьте названия таблиц и колонок."

        if "timeout" in error_msg.lower() or "interrupt" in error_msg.lower():
            suggestion = (
                "Запрос выполнялся слишком долго. Попробуйте упростить запрос или "
                "добавить фильтры (WHERE)."
            )
        elif "external" in error_msg.lower() or "permission" in error_msg.lower():
            suggestion = (
                "Запрещено обращаться к внешним файлам или сети. "
                "Используйте только загруженные таблицы."
            )

        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(
                code="database_error",
                message=f"Ошибка базы данных: {error_msg}",
                suggestion=suggestion,
            ),
        )
    finally:
        if connection is None and con is not None:
            con.close()


# --- ОПИСАТЕЛЬНЫЕ ИНСТРУМЕНТЫ (B2-3) ---


def _get_mock_connection():
    """Вспомогательная функция: тестовая БД в памяти."""
    con = duckdb.connect(database=":memory:")
    con.execute("SET enable_external_access = false;")
    con.execute("CREATE TABLE test (id INT, city VARCHAR, age INT)")
    con.execute(
        "INSERT INTO test VALUES (1, 'Москва', 25), (2, 'СПб', 30), "
        "(3, 'Москва', 25), (4, 'Казань', NULL)"
    )
    con.execute("CREATE TABLE events (id INT, ts TIMESTAMP, city VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO events VALUES "
        "(1, '2024-01-15 10:00:00', 'Москва', 100.0), "
        "(2, '2024-01-15 14:00:00', 'СПб',    200.0), "
        "(3, '2024-01-15 20:00:00', 'Москва', 110.0), "
        "(4, '2024-01-31 23:59:59', 'СПб',    190.0), "
        "(5, '2024-02-01 00:00:00', 'Москва', 105.0), "
        "(6, '2024-02-01 00:00:00', 'Москва', 105.0), "
        "(7, '2024-02-15 12:00:00', 'СПб',  10000.0), "
        "(8, '2024-03-01 00:00:00', 'Москва', 120.0)"
    )
    return con


def describe_dataset(
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Возвращает обзор всех таблиц в базе данных."""
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        if allowed_tables is None:
            tables = con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
            table_names = [t[0] for t in tables]
        else:
            table_names = sorted(allowed_tables)

        rows: list[list] = []
        facts: list[Fact] = [
            Fact(
                text=f"В базе {len(table_names)} таблиц: {', '.join(table_names) or '—'}",
                kind="dataset_summary",
                values={"table_count": len(table_names), "tables": table_names},
            )
        ]

        for t_name in table_names:
            row_count = con.execute(f"SELECT COUNT(*) FROM {_quote_id(t_name)}").fetchone()[0]

            cols = con.execute(f"DESCRIBE SELECT * FROM {_quote_id(t_name)}").fetchall()
            col_names = [c[0] for c in cols]
            col_types = {c[0]: c[1] for c in cols}

            # Табличное представление: одна строка на таблицу
            rows.append([t_name, row_count, ", ".join(col_names)])

            facts.append(
                Fact(
                    text=f"Таблица '{t_name}': {row_count} строк, колонки: {', '.join(col_names)}",
                    kind="table_overview",
                    values={
                        "table_name": t_name,
                        "row_count": row_count,
                        "columns": col_names,
                        "column_types": col_types,
                    },
                )
            )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="describe_dataset",
            params={},
            data=ToolResultData(
                columns=["table_name", "row_count", "columns"],
                rows=rows[: get_limits().sql_max_rows],
                row_count=min(len(rows), get_limits().sql_max_rows),
                truncated=len(rows) > get_limits().sql_max_rows,
            ),
            facts=facts,
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )
    finally:
        timer.cancel()
        if connection is None:
            con.close()


def describe_column(
    table: str,
    column: str,
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Возвращает полный профиль колонки: пропуски, уникальные, статистику, частоты."""
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        if allowed_tables is not None and table not in allowed_tables:
            raise DatasetScopeError("Таблица не относится к выбранному датасету")
        # [P1] Проверка через параметры
        schema = con.execute(f"DESCRIBE SELECT * FROM {_quote_id(table)}").fetchall()
        col_check = next(([item[1]] for item in schema if item[0] == column), None)

        if not col_check:
            available = schema
            avail_str = ", ".join([c[0] for c in available]) if available else "Таблица не найдена"
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка '{column}' не найдена.",
                    suggestion=f"Доступные колонки: {avail_str}",
                ),
            )

        safe_table = _quote_id(table)
        safe_col = _quote_id(column)

        # --- Базовые счётчики ---
        stats = con.execute(
            f"SELECT COUNT(*), COUNT({safe_col}), COUNT(DISTINCT {safe_col}) FROM {safe_table}"
        ).fetchone()
        total, non_null, distinct = stats
        null_count = total - non_null

        base_values = {
            "total": total,
            "non_null": non_null,
            "distinct": distinct,
            "null_count": null_count,
        }

        facts: list[Fact] = [
            Fact(
                text=f"Всего строк: {total}, пропусков: {null_count}, уникальных: {distinct}",
                kind="profile",
                values=base_values,
            )
        ]

        # --- Числовая статистика ---
        dtype = col_check[0].upper()
        if any(t in dtype for t in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "REAL")):
            num_stats = con.execute(
                f"SELECT MIN({safe_col}), MAX({safe_col}), AVG({safe_col}), "
                f"MEDIAN({safe_col}), STDDEV_POP({safe_col}) FROM {safe_table}"
            ).fetchone()
            if num_stats[0] is not None:
                std_val = round(num_stats[4], 2) if num_stats[4] is not None else None
                num_values = {
                    "min": num_stats[0],
                    "max": num_stats[1],
                    "mean": round(num_stats[2], 2),
                    "median": num_stats[3],
                    "std": std_val,
                }
                facts.append(
                    Fact(
                        text=f"Мин: {num_stats[0]}, Макс: {num_stats[1]}, "
                        f"Среднее: {round(num_stats[2], 2)}, "
                        f"Медиана: {num_stats[3]}, Std: {std_val}",
                        kind="numeric_profile",
                        values=num_values,
                    )
                )

        # --- Частоты (табличное представление) + факты ---
        freqs = con.execute(
            f"SELECT {safe_col}, COUNT(*) FROM {safe_table} "
            f"GROUP BY {safe_col} ORDER BY COUNT(*) DESC LIMIT 10"
        ).fetchall()

        freq_rows = [[val, cnt] for val, cnt in freqs]
        for val, cnt in freqs:
            display = val if val is not None else "ПУСТО"
            facts.append(
                Fact(
                    text=f"Значение '{display}' встречается {cnt} раз.",
                    kind="freq",
                    values={"value": val, "count": cnt},
                )
            )

        # --- Примеры значений ---
        examples = con.execute(
            f"SELECT DISTINCT {safe_col} FROM {safe_table} WHERE {safe_col} IS NOT NULL LIMIT 5"
        ).fetchall()
        if examples:
            example_list = [e[0] for e in examples]
            facts.append(
                Fact(
                    text=f"Примеры значений: {', '.join(map(str, example_list))}",
                    kind="examples",
                    values={"examples": example_list},
                )
            )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="describe_column",
            params={"table": table, "column": column},
            data=ToolResultData(
                columns=[column, "count"],
                rows=freq_rows,
                row_count=len(freq_rows),
            ),
            facts=facts,
            chart_hint=ChartHint(type="bar", x=column, y=["count"]),
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )
    finally:
        timer.cancel()
        if connection is None:
            con.close()


def value_counts(
    table: str,
    column: str,
    top_n: int = 10,
    filter: str | None = None,
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Возвращает топ-N самых частых значений в колонке."""
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        if allowed_tables is not None and table not in allowed_tables:
            raise DatasetScopeError("Таблица не относится к выбранному датасету")
        con.execute("SET enable_external_access = false;")
        schema = con.execute(f"DESCRIBE SELECT * FROM {_quote_id(table)}").fetchall()
        col_check = next((item for item in schema if item[0] == column), None)
        if not col_check:
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(code="invalid_column", message="Колонка не найдена"),
            )

        safe_table = _quote_id(table)
        safe_col = _quote_id(column)

        query = f"SELECT {safe_col}, COUNT(*) AS cnt FROM {safe_table}"

        if filter:
            try:
                statements = sqlglot.parse(f"SELECT * FROM t WHERE (\n{filter}\n)", read="duckdb")
                if len(statements) != 1 or not isinstance(statements[0], exp.Select):
                    raise ValueError("Expected a single WHERE expression")
                parsed_filter = statements[0]
                if any(
                    value
                    for key, value in parsed_filter.args.items()
                    if key not in {"expressions", "from_", "where"}
                ):
                    raise ValueError("Unexpected SQL clause in filter")
                where_clause = parsed_filter.args.get("where")
                if not isinstance(where_clause, exp.Where):
                    raise ValueError("Missing WHERE expression")
                query += f" WHERE {where_clause.this.sql(dialect='duckdb')}"
            except Exception:
                return ToolResult(
                    result_id=str(uuid.uuid4()),
                    status="error",
                    error=ToolError(code="invalid_filter", message="Некорректный фильтр"),
                )

        query += f" GROUP BY {safe_col} ORDER BY cnt DESC LIMIT {top_n}"

        if allowed_tables is not None:
            validate_dataset_query(parse_read_query(query), allowed_tables)

        result = con.execute(query).fetchall()

        if not result:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="value_counts",
                params={
                    "table": table,
                    "column": column,
                    "top_n": top_n,
                    "filter": filter,
                },
                facts=[
                    Fact(
                        text="По заданному фильтру данные не найдены.",
                        kind="empty_result",
                    )
                ],
            )

        rows = [[val, cnt] for val, cnt in result]

        facts = [
            Fact(
                text=f"Значение '{val if val is not None else 'ПУСТО'}' встречается {cnt} раз.",
                kind="freq",
                values={"value": val, "count": cnt},
            )
            for val, cnt in result
        ]

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="value_counts",
            params={
                "table": table,
                "column": column,
                "top_n": top_n,
                "filter": filter,
            },
            data=ToolResultData(
                columns=[column, "count"],
                rows=rows[: get_limits().sql_max_rows],
                row_count=min(len(rows), get_limits().sql_max_rows),
                truncated=len(rows) > get_limits().sql_max_rows,
            ),
            facts=facts,
            chart_hint=ChartHint(type="bar", x=column, y=["count"]),
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# TIME AGGREGATE
# ============================================================================


def time_aggregate(
    table: str,
    time_col: str,
    *,
    measure: str | None = None,
    agg: str = "count",
    freq: str = "day",
    group_by: list[str] | None = None,
    filter: str | None = None,
    top_n: int | None = None,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Агрегирует метрику по временным бакетам с опциональной группировкой."""
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)
        # ---------- валидация ----------
        if freq not in _FREQ_UNITS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая частота: '{freq}'.",
                    suggestion=f"Допустимо: {sorted(_FREQ_UNITS)}",
                )
            )
        if agg not in _AGG_FUNCS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая агрегация: '{agg}'.",
                    suggestion=f"Допустимо: {sorted(_AGG_FUNCS)}",
                )
            )
        if agg != "count" and measure is None:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Для agg='{agg}' обязателен measure.",
                    suggestion="Укажите measure или используйте agg='count'.",
                )
            )
        if top_n is not None and (
            not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1
        ):
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="top_n должен быть > 0.",
                )
            )

        group_by = group_by or []

        # ---------- проверка колонок ----------
        col_type = _column_type(con, table, time_col)
        if col_type is None:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка времени '{time_col}' не найдена.",
                    suggestion=f"Доступно: {', '.join(_list_columns(con, table))}",
                )
            )
        if (
            freq in {"minute", "hour", "second"}
            and "DATE" in col_type.upper()
            and "TIMESTAMP" not in col_type.upper()
        ):
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Частота '{freq}' требует TIMESTAMP, а '{time_col}' = {col_type}.",
                    suggestion="Используйте freq='day' или TIMESTAMP-колонку.",
                )
            )
        for col in group_by:
            if _column_type(con, table, col) is None:
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' не найдена.",
                    )
                )
        if measure is not None and _column_type(con, table, measure) is None:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка метрики '{measure}' не найдена.",
                )
            )

        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        # ---------- сборка SQL ----------
        safe_table = _quote_id(table)
        safe_time = _quote_id(time_col)
        safe_groups = [_quote_id(g) for g in group_by]

        if agg == "count":
            agg_expr = "COUNT(*)" if measure is None else f"COUNT({_quote_id(measure)})"
        else:
            agg_expr = f"{_AGG_FUNCS[agg]}({_quote_id(measure)})"

        select_parts = [
            f"DATE_TRUNC('{freq}', CAST({safe_time} AS TIMESTAMP)) AS time_bucket",
            *safe_groups,
            f"{agg_expr} AS value",
        ]
        n_group = 1 + len(group_by)
        group_positions = ", ".join(str(i) for i in range(1, n_group + 1))
        order_positions = group_positions  # time_bucket, затем группы

        effective_limit = get_limits().sql_max_rows
        if top_n is not None:
            effective_limit = min(top_n, effective_limit)

        query = (
            f"SELECT {', '.join(select_parts)} FROM {safe_table} "
            f"WHERE {safe_time} IS NOT NULL{filter_sql} "
            f"GROUP BY {group_positions} "
            f"ORDER BY {order_positions} ASC "
            f"LIMIT {effective_limit + 1}"
        )

        rows_raw = execute(query).fetchall()
        truncated = len(rows_raw) > effective_limit
        rows_raw = rows_raw[:effective_limit]
        row_count = len(rows_raw)

        columns = ["time_bucket", *group_by, "value"]
        data = ToolResultData(
            columns=columns,
            rows=[list(r) for r in rows_raw],
            row_count=row_count,
            truncated=truncated,
        )

        if row_count == 0:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="time_aggregate",
                params={
                    "table": table,
                    "time_col": time_col,
                    "measure": measure,
                    "agg": agg,
                    "freq": freq,
                    "group_by": group_by,
                    "filter": filter,
                    "top_n": top_n,
                },
                data=data,
                facts=[
                    Fact(
                        text="По заданным условиям нет ни одного временного бакета.",
                        kind="empty_result",
                    )
                ],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        # ---------- факты ----------
        facts: list[Fact] = []
        buckets = [r[0] for r in rows_raw]
        vals = [(r[-1], r) for r in rows_raw if r[-1] is not None]

        facts.append(
            Fact(
                text=f"Всего временных бакетов: {row_count} (частота: {freq}).",
                kind="time_summary",
                values={"bucket_count": row_count, "freq": freq},
            )
        )
        facts.append(
            Fact(
                text=f"Период: с {min(buckets)} по {max(buckets)}.",
                kind="time_range",
                values={"start": str(min(buckets)), "end": str(max(buckets))},
            )
        )

        if vals:
            max_val, max_row = max(vals, key=lambda p: p[0])
            min_val, min_row = min(vals, key=lambda p: p[0])
            numeric_vals = [float(v) for v, _ in vals]
            avg_val = round(sum(numeric_vals) / len(numeric_vals), 4)

            grp_max = list(max_row[1:-1]) if group_by else []
            grp_min = list(min_row[1:-1]) if group_by else []

            facts.append(
                Fact(
                    text=f"Максимум: {max_val} (бакет {max_row[0]}"
                    + (f", группа {grp_max}" if group_by else "")
                    + ").",
                    kind="peak",
                    values={"value": float(max_val), "bucket": str(max_row[0]), "group": grp_max},
                )
            )
            facts.append(
                Fact(
                    text=f"Минимум: {min_val} (бакет {min_row[0]}"
                    + (f", группа {grp_min}" if group_by else "")
                    + ").",
                    kind="trough",
                    values={"value": float(min_val), "bucket": str(min_row[0]), "group": grp_min},
                )
            )
            facts.append(
                Fact(
                    text=f"Среднее по бакетам: {avg_val}.",
                    kind="average",
                    values={"value": avg_val},
                )
            )

        chart_hint = ChartHint(
            type="line",
            x="time_bucket",
            y=["value"],
            color=group_by[0] if len(group_by) == 1 else None,
        )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="time_aggregate",
            params={
                "table": table,
                "time_col": time_col,
                "measure": measure,
                "agg": agg,
                "freq": freq,
                "group_by": group_by,
                "filter": filter,
                "top_n": top_n,
            },
            data=data,
            facts=facts,
            chart_hint=chart_hint,
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# FIND DUPLICATES
# ============================================================================


def find_duplicates(
    table: str,
    columns: list[str] | None = None,  # ← было list[str]
    *,
    filter: str | None = None,
    include_all_columns: bool = False,
    limit: int = 100,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Находит группы дублей. Без columns — ищет полные дубли строк."""
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="limit должен быть > 0.",
                )
            )

        all_cols = _list_columns(con, table)
        if not all_cols:
            return _err(
                error=ToolError(
                    code="invalid_table",
                    message=f"Таблица '{table}' не найдена или пуста.",
                )
            )

        # ← КЛЮЧЕВОЕ: если columns не задан — ищем полные дубли по всем колонкам
        if not columns:
            columns = all_cols
            include_all_columns = False  # все колонки уже в ключе, дублировать нечего
        else:
            for col in columns:
                if col not in all_cols:
                    return _err(
                        error=ToolError(
                            code="invalid_column",
                            message=f"Колонка '{col}' не найдена.",
                            suggestion=f"Доступно: {', '.join(all_cols)}",
                        )
                    )
            # если пользователь включил include_all_columns, но columns == all_cols —
            # прочих колонок нет, флаг игнорируется
            if set(columns) >= set(all_cols):
                include_all_columns = False

        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        safe_table = _quote_id(table)
        safe_cols = [_quote_id(c) for c in columns]
        other_cols = [c for c in all_cols if c not in columns] if include_all_columns else []
        safe_other = [_quote_id(c) for c in other_cols]

        effective_limit = min(limit, get_limits().sql_max_rows)

        select_parts = safe_cols + ["COUNT(*) AS duplicate_count"]
        if other_cols:
            select_parts += [f"FIRST({c}) AS {c}" for c in safe_other]

        group_positions = ", ".join(str(i) for i in range(1, len(columns) + 1))
        query = (
            f"SELECT {', '.join(select_parts)} FROM {safe_table} "
            f"WHERE 1=1{filter_sql} "
            f"GROUP BY {group_positions} "
            f"HAVING COUNT(*) > 1 "
            f"ORDER BY duplicate_count DESC "
            f"LIMIT {effective_limit + 1}"
        )

        result = execute(query).fetchall()
        truncated = len(result) > effective_limit
        result = result[:effective_limit]

        # Полная статистика (без учёта limit)
        total_rows = execute(f"SELECT COUNT(*) FROM {safe_table} WHERE 1=1{filter_sql}").fetchone()[
            0
        ]
        group_by_cols = ", ".join(safe_cols)
        dup_total = execute(
            f"SELECT COALESCE(SUM(cnt), 0) FROM ("
            f"  SELECT COUNT(*) AS cnt FROM {safe_table} "
            f"  WHERE 1=1{filter_sql} GROUP BY {group_by_cols} "
            f"  HAVING COUNT(*) > 1"
            f")"
        ).fetchone()[0]
        total_duplicate_groups = execute(
            f"SELECT COUNT(*) FROM ("
            f"  SELECT 1 FROM {safe_table} "
            f"  WHERE 1=1{filter_sql} GROUP BY {group_by_cols} "
            f"  HAVING COUNT(*) > 1"
            f")"
        ).fetchone()[0]

        out_columns = list(columns) + ["duplicate_count"] + other_cols
        out_rows = [list(r) for r in result]
        row_count = len(out_rows)

        if row_count == 0:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="find_duplicates",
                params={
                    "table": table,
                    "columns": columns,
                    "filter": filter,
                    "include_all_columns": include_all_columns,
                    "limit": limit,
                },
                data=ToolResultData(
                    columns=out_columns,
                    rows=[],
                    row_count=0,
                    truncated=False,
                ),
                facts=[
                    Fact(
                        text=(
                            f"Проверено строк: {total_rows}, найдено групп дублей: "
                            f"{int(total_duplicate_groups)}"
                            + (
                                f" (показано: {row_count})"
                                if row_count < total_duplicate_groups
                                else ""
                            )
                            + "."
                        ),
                        kind="duplicate_summary",
                        values={
                            "total_rows": total_rows,
                            "duplicate_groups": int(total_duplicate_groups),
                            "shown_groups": row_count,
                        },
                    ),
                ],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        max_group = max(r[len(columns)] for r in result)
        extra_rows = int(dup_total) - int(total_duplicate_groups)
        pct = round(dup_total / total_rows * 100, 2) if total_rows else 0.0

        facts: list[Fact] = [
            Fact(
                text=(
                    f"Проверено строк: {total_rows}, найдено групп дублей: "
                    f"{int(total_duplicate_groups)}"
                    + (f" (показано: {row_count})" if row_count < total_duplicate_groups else "")
                    + "."
                ),
                kind="duplicate_summary",
                values={
                    "total_rows": total_rows,
                    "duplicate_groups": int(total_duplicate_groups),
                    "shown_groups": row_count,
                },
            ),
            Fact(
                text=f"Всего строк в группах дублей: {dup_total} ({pct}%), "
                f"из них «лишних»: {extra_rows}.",
                kind="duplicate_volume",
                values={"duplicate_rows": int(dup_total), "extra_rows": extra_rows, "percent": pct},
            ),
            Fact(
                text=f"Максимальный размер группы: {max_group}.",
                kind="duplicate_peak",
                values={"max_group_size": max_group},
            ),
        ]
        for r in result[:5]:
            key_vals = r[: len(columns)]
            cnt = r[len(columns)]
            key_str = ", ".join(f"{c}={v}" for c, v in zip(columns, key_vals, strict=False))
            facts.append(
                Fact(
                    text=f"Комбинация ({key_str}) встречается {cnt} раз.",
                    kind="duplicate_group",
                    values={"key": dict(zip(columns, key_vals, strict=False)), "count": cnt},
                )
            )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="find_duplicates",
            params={
                "table": table,
                "columns": columns,
                "filter": filter,
                "include_all_columns": include_all_columns,
                "limit": limit,
            },
            data=ToolResultData(
                columns=out_columns,
                rows=out_rows,
                row_count=row_count,
                truncated=truncated,
            ),
            facts=facts,
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# DETECT ANOMALIES
# ============================================================================
def _detect_stl_residual_anomalies(
    con: duckdb.DuckDBPyConnection,
    table: str,
    time_col: str,
    measure: str,
    freq: str,
    threshold: float,
    group_by: list[str],
    filter_sql: str,
    limit: int,
    allowed_tables: Collection[str] | None = None,
) -> tuple[list[dict], bool, str] | ToolError:
    """STL anomalies and truncation, or an explicit error for an unusable series."""
    import numpy as np
    import pandas as pd
    from statsmodels.tsa.seasonal import STL

    period = _STL_PERIOD_BY_FREQ.get(freq, 7)

    safe_table = _quote_id(table)
    safe_time = _quote_id(time_col)
    safe_measure = _quote_id(measure)
    safe_groups = [_quote_id(g) for g in group_by]

    select_parts = [
        f"DATE_TRUNC('{freq}', CAST({safe_time} AS TIMESTAMP)) AS time_bucket",
        *safe_groups,
        f"SUM({safe_measure}) AS value",
    ]
    n_group = 1 + len(group_by)
    group_positions = ", ".join(str(i) for i in range(1, n_group + 1))

    query = (
        f"SELECT {', '.join(select_parts)} FROM {safe_table} "
        f"WHERE {safe_time} IS NOT NULL{filter_sql} "
        f"GROUP BY {group_positions} "
        f"ORDER BY {group_positions} ASC"
    )

    execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)
    raw = execute(query).fetchall()
    if not raw:
        return [], False, query

    df = pd.DataFrame(raw, columns=["time_bucket", *group_by, "value"])
    df["value"] = df["value"].astype(float)
    # Check every group before fitting: one unknown bucket invalidates a claim
    # about the full result, even if other groups have usable observations.
    if df["value"].isna().any():
        return ToolError(
            code="missing_data",
            message="В ряду есть неизвестные значения. Для STL нужен полный числовой ряд.",
            suggestion="Проверьте пропуски или выберите непрерывный период.",
        )
    if not np.isfinite(df["value"].to_numpy()).all():
        return ToolError(code="invalid_data", message="Ряд содержит бесконечные значения.")

    anomalies: list[dict] = []

    def _process(sub: "pd.DataFrame") -> None:
        if len(sub) < max(period * 2, 4):
            return
        try:
            series = sub["value"].to_numpy(dtype=float)
            check_budget()
            # Work in units of variation, removing a constant level before STL.
            # Both the fit and its roundoff tolerance are invariant to metric units.
            centered = series - np.median(series)
            spread = float(np.max(np.abs(centered)))
            if spread == 0:
                return
            res = STL(centered / spread, period=period, robust=True).fit()
            check_budget()
            resid = res.resid
            resid_s = pd.Series(resid)
            med = float(resid_s.median())
            mad = float((resid_s - med).abs().median())
            scale = 1.4826 * mad if mad > 0 else float(resid_s.std())
            # Only normalized floating-point roundoff is suppressed, not real
            # residuals hidden by a large baseline or very small measurement unit.
            noise_floor = np.finfo(float).eps * 128 * len(series)
            scale = max(scale, noise_floor)
            if not scale:
                return
            z = (resid - med) / scale
            for i, zi in enumerate(z):
                if abs(zi) > threshold:
                    row = sub.iloc[i]
                    item = {
                        "time_bucket": str(row["time_bucket"]),
                        "value": float(row["value"]),
                        "residual": float(resid[i] * spread),
                        "z_score": float(zi),
                        "direction": "above" if zi > 0 else "below",
                    }
                    for g in group_by:
                        item[g] = _to_python(row[g])
                    anomalies.append(item)
        except (TurnCancelled, TurnTimeout):
            raise
        except Exception:
            return

    if group_by:
        for _, sub in df.groupby(group_by, dropna=False):
            _process(sub)
    else:
        _process(df)

    anomalies.sort(key=lambda a: abs(a["z_score"]), reverse=True)
    was_truncated = len(anomalies) > limit
    return anomalies[:limit], was_truncated, query


def detect_anomalies(
    table: str,
    measure: str,  # ← было column
    *,
    method: str = "iqr",
    time_col: str | None = None,  # ← НОВОЕ
    freq: str = "day",  # ← НОВОЕ (только для stl-residual)
    threshold: float | None = None,
    group_by: list[str] | None = None,
    filter: str | None = None,
    id_column: str | None = None,
    limit: int = 100,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Аномалии в числовой колонке: iqr / zscore / mad / stl-residual.

    - iqr, zscore, mad — точечные (по значениям строк).
    - stl-residual — по временному ряду (нужен time_col),
      возвращает аномальные бакеты и их z-score по остаткам STL.
    """
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)
        # ---------- валидация ----------
        if method not in _ANOMALY_METHODS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимый метод: '{method}'.",
                    suggestion=f"Допустимо: {sorted(_ANOMALY_METHODS)}",
                )
            )
        if method == "stl-residual" and time_col is None:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="Для метода 'stl-residual' обязателен time_col.",
                    suggestion="Укажите time_col — колонку даты/времени.",
                )
            )
        if method == "stl-residual" and freq not in _FREQ_UNITS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая частота: '{freq}'.",
                    suggestion=f"Допустимо: {sorted(_FREQ_UNITS)}",
                )
            )
        if method == "stl-residual" and _STL_PERIOD_BY_FREQ.get(freq, 7) < 2:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=(
                        f"STL не поддерживает freq='{freq}' "
                        f"(период {_STL_PERIOD_BY_FREQ.get(freq, 7)} < 2)."
                    ),
                    suggestion=(
                        "Для столь редких данных используйте freq='quarter' "
                        "или точечные методы (iqr/zscore/mad)."
                    ),
                )
            )
        if threshold is None:
            threshold = _DEFAULT_THRESHOLDS[method]
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="threshold должен быть положительным числом.",
                )
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="limit должен быть > 0.",
                )
            )

        group_by = group_by or []
        all_cols = _list_columns(con, table)
        if not all_cols:
            return _err(
                error=ToolError(
                    code="invalid_table",
                    message=f"Таблица '{table}' не найдена.",
                )
            )
        checked = [measure, *group_by, *([id_column] if id_column else [])]
        if time_col:
            checked.append(time_col)
        for col in checked:
            if col not in all_cols:
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' не найдена.",
                        suggestion=f"Доступно: {', '.join(all_cols)}",
                    )
                )
        dtype = (_column_type(con, table, measure) or "").upper()
        if not any(t in dtype for t in _NUMERIC_TYPES):
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка '{measure}' должна быть числовой (тип: {dtype}).",
                )
            )

        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        # ==========================================================
        # ВЕТКА stl-residual — отдельная, работает по временному ряду
        # ==========================================================
        if method == "stl-residual":
            try:
                stl_result = _detect_stl_residual_anomalies(
                    con,
                    table,
                    time_col,
                    measure,
                    freq,
                    threshold,
                    group_by,
                    filter_sql,
                    min(limit, get_limits().sql_max_rows),
                    allowed_tables=allowed_tables,
                )
            except ImportError:
                return _err(
                    error=ToolError(
                        code="method_unavailable",
                        message="Метод 'stl-residual' требует statsmodels.",
                        suggestion="Установите statsmodels или используйте method='iqr'.",
                    )
                )

            if isinstance(stl_result, ToolError):
                return _err(error=stl_result)
            anomalies, was_truncated, query = stl_result
            out_columns = ["time_bucket", *group_by, "value", "residual", "z_score", "direction"]
            params_dict = {
                "table": table,
                "measure": measure,
                "method": method,
                "time_col": time_col,
                "freq": freq,
                "threshold": threshold,
                "group_by": group_by,
                "filter": filter,
                "id_column": id_column,
                "limit": limit,
            }

            if not anomalies:
                return ToolResult(
                    result_id=f"r_{uuid.uuid4().hex}",
                    status="empty",
                    tool="detect_anomalies",
                    params=params_dict,
                    data=ToolResultData(
                        columns=out_columns,
                        rows=[],
                        row_count=0,
                        truncated=False,
                    ),
                    facts=[
                        Fact(
                            text=f"Аномалий методом stl-residual "
                            f"(частота {freq}, порог {threshold}) не обнаружено.",
                            kind="empty_result",
                        )
                    ],
                    sql=query,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

            rows = [[a.get(c) for c in out_columns] for a in anomalies]
            above = sum(1 for a in anomalies if a["direction"] == "above")
            below = len(anomalies) - above
            max_z = max(abs(a["z_score"]) for a in anomalies)

            facts: list[Fact] = [
                Fact(
                    text=f"STL-residual: найдено аномальных бакетов {len(anomalies)} "
                    f"(выше тренда/сезонности: {above}, ниже: {below}).",
                    kind="anomaly_summary",
                    values={"total": len(anomalies), "above": above, "below": below},
                ),
                Fact(
                    text=f"Метод: stl-residual, частота: {freq}, порог |z| > {threshold}.",
                    kind="anomaly_method",
                    values={"method": method, "freq": freq, "threshold": threshold},
                ),
                Fact(
                    text=f"Максимальный |z-score| остатка: {round(max_z, 2)}.",
                    kind="anomaly_peak",
                    values={"max_abs_z": max_z},
                ),
            ]
            for a in anomalies[:5]:
                grp_part = f", группа { {g: a[g] for g in group_by} }" if group_by else ""
                facts.append(
                    Fact(
                        text=f"Аномальный бакет {a['time_bucket']}{grp_part}: "
                        f"значение {a['value']}, остаток {round(a['residual'], 2)}, "
                        f"z = {round(a['z_score'], 2)} ({a['direction']}).",
                        kind="anomaly",
                        values={
                            "time_bucket": str(a["time_bucket"]),
                            "value": a["value"],
                            "residual": a["residual"],
                            "z_score": a["z_score"],
                            "direction": a["direction"],
                            "group": {g: a[g] for g in group_by},
                        },
                    )
                )

            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="ok",
                tool="detect_anomalies",
                params=params_dict,
                data=ToolResultData(
                    columns=out_columns,
                    rows=rows,
                    row_count=len(rows),
                    truncated=was_truncated,
                ),
                facts=facts,
                chart_hint=ChartHint(
                    type="line",
                    x="time_bucket",
                    y=["value"],
                    color=group_by[0] if len(group_by) == 1 else None,
                ),
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        # ==========================================================
        # ВЕТКА точечных методов (iqr / zscore / mad) — как было,
        # только column → measure
        # ==========================================================
        safe_table = _quote_id(table)
        safe_col = _quote_id(measure)
        safe_groups = [_quote_id(g) for g in group_by]
        safe_id = _quote_id(id_column) if id_column else None
        group_list = ", ".join(safe_groups)
        group_list_prefix = f"{group_list}, " if group_by else ""
        # IS NOT DISTINCT FROM сохраняет группы с NULL в ключе
        join_on = (
            " AND ".join(f"t.{g} IS NOT DISTINCT FROM s.{g}" for g in safe_groups)
            if group_by
            else None
        )

        base_cols = ([safe_id] if safe_id else []) + [safe_col] + safe_groups
        seen: set[str] = set()
        base_select = ", ".join(c for c in base_cols if not (c in seen or seen.add(c)))

        base_cte = (
            f"base AS (SELECT {base_select} FROM {safe_table} "
            f"WHERE {safe_col} IS NOT NULL{filter_sql})"
        )

        if method == "iqr":
            stats_cte = (
                (
                    f"stats AS (SELECT {group_list_prefix}"
                    f"quantile_cont({safe_col}, 0.25) AS q1, "
                    f"quantile_cont({safe_col}, 0.75) AS q3 "
                    f"FROM base GROUP BY {group_list or 'ALL'})"
                )
                if group_by
                else (
                    f"stats AS (SELECT quantile_cont({safe_col}, 0.25) AS q1, "
                    f"quantile_cont({safe_col}, 0.75) AS q3 FROM base)"
                )
            )
            join_stats = f"JOIN stats s ON {join_on}" if group_by else ", stats s"
            lower_expr = f"s.q1 - {threshold} * (s.q3 - s.q1)"
            upper_expr = f"s.q3 + {threshold} * (s.q3 - s.q1)"

        elif method == "zscore":
            stats_cte = (
                (
                    f"stats AS (SELECT {group_list_prefix}"
                    f"AVG({safe_col}) AS mean, STDDEV_POP({safe_col}) AS std "
                    f"FROM base GROUP BY {group_list or 'ALL'})"
                )
                if group_by
                else (
                    f"stats AS (SELECT AVG({safe_col}) AS mean, "
                    f"STDDEV_POP({safe_col}) AS std FROM base)"
                )
            )
            join_stats = f"JOIN stats s ON {join_on}" if group_by else ", stats s"
            lower_expr = f"s.mean - {threshold} * s.std"
            upper_expr = f"s.mean + {threshold} * s.std"

        else:  # mad
            med_cte = (
                (
                    f"med AS (SELECT {group_list_prefix}"
                    f"MEDIAN({safe_col}) AS median FROM base "
                    f"GROUP BY {group_list or 'ALL'})"
                )
                if group_by
                else (f"med AS (SELECT MEDIAN({safe_col}) AS median FROM base)")
            )
            join_med_in = (
                (
                    "JOIN med m ON "
                    + " AND ".join(f"t.{g} IS NOT DISTINCT FROM m.{g}" for g in safe_groups)
                )
                if group_by
                else ", med m"
            )
            group_list_qualified = ", ".join(f"t.{g}" for g in safe_groups)
            mad_cte = (
                (
                    f"mad AS (SELECT {group_list_qualified}, "
                    f"MEDIAN(ABS(t.{safe_col} - m.median)) AS mad "
                    f"FROM base t {join_med_in} "
                    f"GROUP BY {group_list_qualified})"
                )
                if group_by
                else (
                    f"mad AS (SELECT MEDIAN(ABS(t.{safe_col} - m.median)) AS mad "
                    f"FROM base t, med m)"
                )
            )
            stats_cte = f"{med_cte}, {mad_cte}"
            join_stats = (
                (
                    "JOIN med m2 ON "
                    + " AND ".join(f"t.{g} IS NOT DISTINCT FROM m2.{g}" for g in safe_groups)
                    + " JOIN mad s ON "
                    + " AND ".join(f"t.{g} IS NOT DISTINCT FROM s.{g}" for g in safe_groups)
                )
                if group_by
                else ", med m2, mad s"
            )
            lower_expr = f"m2.median - {threshold} * 1.4826 * s.mad"
            upper_expr = f"m2.median + {threshold} * 1.4826 * s.mad"

        direction_expr = f"CASE WHEN t.{safe_col} > ({upper_expr}) THEN 'above' ELSE 'below' END"
        deviation_expr = (
            f"CASE WHEN t.{safe_col} > ({upper_expr}) "
            f"THEN t.{safe_col} - ({upper_expr}) "
            f"ELSE ({lower_expr}) - t.{safe_col} END"
        )

        final_cols = []
        if safe_id:
            final_cols.append(f"t.{safe_id} AS {safe_id}")
        final_cols.append(f"t.{safe_col} AS {safe_col}")
        for g in safe_groups:
            final_cols.append(f"t.{g} AS {g}")
        final_cols += [
            f"{direction_expr} AS direction",
            f"({deviation_expr}) AS deviation",
            f"({lower_expr}) AS lower_bound",
            f"({upper_expr}) AS upper_bound",
        ]

        effective_limit = min(limit, get_limits().sql_max_rows)
        query = (
            f"WITH {base_cte}, {stats_cte} "
            f"SELECT {', '.join(final_cols)} "
            f"FROM base t {join_stats} "
            f"WHERE t.{safe_col} < ({lower_expr}) "
            f"   OR t.{safe_col} > ({upper_expr}) "
            f"ORDER BY deviation DESC "
            f"LIMIT {effective_limit + 1}"
        )

        result = execute(query).fetchall()
        truncated = len(result) > effective_limit
        result = result[:effective_limit]
        row_count = len(result)

        out_columns: list[str] = []
        if id_column:
            out_columns.append(id_column)
        out_columns.append(measure)  # ← было column
        out_columns += list(group_by)
        out_columns += ["direction", "deviation", "lower_bound", "upper_bound"]

        params_dict = {
            "table": table,
            "measure": measure,
            "method": method,
            "time_col": time_col,
            "freq": freq,
            "threshold": threshold,
            "group_by": group_by,
            "filter": filter,
            "id_column": id_column,
            "limit": limit,
        }

        if row_count == 0:
            stats_row = execute(
                f"SELECT COUNT(*), COUNT(DISTINCT {safe_col}) "
                f"FROM {safe_table} "
                f"WHERE {safe_col} IS NOT NULL{filter_sql}"
            ).fetchone()
            total_vals, distinct_vals = stats_row
            if distinct_vals <= 1:
                msg = (
                    f"Все значения колонки '{measure}' одинаковы "
                    f"({distinct_vals} уникальных) — аномалий нет."
                )
            else:
                msg = (
                    f"Аномалий методом {method} с порогом {threshold} "
                    f"не обнаружено (проверено значений: {total_vals})."
                )
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="detect_anomalies",
                params=params_dict,
                data=ToolResultData(
                    columns=out_columns,
                    rows=[],
                    row_count=0,
                    truncated=False,
                ),
                facts=[Fact(text=msg, kind="empty_result")],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        rows_as_dicts = [dict(zip(out_columns, r, strict=False)) for r in result]
        directions = [d["direction"] for d in rows_as_dicts]
        above = sum(1 for d in directions if d == "above")
        below = row_count - above
        max_dev = max(abs(d["deviation"]) for d in rows_as_dicts)

        facts = [
            Fact(
                text=f"Найдено аномалий: {row_count} (выше нормы: {above}, ниже нормы: {below}).",
                kind="anomaly_summary",
                values={"total": row_count, "above": above, "below": below},
            ),
            Fact(
                text=f"Метод: {method}, порог: {threshold}.",
                kind="anomaly_method",
                values={"method": method, "threshold": threshold},
            ),
            Fact(
                text=f"Максимальное отклонение от границы: {max_dev}.",
                kind="anomaly_peak",
                values={"max_deviation": float(max_dev)},
            ),
        ]
        for d in rows_as_dicts[:5]:
            grp = {g: d[g] for g in group_by} if group_by else {}
            id_part = f"id={d[id_column]}, " if id_column else ""
            grp_part = f", группа {grp}" if group_by else ""
            facts.append(
                Fact(
                    text=f"Аномалия ({id_part}{measure}={d[measure]}{grp_part}): "
                    f"{d['direction']} нормы, отклонение {d['deviation']} "
                    f"(границы [{d['lower_bound']}; {d['upper_bound']}]).",
                    kind="anomaly",
                    values={
                        "id": d.get(id_column) if id_column else None,
                        "value": float(d[measure]),
                        "direction": d["direction"],
                        "deviation": float(d["deviation"]),
                        "lower_bound": float(d["lower_bound"]),
                        "upper_bound": float(d["upper_bound"]),
                        "group": grp,
                    },
                )
            )

        chart_hint = (
            ChartHint(type="bar", x=id_column or measure, y=[measure]) if id_column else None
        )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="detect_anomalies",
            params=params_dict,
            data=ToolResultData(
                columns=out_columns,
                rows=[list(r) for r in result],
                row_count=row_count,
                truncated=truncated,
            ),
            facts=facts,
            chart_hint=chart_hint,
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# SEASONALITY
# ============================================================================


def seasonality(
    table: str,
    time_col: str,
    *,
    measure: str | None = None,
    agg: str = "sum",
    freq: str = "month",
    period: int | None = None,
    filter: str | None = None,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Ищет сезонность STL-декомпозицией.

    Возвращает силу сезонности/тренда, пиковую и минимальную фазы,
    амплитуду. Требует минимум 2*period наблюдений.
    """
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)
        if freq not in _FREQ_UNITS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая частота: '{freq}'.",
                    suggestion=f"Допустимо: {sorted(_FREQ_UNITS)}",
                )
            )
        if agg not in _AGG_FUNCS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая агрегация: '{agg}'.",
                    suggestion=f"Допустимо: {sorted(_AGG_FUNCS)}",
                )
            )
        if agg != "count" and measure is None:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Для agg='{agg}' обязателен measure.",
                    suggestion="Укажите measure или используйте agg='count'.",
                )
            )
        if period is None:
            period = _STL_PERIOD_BY_FREQ[freq]
        if not isinstance(period, int) or isinstance(period, bool) or period < 2:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="period должен быть целым числом >= 2.",
                )
            )

        if _column_type(con, table, time_col) is None:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка времени '{time_col}' не найдена.",
                    suggestion=f"Доступно: {', '.join(_list_columns(con, table))}",
                )
            )
        if measure is not None and _column_type(con, table, measure) is None:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка метрики '{measure}' не найдена.",
                )
            )

        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        try:
            import pandas as pd
            from statsmodels.tsa.seasonal import STL
        except ImportError:
            return _err(
                error=ToolError(
                    code="method_unavailable",
                    message="Для seasonality нужны statsmodels и pandas.",
                    suggestion="Установите statsmodels.",
                )
            )

        safe_table = _quote_id(table)
        safe_time = _quote_id(time_col)
        if agg == "count":
            agg_expr = "COUNT(*)" if measure is None else f"COUNT({_quote_id(measure)})"
        else:
            agg_expr = f"{_AGG_FUNCS[agg]}({_quote_id(measure)})"

        query = (
            f"SELECT DATE_TRUNC('{freq}', CAST({safe_time} AS TIMESTAMP)) AS bucket, "
            f"{agg_expr} AS value "
            f"FROM {safe_table} "
            f"WHERE {safe_time} IS NOT NULL{filter_sql} "
            f"GROUP BY 1 ORDER BY 1 ASC"
        )
        raw = execute(query).fetchall()

        if len(raw) < 2 * period:
            return _err(
                error=ToolError(
                    code="insufficient_data",
                    message=(
                        f"Для сезонности с period={period} нужно >= {2 * period} "
                        f"наблюдений, получено {len(raw)}."
                    ),
                    suggestion="Уменьшите period, расширьте диапазон или увеличьте частоту.",
                )
            )

        df = pd.DataFrame(raw, columns=["bucket", "value"])
        df["bucket"] = pd.to_datetime(df["bucket"])
        df = df.set_index("bucket").sort_index()

        full_idx = pd.date_range(
            df.index.min(),
            df.index.max(),
            freq=_PANDAS_FREQ[freq],
        )
        gaps = len(full_idx) - len(df)
        # An observed NULL is unknown, not an absent sum/count bucket.
        # Do not invent zero-valued means, medians or other missing statistics.
        if df["value"].isna().any() or (gaps > 0 and agg not in {"sum", "count"}):
            return _err(
                error=ToolError(
                    code="missing_data",
                    message=(
                        "В ряду есть неизвестные значения. "
                        "Для сезонности нужен полный числовой ряд."
                    ),
                    suggestion="Проверьте пропуски или выберите непрерывный период.",
                )
            )
        df = df.reindex(full_idx, fill_value=0.0)
        series = df["value"].astype(float)
        if not np.isfinite(series.to_numpy()).all():
            return _err(
                error=ToolError(
                    code="invalid_data", message="Ряд содержит нечисловые или бесконечные значения."
                )
            )
        check_budget()

        level = float(np.median(series.to_numpy()))
        centered = series.to_numpy() - level
        spread = float(np.max(np.abs(centered)))
        if spread == 0.0:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="seasonality",
                params={
                    "table": table,
                    "time_col": time_col,
                    "measure": measure,
                    "agg": agg,
                    "freq": freq,
                    "period": period,
                    "filter": filter,
                },
                facts=[
                    Fact(
                        text="Ряд постоянный (std=0) — сезонность не выявлена.",
                        kind="empty_result",
                    )
                ],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            # Fit in units of variation; a large baseline or small metric unit
            # must not change the significance of floating-point roundoff.
            res = STL(centered / spread, period=period, robust=True).fit()
            check_budget()
        except (TurnCancelled, TurnTimeout):
            raise
        except Exception as e:
            return _err(
                error=ToolError(
                    code="db_error",
                    message=f"Ошибка STL-декомпозиции: {e}",
                )
            )

        trend = np.asarray(res.trend, dtype=float)
        seasonal = np.asarray(res.seasonal, dtype=float)
        resid = np.asarray(res.resid, dtype=float)
        if not all(np.isfinite(values).all() for values in (trend, seasonal, resid)):
            return _err(
                error=ToolError(
                    code="invalid_data", message="STL не вернул конечные числовые значения."
                )
            )

        var_r = float(np.var(resid))
        var_sr = float(np.var(seasonal + resid))
        var_tr = float(np.var(trend + resid))
        noise_variance = (np.finfo(float).eps * 128 * len(series)) ** 2
        strength_s = max(0.0, 1.0 - var_r / var_sr) if var_sr > noise_variance else 0.0
        strength_t = max(0.0, 1.0 - var_r / var_tr) if var_tr > noise_variance else 0.0

        # Strengths are dimensionless; publish the decomposition in source units.
        trend = trend * spread + level
        seasonal = seasonal * spread
        resid = resid * spread

        usable = len(seasonal) - len(seasonal) % period
        phase_means = seasonal[:usable].reshape(-1, period).mean(axis=0)
        peak_phase = int(np.argmax(phase_means))
        trough_phase = int(np.argmin(phase_means))
        amplitude = float(phase_means[peak_phase] - phase_means[trough_phase])

        rows = [
            [
                ts.isoformat() if freq in {"hour", "minute"} else ts.strftime("%Y-%m-%d"),
                float(v),
                float(t),
                float(s),
                float(r),
            ]
            for ts, v, t, s, r in zip(df.index, series, trend, seasonal, resid, strict=False)
        ]

        phase_label = ""
        if freq == "month" and period == 12:
            phase_label = f" ({_RU_MONTHS[df.index[peak_phase].month - 1]})"
        elif freq == "day" and period == 7:
            phase_label = f" ({_RU_WEEKDAYS[df.index[peak_phase].weekday()]})"

        interp = "сильная" if strength_s >= 0.6 else "умеренная" if strength_s >= 0.3 else "слабая"

        facts = [
            Fact(
                text=f"Сезонность с периодом {period} (частота {freq}). "
                f"Сила сезонности: {round(strength_s, 3)}, "
                f"сила тренда: {round(strength_t, 3)}.",
                kind="seasonal_summary",
                values={
                    "period": period,
                    "freq": freq,
                    "strength_seasonal": strength_s,
                    "strength_trend": strength_t,
                },
            ),
            Fact(
                text=f"Пиковая фаза: {peak_phase}{phase_label}, "
                f"амплитуда сезонности {round(amplitude, 4)}.",
                kind="seasonal_peak",
                values={"peak_phase": peak_phase, "amplitude": amplitude},
            ),
            Fact(
                text=f"Минимальная фаза: {trough_phase} "
                f"(значение {round(float(phase_means[trough_phase]), 4)}).",
                kind="seasonal_trough",
                values={"trough_phase": trough_phase, "value": float(phase_means[trough_phase])},
            ),
            Fact(
                text=f"Интерпретация: сезонность {interp} (порог 0.6 — сильная, 0.3 — умеренная).",
                kind="interpretation",
                values={"strength": strength_s},
            ),
        ]
        if gaps > 0:
            facts.append(
                Fact(
                    text=f"В ряду {gaps} пропущенных бакетов — заполнены нулями.",
                    kind="note",
                    values={"gaps": gaps},
                )
            )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="seasonality",
            params={
                "table": table,
                "time_col": time_col,
                "measure": measure,
                "agg": agg,
                "freq": freq,
                "period": period,
                "filter": filter,
            },
            data=ToolResultData(
                columns=["time_bucket", "value", "trend", "seasonal", "resid"],
                rows=rows[: get_limits().sql_max_rows],
                row_count=min(len(rows), get_limits().sql_max_rows),
                truncated=len(rows) > get_limits().sql_max_rows,
            ),
            facts=facts,
            chart_hint=ChartHint(
                type="line",
                x="time_bucket",
                y=["value", "trend", "seasonal"],
            ),
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# SEGMENT PROFILE
# ============================================================================


def segment_profile(
    table: str,
    group_by: list[str],
    *,
    measures: list[str] | None = None,
    filter: str | None = None,
    top_n: int = 100,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Сводка по группам: размер + mean/median/min/max/std по measures."""
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)
        if not group_by:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="Параметр group_by не может быть пустым.",
                    suggestion="Укажите хотя бы одну колонку группировки.",
                )
            )
        if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="top_n должен быть > 0.",
                )
            )

        all_cols = _list_columns(con, table)
        if not all_cols:
            return _err(
                error=ToolError(
                    code="invalid_table",
                    message=f"Таблица '{table}' не найдена.",
                )
            )
        for col in [*group_by, *(measures or [])]:
            if col not in all_cols:
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' не найдена.",
                        suggestion=f"Доступно: {', '.join(all_cols)}",
                    )
                )

        measures = measures or []
        for m in measures:
            dtype = (_column_type(con, table, m) or "").upper()
            if not any(t in dtype for t in _NUMERIC_TYPES):
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка measure '{m}' должна быть числовой (тип: {dtype}).",
                    )
                )

        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        safe_table = _quote_id(table)
        safe_groups = [_quote_id(g) for g in group_by]

        out_columns = list(group_by)
        used_columns = {name.casefold() for name in out_columns}

        def output_alias(base: str) -> str:
            name = base
            suffix = 1
            while name.casefold() in used_columns:
                name = f"{base}_{suffix}"
                suffix += 1
            used_columns.add(name.casefold())
            out_columns.append(name)
            return name

        size_column = output_alias("segment_size")
        measure_columns = []
        select_parts = safe_groups + [f"COUNT(*) AS {_quote_id(size_column)}"]
        for m in measures:
            safe_m = _quote_id(m)
            aliases = [
                output_alias(f"{m}_{stat}") for stat in ("mean", "median", "min", "max", "std")
            ]
            measure_columns.append(aliases)
            select_parts.extend(
                f"{function}({safe_m}) AS {_quote_id(alias)}"
                for function, alias in zip(
                    ("AVG", "MEDIAN", "MIN", "MAX", "STDDEV_POP"), aliases, strict=True
                )
            )

        effective_limit = min(top_n, get_limits().sql_max_rows)
        n_group = len(group_by)
        group_positions = ", ".join(str(i) for i in range(1, n_group + 1))

        base_query = (
            f"SELECT {', '.join(select_parts)} FROM {safe_table} "
            f"WHERE 1=1{filter_sql} GROUP BY {group_positions}"
        )
        query = f"{base_query} ORDER BY {_quote_id(size_column)} DESC LIMIT {effective_limit + 1}"
        summary_columns = ["COUNT(*)", f"COALESCE(SUM({_quote_id(size_column)}), 0)"]
        for aliases in measure_columns:
            mean_alias = _quote_id(aliases[0])
            summary_columns.extend([f"MAX({mean_alias})", f"MIN({mean_alias})"])
        summary = execute(
            f"SELECT {', '.join(summary_columns)} FROM ({base_query}) segments"
        ).fetchone()
        raw = execute(query).fetchall()
        truncated = len(raw) > effective_limit
        raw = raw[:effective_limit]

        params_dict = {
            "table": table,
            "group_by": group_by,
            "measures": measures,
            "filter": filter,
            "top_n": top_n,
        }

        if not raw:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="segment_profile",
                params=params_dict,
                data=ToolResultData(
                    columns=out_columns,
                    rows=[],
                    row_count=0,
                    truncated=False,
                ),
                facts=[
                    Fact(
                        text="По заданным условиям нет ни одного сегмента.",
                        kind="empty_result",
                    )
                ],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        rows = [list(r) for r in raw]
        total_rows = int(summary[1])
        segment_count = int(summary[0])

        facts: list[Fact] = [
            Fact(
                text=f"Всего сегментов: {segment_count}, строк в них: {total_rows}.",
                kind="segment_summary",
                values={"segment_count": segment_count, "total_rows": total_rows},
            ),
        ]

        for r in rows[:3]:
            group_vals = {g: r[i] for i, g in enumerate(group_by)}
            parts = [f"Сегмент {group_vals}: {r[n_group]} строк"]
            for i, m in enumerate(measures):
                mv = r[n_group + 1 + i * 5]
                if mv is not None:
                    parts.append(f"{m} mean={round(float(mv), 4)}")
            facts.append(
                Fact(
                    text=", ".join(parts) + ".",
                    kind="segment_sample",
                    values={
                        "group": group_vals,
                        "size": r[n_group],
                        "measures": {
                            m: {
                                "mean": r[n_group + 1 + i * 5],
                                "median": r[n_group + 2 + i * 5],
                                "min": r[n_group + 3 + i * 5],
                                "max": r[n_group + 4 + i * 5],
                                "std": r[n_group + 5 + i * 5],
                            }
                            for i, m in enumerate(measures)
                        },
                    },
                )
            )

        for i, m in enumerate(measures):
            max_mean, min_mean = summary[2 + 2 * i : 4 + 2 * i]
            if max_mean is None:
                continue
            facts.append(
                Fact(
                    text=(
                        f"{m}: по всем сегментам максимум среднего {round(float(max_mean), 4)}, "
                        f"минимум {round(float(min_mean), 4)}."
                    ),
                    kind="segment_measure",
                    values={"measure": m, "max_mean": float(max_mean), "min_mean": float(min_mean)},
                )
            )

        chart_hint = None
        if len(group_by) == 1:
            chart_hint = ChartHint(
                type="bar",
                x=group_by[0],
                y=[measure_columns[0][0]] if measures else [size_column],
            )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="segment_profile",
            params=params_dict,
            data=ToolResultData(
                columns=out_columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
            ),
            facts=facts,
            chart_hint=chart_hint,
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# COMPARE SEGMENTS
# ============================================================================


def compare_segments(
    table: str,
    segment_col: str,
    a: str,
    b: str,
    *,
    measures: list[str],
    filter: str | None = None,
    alpha: float = 0.05,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Сравнивает группы a и b: t-test + Cohen's d (числовые) / χ² + Cramér's V."""
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)
        if not measures:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="Укажите хотя бы одну колонку в measures.",
                )
            )
        if not isinstance(alpha, (int, float)) or isinstance(alpha, bool) or not (0 < alpha < 1):
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="alpha должен быть числом в интервале (0, 1).",
                )
            )

        all_cols = _list_columns(con, table)
        if not all_cols:
            return _err(
                error=ToolError(
                    code="invalid_table",
                    message=f"Таблица '{table}' не найдена.",
                )
            )
        for col in [segment_col, *measures]:
            if col not in all_cols:
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' не найдена.",
                        suggestion=f"Доступно: {', '.join(all_cols)}",
                    )
                )

        safe_table = _quote_id(table)
        safe_seg = _quote_id(segment_col)
        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        count_row = execute(
            f"SELECT COUNT(*) FILTER (WHERE {safe_seg} = ?), "
            f"COUNT(*) FILTER (WHERE {safe_seg} = ?), COUNT(DISTINCT {safe_seg}) "
            f"FROM {safe_table} WHERE {safe_seg} IN (?, ?){filter_sql}",
            [a, b, a, b],
        ).fetchone()
        counts = {a: count_row[0], b: count_row[1]}
        if not count_row[0] or not count_row[1]:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Сегмент '{a if not count_row[0] else b}' отсутствует после фильтра.",
                )
            )
        if count_row[2] < 2:
            return _err(
                error=ToolError(code="invalid_parameter", message="Выберите два разных сегмента.")
            )

        try:
            from scipy import stats
        except ImportError:
            return _err(
                error=ToolError(
                    code="method_unavailable",
                    message="Для compare_segments нужен scipy.",
                    suggestion="Установите scipy.",
                )
            )

        facts: list[Fact] = [
            Fact(
                text=f"Сравнение '{a}' (n={counts[a]}) и '{b}' (n={counts[b]}) "
                f"по {len(measures)} признакам, α={alpha}.",
                kind="compare_summary",
                values={"a": a, "b": b, "n_a": counts[a], "n_b": counts[b], "alpha": alpha},
            ),
        ]
        rows: list[list] = []

        for measure in measures:
            safe_m = _quote_id(measure)
            dtype = (_column_type(con, table, measure) or "").upper()
            is_numeric = any(t in dtype for t in _NUMERIC_TYPES)

            if is_numeric:
                a_vals = [
                    float(r[0])
                    for r in execute(
                        f"SELECT {safe_m} FROM {safe_table} "
                        f"WHERE {safe_seg} = ? AND {safe_m} IS NOT NULL{filter_sql}",
                        [a],
                    ).fetchall()
                ]
                b_vals = [
                    float(r[0])
                    for r in execute(
                        f"SELECT {safe_m} FROM {safe_table} "
                        f"WHERE {safe_seg} = ? AND {safe_m} IS NOT NULL{filter_sql}",
                        [b],
                    ).fetchall()
                ]
                if len(a_vals) < 2 or len(b_vals) < 2:
                    facts.append(
                        Fact(
                            text=f"{measure}: недостаточно данных "
                            f"(A={len(a_vals)}, B={len(b_vals)}).",
                            kind="compare_skip",
                            values={"measure": measure},
                        )
                    )
                    rows.append(
                        [
                            measure,
                            "numeric",
                            len(a_vals),
                            len(b_vals),
                            None,
                            None,
                            None,
                            None,
                            False,
                            "insufficient_data",
                        ]
                    )
                    continue

                mean_a = float(np.mean(a_vals))
                mean_b = float(np.mean(b_vals))
                var_a = float(np.var(a_vals, ddof=1))
                var_b = float(np.var(b_vals, ddof=1))
                n_a, n_b = len(a_vals), len(b_vals)
                pooled = (
                    float(np.sqrt(((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)))
                    if (n_a + n_b > 2)
                    else 0.0
                )
                p_val = None
                if pooled > 0:
                    check_budget()
                    _, probability = stats.ttest_ind(a_vals, b_vals, equal_var=False)
                    p_val = float(probability) if np.isfinite(probability) else None
                d = (mean_a - mean_b) / pooled if pooled > 0 else None
                effect_name = "cohens_d" if d is not None else "undefined_effect"
                p_val = (
                    p_val if d is not None and p_val is not None and np.isfinite(p_val) else None
                )
                significant = p_val is not None and p_val < alpha
                verdict = (
                    "вывод недоступен"
                    if p_val is None
                    else "значимо"
                    if significant
                    else "не значимо"
                )
                effect_text = (
                    f"Cohen's d={round(d, 3)} ({_cohens_d_label(d)})"
                    if d is not None
                    else "t-test и размер эффекта не определены при нулевой совместной дисперсии"
                )
                check_budget()

                facts.append(
                    Fact(
                        text=(
                            f"{measure}: mean(A)={round(mean_a, 4)}, "
                            f"mean(B)={round(mean_b, 4)}, "
                            f"{effect_text}, "
                            f"p={round(p_val, 6) if p_val is not None else '—'} — "
                            f"{verdict}."
                        ),
                        kind="compare_numeric",
                        values={
                            "measure": measure,
                            "mean_a": mean_a,
                            "mean_b": mean_b,
                            "cohens_d": d,
                            "p_value": p_val,
                            "significant": significant,
                        },
                    )
                )
                rows.append(
                    [
                        measure,
                        "numeric",
                        n_a,
                        n_b,
                        mean_a,
                        mean_b,
                        d,
                        p_val,
                        significant,
                        effect_name,
                    ]
                )

            else:
                a_counts = {
                    k: int(v)
                    for k, v in execute(
                        f"SELECT {safe_m}, COUNT(*) FROM {safe_table} "
                        f"WHERE {safe_seg} = ?{filter_sql} GROUP BY 1",
                        [a],
                    ).fetchall()
                }
                b_counts = {
                    k: int(v)
                    for k, v in execute(
                        f"SELECT {safe_m}, COUNT(*) FROM {safe_table} "
                        f"WHERE {safe_seg} = ?{filter_sql} GROUP BY 1",
                        [b],
                    ).fetchall()
                }
                categories = list(dict.fromkeys([*a_counts, *b_counts]))
                cont = [
                    [a_counts.get(c, 0) for c in categories],
                    [b_counts.get(c, 0) for c in categories],
                ]
                n_a_tot = sum(cont[0])
                n_b_tot = sum(cont[1])
                if n_a_tot < 5 or n_b_tot < 5 or len(categories) < 2:
                    facts.append(
                        Fact(
                            text=f"{measure}: недостаточно категорий/данных для χ².",
                            kind="compare_skip",
                            values={"measure": measure},
                        )
                    )
                    rows.append(
                        [
                            measure,
                            "categorical",
                            n_a_tot,
                            n_b_tot,
                            None,
                            None,
                            None,
                            None,
                            False,
                            "insufficient_data",
                        ]
                    )
                    continue
                try:
                    chi2, p_val, _, expected = stats.chi2_contingency(cont)
                    if np.any(expected < 5):
                        rows.append(
                            [
                                measure,
                                "categorical",
                                n_a_tot,
                                n_b_tot,
                                None,
                                None,
                                None,
                                None,
                                False,
                                "insufficient_data",
                            ]
                        )
                        facts.append(
                            Fact(
                                text=f"{measure}: ожидаемые частоты меньше 5; χ² неприменим.",
                                kind="compare_skip",
                                values={"measure": measure},
                            )
                        )
                        continue
                    check_budget()
                    p_val = float(p_val)
                    v = float(np.sqrt(chi2 / (n_a_tot + n_b_tot)))
                except (TurnCancelled, TurnTimeout):
                    raise
                except Exception:
                    p_val = None
                    v = None

                significant = p_val is not None and p_val < alpha
                verdict = (
                    "вывод недоступен"
                    if p_val is None
                    else "значимо"
                    if significant
                    else "не значимо"
                )
                top_a = max(a_counts, key=a_counts.get) if a_counts else None
                top_b = max(b_counts, key=b_counts.get) if b_counts else None

                facts.append(
                    Fact(
                        text=(
                            f"{measure}: категорий {len(categories)}, "
                            f"топ в A='{top_a}', топ в B='{top_b}', "
                            f"Cramér's V={round(v, 3) if v is not None else '—'}, "
                            f"p={round(p_val, 6) if p_val is not None else '—'} — "
                            f"{verdict}."
                        ),
                        kind="compare_categorical",
                        values={
                            "measure": measure,
                            "top_a": top_a,
                            "top_b": top_b,
                            "cramers_v": v,
                            "p_value": p_val,
                            "significant": significant,
                        },
                    )
                )
                rows.append(
                    [
                        measure,
                        "categorical",
                        n_a_tot,
                        n_b_tot,
                        top_a,
                        top_b,
                        v,
                        p_val,
                        significant,
                        "cramers_v",
                    ]
                )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="compare_segments",
            params={
                "table": table,
                "segment_col": segment_col,
                "a": a,
                "b": b,
                "measures": measures,
                "filter": filter,
                "alpha": alpha,
            },
            data=ToolResultData(
                columns=[
                    "measure",
                    "kind",
                    "n_a",
                    "n_b",
                    "stat_a",
                    "stat_b",
                    "effect_size",
                    "p_value",
                    "significant",
                    "effect_size_name",
                ],
                rows=rows[: get_limits().sql_max_rows],
                row_count=min(len(rows), get_limits().sql_max_rows),
                truncated=len(rows) > get_limits().sql_max_rows,
            ),
            facts=facts,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# CORRELATION
# ============================================================================

_CORRELATION_METHODS = {"pearson", "spearman", "kendall"}
_FREQ_UNIT_RU = {
    "minute": "мин.",
    "hour": "ч.",
    "day": "дн.",
    "week": "нед.",
    "month": "мес.",
    "quarter": "кв.",
    "year": "г.",
}


def _is_numeric_type(dtype: str) -> bool:
    return any(t in dtype.upper() for t in _NUMERIC_TYPES)


def _corr_coefficient(x: np.ndarray, y: np.ndarray, method: str) -> tuple[float, float, int]:
    """Pearson/Spearman/Kendall на парах без NaN. Возвращает (r, p, n)."""
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]
    n = int(x.size)
    if n < 3 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan"), float("nan"), n
    from scipy import stats

    if method == "pearson":
        r, p = stats.pearsonr(x, y)
    elif method == "spearman":
        r, p = stats.spearmanr(x, y)
    else:
        r, p = stats.kendalltau(x, y)
    return float(r), float(p), n


def _strength_label(r: float) -> str:
    a = abs(r)
    if math.isnan(a):
        return "не определена"
    if a < 0.1:
        return "связи не обнаружено"
    if a < 0.3:
        return "слабая"
    if a < 0.5:
        return "умеренная"
    if a < 0.7:
        return "заметная"
    return "сильная"


def _direction_label(r: float) -> str:
    if math.isnan(r):
        return ""
    if r > 0:
        return "положительная"
    if r < 0:
        return "отрицательная"
    return "нулевая"


def correlation(
    table: str,
    columns: list[str],
    *,
    method: str = "pearson",
    filter: str | None = None,
    min_n: int = 10,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Попарные корреляции между числовыми колонками (B2-6)."""
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    params_dict = {
        "table": table,
        "columns": columns,
        "method": method,
        "filter": filter,
        "min_n": min_n,
    }
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)

        if method not in _CORRELATION_METHODS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимый метод корреляции: '{method}'.",
                    suggestion=f"Допустимо: {sorted(_CORRELATION_METHODS)}",
                )
            )
        if not isinstance(min_n, int) or isinstance(min_n, bool) or min_n < 3:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="min_n должен быть целым >= 3.",
                )
            )
        if not columns or len(columns) < 2:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="Укажите минимум две колонки.",
                )
            )
        all_cols = _list_columns(con, table)
        if not all_cols:
            return _err(
                error=ToolError(
                    code="invalid_table",
                    message=f"Таблица '{table}' не найдена.",
                )
            )
        for col in columns:
            if col not in all_cols:
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' не найдена.",
                        suggestion=f"Доступно: {', '.join(all_cols)}",
                    )
                )
            dtype = (_column_type(con, table, col) or "").upper()
            if not _is_numeric_type(dtype):
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' должна быть числовой (тип: {dtype}).",
                    )
                )
        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        safe_table = _quote_id(table)
        safe_cols = [_quote_id(c) for c in columns]
        query = f"SELECT {', '.join(safe_cols)} FROM {safe_table} WHERE 1=1{filter_sql}"
        raw = execute(query).fetchall()

        if not raw:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="correlation",
                params=params_dict,
                data=ToolResultData(
                    columns=["from", "to", "r", "p_value", "n"],
                    rows=[],
                    row_count=0,
                    truncated=False,
                ),
                facts=[
                    Fact(
                        text="По заданным условиям нет строк для расчёта корреляции.",
                        kind="empty_result",
                    )
                ],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        arr = np.array(
            [[float(v) if v is not None else np.nan for v in row] for row in raw],
            dtype=float,
        )
        index = {c: i for i, c in enumerate(columns)}
        rows: list[list] = []
        facts: list[Fact] = []
        for i, c1 in enumerate(columns):
            for c2 in columns[i + 1 :]:
                check_budget()
                r, p, n = _corr_coefficient(arr[:, i], arr[:, index[c2]], method)
                rows.append([c1, c2, r, p, n])
                if n < min_n:
                    facts.append(
                        Fact(
                            text=(
                                f"Для пары {c1}–{c2} только {n} наблюдений "
                                f"(< {min_n}); оценка ненадёжна."
                            ),
                            kind="warning",
                            values={"a": c1, "b": c2, "n": n},
                        )
                    )
                else:
                    facts.append(
                        Fact(
                            text=(
                                f"{method.capitalize()} r({c1}, {c2}) = {round(r, 2)}, "
                                f"n = {n}: {_direction_label(r)} {_strength_label(r)} связь."
                            ),
                            kind="metric",
                            values={"a": c1, "b": c2, "r": r, "p": p, "n": n},
                        )
                    )
        facts.append(
            Fact(
                text=("Корреляция показывает статистическую связь, а не доказанную причинность."),
                kind="caveat",
                values={},
            )
        )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="correlation",
            params=params_dict,
            data=ToolResultData(
                columns=["from", "to", "r", "p_value", "n"],
                rows=rows,
                row_count=len(rows),
                truncated=False,
            ),
            facts=facts,
            chart_hint=ChartHint(type="heatmap", x="from", y=["to"]),
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# LAGGED CORRELATION
# ============================================================================


def _estimate_calendar_buckets(start, end, freq: str) -> int:
    """Оценка сверху числа бакетов между start и end для выбранной частоты."""
    delta_seconds = (end - start).total_seconds()
    unit = _FREQ_MIN_SECONDS[freq]
    return int(delta_seconds // unit) + 1


def _agg_expression(agg: str, column: str) -> str:
    if agg == "count":
        return f"COUNT({_quote_id(column)})"
    return f"{_AGG_FUNCS[agg]}({_quote_id(column)})"


def lagged_correlation(
    table: str,
    time_col: str,
    a: str,
    b: str,
    *,
    freq: str = "day",
    agg: str = "sum",
    max_lag: int = 14,
    min_n: int = 10,
    method: str = "pearson",
    filter: str | None = None,
    include_granger: bool = False,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Кросс-корреляция a и b по лагам (B2-6).

    Конвенция: lag=k — сравниваем a(t) и b(t+k). Положительный лаг — a опережает b.
    """
    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    params_dict = {
        "table": table,
        "time_col": time_col,
        "a": a,
        "b": b,
        "freq": freq,
        "agg": agg,
        "max_lag": max_lag,
        "min_n": min_n,
        "method": method,
        "filter": filter,
        "include_granger": include_granger,
    }
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)

        if freq not in _FREQ_UNITS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая частота: '{freq}'.",
                    suggestion=f"Допустимо: {sorted(_FREQ_UNITS)}",
                )
            )
        if agg not in _AGG_FUNCS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая агрегация: '{agg}'.",
                    suggestion=f"Допустимо: {sorted(_AGG_FUNCS)}",
                )
            )
        if method not in ("pearson", "spearman"):
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Для лагов допустимы pearson и spearman, получено '{method}'.",
                )
            )
        if not isinstance(max_lag, int) or isinstance(max_lag, bool) or max_lag < 0:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="max_lag должен быть целым >= 0.",
                )
            )
        if not isinstance(min_n, int) or isinstance(min_n, bool) or min_n < 3:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="min_n должен быть целым >= 3.",
                )
            )
        all_cols = _list_columns(con, table)
        if not all_cols:
            return _err(
                error=ToolError(
                    code="invalid_table",
                    message=f"Таблица '{table}' не найдена.",
                )
            )
        for col in (time_col, a, b):
            if col not in all_cols:
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' не найдена.",
                        suggestion=f"Доступно: {', '.join(all_cols)}",
                    )
                )
        for col in (a, b):
            dtype = (_column_type(con, table, col) or "").upper()
            if not _is_numeric_type(dtype):
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{col}' должна быть числовой (тип: {dtype}).",
                    )
                )
        time_dtype = (_column_type(con, table, time_col) or "").upper()
        if "DATE" not in time_dtype and "TIMESTAMP" not in time_dtype:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка времени '{time_col}' должна быть DATE или TIMESTAMP "
                    f"(тип: {time_dtype}).",
                )
            )
        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        safe_table = _quote_id(table)
        safe_time = _quote_id(time_col)
        query = (
            f"SELECT DATE_TRUNC('{freq}', CAST({safe_time} AS TIMESTAMP)) AS time_bucket, "
            f"{_agg_expression(agg, a)} AS a, {_agg_expression(agg, b)} AS b "
            f"FROM {safe_table} "
            f"WHERE {safe_time} IS NOT NULL{filter_sql} "
            f"GROUP BY 1 ORDER BY 1"
        )
        raw = execute(query).fetchall()

        if not raw:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="lagged_correlation",
                params=params_dict,
                data=ToolResultData(
                    columns=["lag", "r", "p_value", "n"],
                    rows=[],
                    row_count=0,
                    truncated=False,
                ),
                facts=[
                    Fact(
                        text="По заданным условиям нет временных бакетов.",
                        kind="empty_result",
                    )
                ],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        import pandas as pd

        df = pd.DataFrame(raw, columns=["time_bucket", "a", "b"])
        df["time_bucket"] = pd.to_datetime(df["time_bucket"])
        df = df.set_index("time_bucket").sort_index()
        # Защита от раздувания календарной сетки до аллокации: одна ошибочная
        # дата (например, 1970-01-01 рядом с 2026-10-26) при freq='minute'
        # даёт десятки миллионов бакетов до первой CPU-проверки бюджета.
        n_buckets = _estimate_calendar_buckets(df.index.min(), df.index.max(), freq)
        if n_buckets > _MAX_CALENDAR_BUCKETS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=(
                        f"Календарная сетка частоты '{freq}' между "
                        f"{df.index.min().isoformat()} и {df.index.max().isoformat()} "
                        f"содержит около {n_buckets} бакетов "
                        f"(лимит {_MAX_CALENDAR_BUCKETS})."
                    ),
                    suggestion=(
                        "Укрупните частоту (например, 'day' вместо 'minute'), "
                        "сузьте диапазон фильтром или проверьте колонку времени — "
                        "возможно, в данных есть ошибочная дата вне основного периода."
                    ),
                )
            )
        df = df.reindex(pd.date_range(df.index.min(), df.index.max(), freq=_PANDAS_FREQ[freq]))
        # sum/count — отсутствующий бакет = 0; остальные — неизвестно (NaN)
        if agg in ("sum", "count"):
            df = df.fillna({"a": 0.0, "b": 0.0})

        lags = list(range(-max_lag, max_lag + 1))
        a_vals = df["a"].to_numpy(dtype=float)
        rows: list[list] = []
        best = {"lag": None, "r": float("nan"), "p": float("nan"), "n": 0}
        for k in lags:
            check_budget()
            # lag=k: a(t) vs b(t+k) => b.shift(-k)
            shifted = df["b"].shift(-k).to_numpy(dtype=float)
            r, p, n = _corr_coefficient(a_vals, shifted, method)
            rows.append([k, r, p, n])
            if n >= min_n and not math.isnan(r):
                if best["lag"] is None or abs(r) > abs(best["r"]):
                    best = {"lag": k, "r": r, "p": p, "n": n}

        facts: list[Fact] = []
        if best["lag"] is None:
            facts.append(
                Fact(
                    text=(
                        f"Не удалось оценить лаг: слишком мало наблюдений "
                        f"(< {min_n} на всех сдвигах)."
                    ),
                    kind="empty_result",
                )
            )
        else:
            k = best["lag"]
            unit = _FREQ_UNIT_RU[freq]
            if k == 0:
                rel = "одновременно"
            elif k > 0:
                rel = f"изменения {a} опережают {b} на {abs(k)} {unit}"
            else:
                rel = f"изменения {b} опережают {a} на {abs(k)} {unit}"
            facts.append(
                Fact(
                    text=(
                        f"Максимальная корреляция {round(best['r'], 2)} при сдвиге {k} "
                        f"({rel}); n = {best['n']}. Это статистическая связь, "
                        f"не доказательство причинности."
                    ),
                    kind="metric",
                    values={
                        "a": a,
                        "b": b,
                        "lag": k,
                        "r": best["r"],
                        "p": best["p"],
                        "n": best["n"],
                    },
                )
            )
            facts.append(
                Fact(
                    text=(
                        f"Лучший лаг выбран как максимум по {2 * max_lag + 1} сдвигам — "
                        f"возможна ложная находка; проверьте на отложенном периоде "
                        f"и скорректируйте p-value."
                    ),
                    kind="caveat",
                    values={"multiple_tests": 2 * max_lag + 1},
                )
            )

        if include_granger:
            if len(df) < 30:
                facts.append(
                    Fact(
                        text="Тест Грейнджера не рассчитан: нужно минимум 30 наблюдений.",
                        kind="warning",
                        values={},
                    )
                )
            elif df[["a", "b"]].isna().any().any():
                # Granger определён на регулярной сетке. При agg != sum/count
                # отсутствующие бакеты остаются NaN, и dropna() сжал бы ряд:
                # «лаг 1» в сжатом ряду соответствовал бы 2 дням, max_lag
                # перестал бы ограничивать календарный сдвиг. Отказываемся
                # явно вместо того, чтобы молча публиковать неверные единицы.
                missing = int(df[["a", "b"]].isna().any(axis=1).sum())
                facts.append(
                    Fact(
                        text=(
                            f"Тест Грейнджера не рассчитан: в ряду {missing} "
                            f"пропущенных бакетов, а Granger требует регулярной "
                            f"сетки. Используйте agg='sum' или agg='count', "
                            f"либо сузьте диапазон до непрерывного периода."
                        ),
                        kind="warning",
                        values={"n": len(df), "missing_buckets": missing},
                    )
                )
            else:
                try:
                    check_budget()
                    import warnings

                    from statsmodels.tsa.stattools import grangercausalitytests

                    # df уже на полной календарной сетке (reindex выше),
                    # NaN нет — dropna() не нужен и только сжал бы периоды.
                    x = df[["b", "a"]].to_numpy(dtype=float)
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        g = grangercausalitytests(x, maxlag=min(max_lag, 10))

                    pvals: dict[int, float] = {}
                    for lag, (results, _) in g.items():
                        ftest = results.get("ssr_ftest")
                        if ftest is None:
                            continue
                        p = float(ftest[1])
                        if not math.isnan(p):
                            pvals[lag] = p
                    if not pvals:
                        raise ValueError("все p-value нечисловые")

                    best_g = min(pvals, key=pvals.get)
                    facts.append(
                        Fact(
                            text=(
                                f"Тест Грейнджера: p = {round(pvals[best_g], 3)} "
                                f"на лаге {best_g} для {a}→{b}. Это потенциальная "
                                f"предсказуемость, а не причинность; на "
                                f"нестационарных рядах ненадёжно."
                            ),
                            kind="granger",
                            values={"p": pvals[best_g], "lag": best_g},
                        )
                    )
                except (TurnCancelled, TurnTimeout):
                    raise
                except Exception as e:  # noqa: BLE001
                    facts.append(
                        Fact(
                            text=f"Тест Грейнджера не рассчитан: {type(e).__name__}: {e}",
                            kind="warning",
                            values={"error": type(e).__name__, "message": str(e)},
                        )
                    )
        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="lagged_correlation",
            params=params_dict,
            data=ToolResultData(
                columns=["lag", "r", "p_value", "n"],
                rows=rows,
                row_count=len(rows),
                truncated=False,
            ),
            facts=facts,
            chart_hint=ChartHint(type="bar", x="lag", y=["r"]),
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()


# ============================================================================
# FORECAST
# ============================================================================

_FORECAST_METHODS = {"ets", "arima", "naive"}


def _forecast_ets(y: np.ndarray, horizon: int, period: int | None, alpha: float):
    """Holt-Winters additive. Интервалы — аналитическая дисперсия ETS (Hyndman 8.7).

    Для ETS(A,A,N)/ETS(A,A,A): Var(e_{t+h}) = σ²·(1 + Σ_{j=1}^{h-1} c_j²),
    c_j = α + j·β + γ·1_{j ≡ 0 mod m}. sqrt(h) — это случайное блуждание,
    оно завышает интервалы в разы на почти-постоянных рядах.
    """
    import warnings

    from scipy import stats
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    check_budget()
    n = len(y)
    use_seasonal = period is not None and period >= 2 and n >= 2 * period
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = ExponentialSmoothing(
            y,
            trend="add",
            seasonal="add" if use_seasonal else None,
            seasonal_periods=period if use_seasonal else None,
        )
        fit = model.fit()
    check_budget()

    point = np.asarray(fit.forecast(horizon), dtype=float)

    alpha_s = float(fit.params.get("smoothing_level", 0.0))
    beta_s = float(fit.params.get("smoothing_trend", 0.0))
    gamma_s = float(fit.params.get("smoothing_seasonal", 0.0)) if use_seasonal else 0.0

    resid = np.asarray(fit.resid, dtype=float)
    resid = resid[~np.isnan(resid)]
    sigma = float(np.std(resid, ddof=1)) if len(resid) >= 2 else 0.0

    # c_j для j=1..h-1; для h=1 c пуст, множитель 1
    c = np.zeros(max(horizon - 1, 0))
    for j in range(1, horizon):
        c[j - 1] = alpha_s + j * beta_s
        if use_seasonal and period and j % period == 0:
            c[j - 1] += gamma_s
    cum_sq = np.cumsum(c**2) if len(c) > 0 else np.array([])
    multipliers = np.array([1.0] + [float(np.sqrt(1 + cum_sq[k])) for k in range(horizon - 1)])
    z = float(stats.norm.ppf(1 - alpha / 2))
    half = z * sigma * multipliers
    return point, point - half, point + half


def _forecast_arima(y: np.ndarray, horizon: int, period: int | None, alpha: float):
    """ARIMA(1,1,1); SARIMAX — только при достаточной длине и с fallback."""
    import warnings

    from statsmodels.tsa.arima.model import ARIMA as _ARIMA

    check_budget()
    n = len(y)
    # 4 полных сезона — минимум, ниже которого SARIMAX нестабилен.
    use_seasonal = period is not None and period >= 2 and n >= 4 * period
    if use_seasonal:
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                fit = SARIMAX(
                    y,
                    order=(1, 1, 1),
                    seasonal_order=(1, 1, 1, period),
                    enforce_stationarity=False,
                    enforce_invertibility=False,
                ).fit(disp=False)
                if not fit.mle_retvals.get("converged", True):
                    raise RuntimeError("SARIMAX не сошёлся")
            except (TurnCancelled, TurnTimeout):
                raise
            except Exception:  # noqa: BLE001
                use_seasonal = False
        check_budget()
    if not use_seasonal:
        check_budget()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = _ARIMA(y, order=(1, 1, 1)).fit()
        check_budget()

    fc = fit.get_forecast(steps=horizon)
    point = np.asarray(fc.predicted_mean, dtype=float)
    ci = np.asarray(fc.conf_int(alpha=alpha), dtype=float)
    return point, ci[:, 0], ci[:, 1]


def _forecast_naive(y: np.ndarray, horizon: int, period: int | None, alpha: float):
    """Сезонный naive: интервал растёт по числу пройденных сезонов, не по h.

    Множитель: sqrt(1 + floor((h-1)/p)); для p=1 сводится к sqrt(h).
    """
    import numpy as np
    from scipy import stats

    check_budget()
    p = period if period and period >= 1 else 1
    if len(y) <= p:
        raise RuntimeError(f"для naive нужно > {p} наблюдений")
    resid = y[p:] - y[:-p]
    resid = resid[~np.isnan(resid)]
    std = float(np.std(resid, ddof=1)) if len(resid) >= 2 else 0.0
    point = np.array([y[-p + (h % p)] for h in range(horizon)], dtype=float)
    z = float(stats.norm.ppf(1 - alpha / 2))
    steps = np.arange(1, horizon + 1, dtype=float)
    multipliers = np.sqrt(1 + np.floor((steps - 1) / p))
    half = z * std * multipliers
    return point, point - half, point + half


def _dispatch_forecast(method: str, y: np.ndarray, horizon: int, period, alpha):
    if method == "ets":
        return _forecast_ets(y, horizon, period, alpha)
    if method == "arima":
        return _forecast_arima(y, horizon, period, alpha)
    if method == "naive":
        return _forecast_naive(y, horizon, period, alpha)
    raise ValueError(f"Неизвестный метод: {method}")


def _forecast_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
    import numpy as np

    a = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(p))
    a, p = a[mask], p[mask]
    if len(a) == 0:
        return {"mae": None, "rmse": None, "mape": None, "n_holdout": 0}
    mae = float(np.mean(np.abs(a - p)))
    rmse = float(np.sqrt(np.mean((a - p) ** 2)))
    mape = float(np.mean(np.abs((a - p) / a)) * 100) if np.all(a != 0) else None
    return {"mae": mae, "rmse": rmse, "mape": mape, "n_holdout": int(len(a))}


def forecast(
    table: str,
    time_col: str,
    *,
    horizon: int,
    measure: str | None = None,
    agg: str = "sum",
    freq: str = "day",
    period: int | None = None,
    method: str = "ets",
    alpha: float = 0.05,
    holdout: int | None = None,
    filter: str | None = None,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Прогноз показателя на горизонт с интервалами (B2-7).

    Методы: ``ets`` (Holt-Winters), ``arima`` (ARIMA/SARIMAX), ``naive`` (сезонный naive).
    ``alpha`` задаёт интервал: 0.05 → 95%. ``holdout`` — число последних точек
    для валидации на отложенном периоде (MAE/RMSE/MAPE попадают в ``facts``).
    """
    import pandas as pd

    started = time.perf_counter()
    con = connection if connection is not None else _get_mock_connection()
    timer = query_timer(get_limits().tool_timeout_seconds, con.interrupt)
    timer.start()
    params_dict = {
        "table": table,
        "time_col": time_col,
        "measure": measure,
        "agg": agg,
        "freq": freq,
        "horizon": horizon,
        "period": period,
        "method": method,
        "alpha": alpha,
        "holdout": holdout,
        "filter": filter,
    }
    try:
        check_dataset_table(table, allowed_tables)
        execute = partial(execute_dataset_query, con, allowed_tables=allowed_tables)

        # --- валидация параметров ---
        if freq not in _FREQ_UNITS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая частота: '{freq}'.",
                    suggestion=f"Допустимо: {sorted(_FREQ_UNITS)}",
                )
            )
        if agg not in _AGG_FUNCS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимая агрегация: '{agg}'.",
                    suggestion=f"Допустимо: {sorted(_AGG_FUNCS)}",
                )
            )
        if agg != "count" and measure is None:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Для agg='{agg}' обязателен measure.",
                    suggestion="Укажите measure или agg='count'.",
                )
            )
        if method not in _FORECAST_METHODS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=f"Недопустимый метод: '{method}'.",
                    suggestion=f"Допустимо: {sorted(_FORECAST_METHODS)}",
                )
            )
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="horizon должен быть >= 1.",
                )
            )
        if not isinstance(alpha, (int, float)) or isinstance(alpha, bool) or not 0 < alpha < 1:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="alpha должен быть числом в интервале (0, 1).",
                )
            )
        if holdout is not None and (
            not isinstance(holdout, int) or isinstance(holdout, bool) or holdout < 1
        ):
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="holdout должен быть >= 1.",
                )
            )
        if period is not None and (
            not isinstance(period, int) or isinstance(period, bool) or period < 1
        ):
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message="period должен быть целым >= 1.",
                )
            )
        if period is None:
            default = _STL_PERIOD_BY_FREQ.get(freq)
            period = default if default and default >= 1 else None

        # --- проверка колонок ---
        all_cols = _list_columns(con, table)
        if not all_cols:
            return _err(
                error=ToolError(
                    code="invalid_table",
                    message=f"Таблица '{table}' не найдена.",
                )
            )
        if time_col not in all_cols:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка времени '{time_col}' не найдена.",
                    suggestion=f"Доступно: {', '.join(all_cols)}",
                )
            )
        if measure is not None and measure not in all_cols:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка метрики '{measure}' не найдена.",
                )
            )
        time_dtype = (_column_type(con, table, time_col) or "").upper()
        if "DATE" not in time_dtype and "TIMESTAMP" not in time_dtype:
            return _err(
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка времени '{time_col}' должна быть DATE или TIMESTAMP.",
                )
            )
        if measure is not None:
            dtype = (_column_type(con, table, measure) or "").upper()
            if not any(t in dtype for t in _NUMERIC_TYPES):
                return _err(
                    error=ToolError(
                        code="invalid_column",
                        message=f"Колонка '{measure}' должна быть числовой (тип: {dtype}).",
                    )
                )

        try:
            filter_expr = _parse_filter_sql(filter)
        except ValueError as e:
            return _err(
                error=ToolError(
                    code="invalid_filter",
                    message=f"Некорректный фильтр: {e}",
                )
            )
        filter_sql = f" AND {filter_expr.sql(dialect='duckdb')}" if filter_expr else ""

        # --- сборка ряда ---
        safe_table = _quote_id(table)
        safe_time = _quote_id(time_col)
        if agg == "count":
            agg_expr = "COUNT(*)" if measure is None else f"COUNT({_quote_id(measure)})"
        else:
            agg_expr = f"{_AGG_FUNCS[agg]}({_quote_id(measure)})"
        query = (
            f"SELECT DATE_TRUNC('{freq}', CAST({safe_time} AS TIMESTAMP)) AS time_bucket, "
            f"{agg_expr} AS value "
            f"FROM {safe_table} "
            f"WHERE {safe_time} IS NOT NULL{filter_sql} "
            f"GROUP BY 1 ORDER BY 1"
        )
        raw = execute(query).fetchall()

        if not raw:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex}",
                status="empty",
                tool="forecast",
                params=params_dict,
                data=ToolResultData(
                    columns=["time_bucket", "yhat", "yhat_lower", "yhat_upper"],
                    rows=[],
                    row_count=0,
                    truncated=False,
                ),
                facts=[
                    Fact(
                        text="По заданным условиям нет данных для прогноза.",
                        kind="empty_result",
                    )
                ],
                sql=query,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        df = pd.DataFrame(raw, columns=["time_bucket", "value"])
        df["time_bucket"] = pd.to_datetime(df["time_bucket"])
        df = df.set_index("time_bucket").sort_index()
        # Выравниваем по календарю: пропущенные бакеты восстанавливаем,
        # чтобы дальше работали периоды, а не позиции.
        buckets = _estimate_calendar_buckets(df.index.min(), df.index.max(), freq)
        if buckets > _MAX_CALENDAR_BUCKETS:
            return _err(
                error=ToolError(
                    code="invalid_parameter",
                    message=(
                        f"Календарная сетка прогноза содержит около {buckets} бакетов "
                        f"при лимите {_MAX_CALENDAR_BUCKETS}."
                    ),
                    suggestion="Укрупните частоту или сузьте диапазон дат фильтром.",
                )
            )
        check_budget()
        df = df.reindex(pd.date_range(df.index.min(), df.index.max(), freq=_PANDAS_FREQ[freq]))
        if agg in ("sum", "count"):
            df["value"] = df["value"].fillna(0.0)
        elif df["value"].isna().any():
            return _err(
                error=ToolError(
                    code="missing_data",
                    message=(
                        f"В ряду есть пропущенные бакеты; метод '{method}' требует полный ряд."
                    ),
                    suggestion="Используйте agg='sum' или agg='count'.",
                )
            )
        series = df["value"].astype(float)

        # --- достаточность истории ---
        min_required = max(2 * (period or 1), 5)
        if len(series) < min_required:
            return _err(
                error=ToolError(
                    code="insufficient_data",
                    message=(
                        f"Для прогноза '{method}' нужно >= {min_required} наблюдений "
                        f"(частота {freq}, период {period}), получено {len(series)}."
                    ),
                    suggestion=(
                        "Расширьте диапазон, уменьшите частоту или выберите method='naive'."
                    ),
                )
            )

        facts: list[Fact] = []

        # --- валидация на отложенном периоде ---
        if holdout is not None:
            if holdout >= len(series):
                facts.append(
                    Fact(
                        text=(
                            f"holdout={holdout} больше длины ряда ({len(series)}) — "
                            f"валидация пропущена."
                        ),
                        kind="warning",
                        values={"holdout": holdout, "n": len(series)},
                    )
                )
            elif len(series) - holdout < min_required:
                facts.append(
                    Fact(
                        text=(
                            f"Для валидации при holdout={holdout} обучающая часть "
                            f"({len(series) - holdout}) короче минимума "
                            f"({min_required}) — валидация пропущена."
                        ),
                        kind="warning",
                        values={"holdout": holdout, "train_size": len(series) - holdout},
                    )
                )
            else:
                train = series.iloc[:-holdout].to_numpy()
                actual = series.iloc[-holdout:].to_numpy()
                check_budget()
                try:
                    point_train, _, _ = _dispatch_forecast(
                        method,
                        train,
                        holdout,
                        period,
                        alpha,
                    )
                    metrics = _forecast_metrics(actual, point_train)
                    parts = [
                        f"MAE = {round(metrics['mae'], 4)}",
                        f"RMSE = {round(metrics['rmse'], 4)}",
                    ]
                    if metrics["mape"] is not None:
                        parts.append(f"MAPE = {round(metrics['mape'], 2)}%")
                    facts.append(
                        Fact(
                            text=(
                                f"Валидация на отложенном периоде "
                                f"(последние {holdout} точек): " + ", ".join(parts) + "."
                            ),
                            kind="holdout_metrics",
                            values=metrics,
                        )
                    )
                except (TurnCancelled, TurnTimeout):
                    raise
                except Exception as e:  # noqa: BLE001
                    facts.append(
                        Fact(
                            text=f"Валидация не выполнена: {type(e).__name__}: {e}",
                            kind="warning",
                            values={"error": type(e).__name__, "message": str(e)},
                        )
                    )

        # --- основной прогноз ---
        check_budget()
        try:
            point, lower, upper = _dispatch_forecast(
                method,
                series.to_numpy(),
                horizon,
                period,
                alpha,
            )
        except (TurnCancelled, TurnTimeout):
            raise
        except Exception as e:  # noqa: BLE001
            return _err(
                error=ToolError(
                    code="forecast_failed",
                    message=f"Не удалось построить прогноз: {type(e).__name__}: {e}",
                    suggestion="Попробуйте method='naive' или другую частоту/период.",
                )
            )

        # --- метки прогноза: сдвиг на один шаг после последней точки истории ---
        future_index = pd.date_range(
            start=df.index[-1],
            periods=horizon + 1,
            freq=_PANDAS_FREQ[freq],
        )[1:]

        # --- факты ---
        facts.append(
            Fact(
                text=(
                    f"Прогноз методом '{method}' на {horizon} точек вперёд "
                    f"(частота {freq}"
                    + (f", период {period}" if period else "")
                    + f"), интервал {round((1 - alpha) * 100)}%."
                ),
                kind="forecast_summary",
                values={
                    "method": method,
                    "horizon": horizon,
                    "freq": freq,
                    "period": period,
                    "alpha": alpha,
                    "coverage": 1 - alpha,
                },
            )
        )
        facts.append(
            Fact(
                text=(
                    "Прогноз — статистическая экстраполяция, а не гарантия. "
                    "Интервал отражает неопределённость модели при допущении "
                    "о стабильности прошлых закономерностей."
                ),
                kind="caveat",
                values={},
            )
        )
        facts.append(
            Fact(
                text=(
                    f"Последняя точка горизонта: {round(float(point[-1]), 4)} "
                    f"(интервал [{round(float(lower[-1]), 4)}; "
                    f"{round(float(upper[-1]), 4)}])."
                ),
                kind="forecast_endpoint",
                values={
                    "yhat": float(point[-1]),
                    "yhat_lower": float(lower[-1]),
                    "yhat_upper": float(upper[-1]),
                },
            )
        )

        rows = [
            [
                future_index[i].isoformat()
                if freq in {"hour", "minute"}
                else future_index[i].strftime("%Y-%m-%d"),
                float(point[i]),
                float(lower[i]),
                float(upper[i]),
            ]
            for i in range(horizon)
        ]

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex}",
            status="ok",
            tool="forecast",
            params=params_dict,
            data=ToolResultData(
                columns=["time_bucket", "yhat", "yhat_lower", "yhat_upper"],
                rows=rows,
                row_count=horizon,
                truncated=False,
            ),
            facts=facts,
            chart_hint=ChartHint(
                type="line",
                x="time_bucket",
                y=["yhat", "yhat_lower", "yhat_upper"],
            ),
            sql=query,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TurnCancelled, TurnTimeout):
        raise
    except DatasetScopeError:
        return _err(
            error=ToolError(
                code="dataset_scope_violation",
                message="Запрос выходит за область выбранного датасета.",
            )
        )
    except Exception as e:  # noqa: BLE001
        return _err(error=ToolError(code="db_error", message=str(e)))
    finally:
        timer.cancel()
        if connection is None:
            con.close()
