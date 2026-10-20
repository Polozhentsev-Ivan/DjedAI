import threading
import uuid

import duckdb
import sqlglot
from sqlglot import exp

from djedai.contracts.tool_result import ToolError, ToolResult


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

        # 3. ВЫПОЛНЯЕМ С ТАЙМАУТОМ
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
            # Лимит на уровне relation ограничивает работу движка, сохраняя
            # исходные LIMIT/FETCH, ORDER BY, OFFSET и повторяющиеся имена колонок.
            cursor = con.sql(sql).limit(max_rows).execute()
            # [P2] FETCHMANY гарантирует, что в память Python попадет не больше строк, чем max_rows
            result = cursor.fetchmany(max_rows)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
        finally:
            timer.cancel()

        # 4. УПАКОВЫВАЕМ
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
