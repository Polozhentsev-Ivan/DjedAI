import threading
import uuid

import duckdb
import sqlglot
from sqlglot import exp

from djedai.contracts.tool_result import ToolError, ToolResult


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

        # 3. ДОБАВЛЯЕМ ЛИМИТ (на уровне SQL, чтобы база не считала лишнее)
        # Проверяем, есть ли LIMIT в запросе. Если нет или он больше max_rows — ставим свой.
        limit_node = parsed.args.get("limit")
        rewrite_limit = True
        if limit_node:
            try:
                # Пытаемся вытащить число из LIMIT. Если там LIMIT ALL — выдаст ошибку,
                # и мы перезапишем.
                limit_val = int(limit_node.expression.this)
                if limit_val <= max_rows:
                    rewrite_limit = False  # Если лимит адекватный, оставляем как есть
            except Exception:
                pass  # Если не смогли распарсить, принудительно перезапишем

        if rewrite_limit:
            parsed = parsed.limit(max_rows)
            sql = parsed.sql(dialect="duckdb")

        # 4. ВЫПОЛНЯЕМ С ТАЙМАУТОМ
        con = duckdb.connect(database=":memory:")

        # [P1] БЛОКИРУЕМ ВНЕШНИЕ ИСТОЧНИКИ (чтение файлов, сеть) НА УРОВНЕ ДВИЖКА
        # Это отсекает все обходы, включая read_text, SELECT * FROM '/path', etc.
        con.execute("SET enable_external_access = false;")

        # Создаем тестовые данные
        con.execute("CREATE TABLE test (id INT, name VARCHAR)")
        con.execute("INSERT INTO test VALUES (1, 'Alice'), (2, 'Bob')")

        timer = threading.Timer(30.0, con.interrupt)
        timer.start()

        try:
            cursor = con.execute(sql)
            # [P2] FETCHMANY гарантирует, что в память Python попадет не больше строк, чем max_rows
            result = cursor.fetchmany(max_rows)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
        finally:
            timer.cancel()

        # 5. УПАКОВЫВАЕМ
        data = {
            "columns": columns,
            "rows": result,
            "row_count": len(result),
        }

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",  # [P2] Генерируем уникальный ID
            status="ok",
            tool="run_sql",
            sql=sql,
            data=data,
            facts=[{"text": f"Запрос выполнен успешно, получено строк: {len(result)}"}],
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
                "Запрещено обращаться к внешним файлам или сети."
                " Используйте только загруженные таблицы."
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


# --- НОВЫЕ ФУНКЦИИ ДЛЯ B2-3 ---


def _get_mock_connection():
    """Вспомогательная функция для создания тестовой БД (пока нет реального датасета от DE)."""
    con = duckdb.connect(database=":memory:")
    con.execute("SET enable_external_access = false;")
    # Создаем тестовую таблицу с пропусками для проверки
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
        # Получаем список таблиц (в будущем заменим на чтение из DatasetCatalog #59)
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        tables_info = []
        facts = []

        for t_name in table_names:
            # Считаем строки
            row_count = con.execute(f"SELECT COUNT(*) FROM {_quote_id(t_name)}").fetchone()[0]
            # Получаем колонки
            cols = con.execute(
                f"""SELECT column_name, data_type FROM information_schema.columns
                 WHERE table_name = '{t_name}'"""
            ).fetchall()
            col_info = [{"name": c[0], "type": c[1]} for c in cols]

            tables_info.append({"table_name": t_name, "row_count": row_count, "columns": col_info})
            facts.append(
                {
                    "text": f"Таблица '{t_name}': {row_count} строк, колонки: {
                        ', '.join([c['name'] for c in col_info])
                    }"
                }
            )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",
            status="ok",
            tool="describe_dataset",
            data={"tables": tables_info},
            facts=facts,
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )


def describe_column(table: str, column: str) -> ToolResult:
    con = _get_mock_connection()
    try:
        # [P1] Безопасно проверяем существование
        col_check = con.execute(
            """SELECT data_type FROM information_schema.columns WHERE table_name = \
                ? AND column_name = ?""",
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

        # [P2] Считаем базовый профиль
        stats = con.execute(
            f"SELECT COUNT(*), COUNT({safe_col}), COUNT(DISTINCT {safe_col}) FROM {safe_table}"
        ).fetchone()
        total, non_null, distinct = stats
        null_count = total - non_null

        data = {
            "total": total,
            "non_null": non_null,
            "distinct": distinct,
            "null_count": null_count,
        }
        facts = [{"text": f"Всего строк: {total}, пропусков: {null_count}, уникальных: {distinct}"}]

        # [P2] Добавляем частоты и примеры для ЛЮБОЙ колонки
        freqs = con.execute(
            f"SELECT {safe_col}, COUNT(*) FROM {safe_table} GROUP BY {
                safe_col
            } ORDER BY COUNT(*) DESC LIMIT 10"
        ).fetchall()
        data["frequencies"] = freqs

        examples = con.execute(
            f"SELECT DISTINCT {safe_col} FROM {safe_table} WHERE {safe_col} IS NOT NULL LIMIT 5"
        ).fetchall()
        data["examples"] = [e[0] for e in examples]

        # [P2] Добавляем числовую статистику
        dtype = col_check[0].upper()
        if "INT" in dtype or "DECIMAL" in dtype or "DOUBLE" in dtype or "FLOAT" in dtype:
            num_stats = con.execute(
                f"SELECT MIN({safe_col}), MAX({safe_col}), AVG({safe_col}), MEDIAN({
                    safe_col
                }), STDDEV_POP({safe_col}) FROM {safe_table}"
            ).fetchone()
            if num_stats[0] is not None:
                data.update(
                    {
                        "min": num_stats[0],
                        "max": num_stats[1],
                        "mean": round(num_stats[2], 2),
                        "median": num_stats[3],
                        "std": round(num_stats[4], 2),
                    }
                )
                # ДОБАВЛЯЕМ МЕДИАНУ В FACTS (как просил тест)
                facts.append(
                    {
                        "text": f"Мин: {num_stats[0]}, Макс: {num_stats[1]}, Среднее: {
                            round(num_stats[2], 2)
                        }, Медиана: {num_stats[3]}"
                    }
                )

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",
            status="ok",
            tool="describe_column",
            data=data,
            facts=facts,
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )


def value_counts(table: str, column: str, top_n: int = 10, filter: str | None = None) -> ToolResult:
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

        query = f"SELECT {safe_col}, COUNT(*) as cnt FROM {safe_table}"

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
                facts=[{"text": "По заданному фильтру данные не найдены."}],
            )

        facts = []
        for row in result:
            val = row[0] if row[0] is not None else "ПУСТО"
            facts.append({"text": f"Значение '{val}' встречается {row[1]} раз."})

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",
            status="ok",
            tool="value_counts",
            data={"rows": result, "columns": [column, "count"]},
            facts=facts,
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )
