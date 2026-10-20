import threading
import uuid

import duckdb
import sqlglot
from sqlglot import exp

from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultData,
)


def _quote_id(identifier: str) -> str:
    """Безопасно оборачивает идентификатор в кавычки (защита от инъекций)."""
    return '"' + identifier.replace('"', '""') + '"'


def run_sql(sql: str, max_rows: int = 5000) -> ToolResult:
    """Безопасно выполняет SQL-запрос (только SELECT/CTE) в DuckDB."""
    try:
        # 1. ПАРСИМ SQL
        try:
            parsed = sqlglot.parse_one(sql, read="duckdb")
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

        # 3. ДОБАВЛЯЕМ ЛИМИТ
        limit_node = parsed.args.get("limit")
        rewrite_limit = True
        if limit_node:
            try:
                limit_val = int(limit_node.expression.this)
                if limit_val <= max_rows:
                    rewrite_limit = False
            except Exception:
                pass

        if rewrite_limit:
            parsed = parsed.limit(max_rows)
            sql = parsed.sql(dialect="duckdb")

        # 4. ВЫПОЛНЯЕМ С ТАЙМАУТОМ
        con = duckdb.connect(database=":memory:")
        con.execute("SET enable_external_access = false;")

        con.execute("CREATE TABLE test (id INT, name VARCHAR)")
        con.execute("INSERT INTO test VALUES (1, 'Alice'), (2, 'Bob')")

        timer = threading.Timer(30.0, con.interrupt)
        timer.start()

        try:
            cursor = con.execute(sql)
            result = cursor.fetchmany(max_rows)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
        finally:
            timer.cancel()

        # 5. УПАКОВЫВАЕМ ПОД НОВЫЙ КОНТРАКТ
        rows = [list(row) for row in result]  # tuple -> list для JSON
        row_count = len(rows)

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",
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
    return con


def describe_dataset() -> ToolResult:
    """Возвращает обзор всех таблиц в базе данных."""
    con = _get_mock_connection()
    try:
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        table_names = [t[0] for t in tables]

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

            cols = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ?",
                [t_name],
            ).fetchall()
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
            result_id=f"r_{uuid.uuid4().hex[:8]}",
            status="ok",
            tool="describe_dataset",
            params={},
            data=ToolResultData(
                columns=["table_name", "row_count", "columns"],
                rows=rows,
                row_count=len(rows),
            ),
            facts=facts,
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )


def describe_column(table: str, column: str) -> ToolResult:
    """Возвращает полный профиль колонки: пропуски, уникальные, статистику, частоты."""
    con = _get_mock_connection()
    try:
        # [P1] Проверка через параметры
        col_check = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()

        if not col_check:
            available = con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table],
            ).fetchall()
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
                    kind="frequency",
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
            result_id=f"r_{uuid.uuid4().hex[:8]}",
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


def value_counts(
    table: str,
    column: str,
    top_n: int = 10,
    filter: str | None = None,
) -> ToolResult:
    """Возвращает топ-N самых частых значений в колонке."""
    con = _get_mock_connection()
    try:
        col_check = con.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()
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
                parsed_filter = sqlglot.parse_one(f"SELECT * FROM t WHERE {filter}", read="duckdb")
                where_clause = parsed_filter.args.get("where")
                if where_clause:
                    query += f" WHERE {where_clause.this.sql(dialect='duckdb')}"
            except Exception:
                return ToolResult(
                    result_id=str(uuid.uuid4()),
                    status="error",
                    error=ToolError(code="invalid_filter", message="Некорректный фильтр"),
                )

        query += f" GROUP BY {safe_col} ORDER BY cnt DESC LIMIT {top_n}"

        result = con.execute(query).fetchall()

        if not result:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex[:8]}",
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
                kind="frequency",
                values={"value": val, "count": cnt},
            )
            for val, cnt in result
        ]

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",
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
                rows=rows,
                row_count=len(rows),
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
