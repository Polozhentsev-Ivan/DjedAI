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

        # 3. БЛОКИРУЕМ ЧТЕНИЕ ФАЙЛОВ/СЕТИ
        forbidden_functions = [
            "read_csv",
            "read_parquet",
            "read_json",
            "read_text",
            "sqlite_scan",
            "postgres_scan",
        ]
        sql_lower = sql.lower()
        for func in forbidden_functions:
            if func in sql_lower:
                return ToolResult(
                    result_id=str(uuid.uuid4()),
                    status="error",
                    error=ToolError(
                        code="forbidden_function",
                        message=f"Использование функции {func} запрещено.",
                        suggestion="Запрашивайте данные только из уже загруженных таблиц.",
                    ),
                )

        # 4. ДОБАВЛЯЕМ ЛИМИТ (на уровне SQL, чтобы база не считала лишнее)
        if not parsed.args.get("limit"):
            parsed = parsed.limit(max_rows)
            sql = parsed.sql(dialect="duckdb")

        # 5. ВЫПОЛНЯЕМ С ТАЙМАУТОМ
        con = duckdb.connect(database=":memory:")

        # [P1] БЛОКИРУЕМ ВНЕШНИЕ ИСТОЧНИКИ НА УРОВНЕ ДВИЖКА DUCKDB
        con.execute("SET enable_external_access = false;")

        # Создаем тестовые данные
        con.execute("CREATE TABLE test (id INT, name VARCHAR)")
        con.execute("INSERT INTO test VALUES (1, 'Alice'), (2, 'Bob')")

        timer = threading.Timer(30.0, con.interrupt)
        timer.start()

        try:
            # [P2] ИСПОЛЬЗУЕМ FETCHMANY, ЧТОБЫ НЕ ЗАГРУЖАТЬ БОЛЬШЕ max_rows В ПАМЯТЬ
            cursor = con.execute(sql)
            result = cursor.fetchmany(max_rows)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
        finally:
            timer.cancel()

        # 6. УПАКОВЫВАЕМ
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

        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(
                code="database_error",
                message=f"Ошибка базы данных: {error_msg}",
                suggestion=suggestion,
            ),
        )
