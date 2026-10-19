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

        facts = [{"text": f"В датасете доступны таблицы: {', '.join(table_names)}"}]

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",
            status="ok",
            tool="describe_dataset",
            data={"tables": table_names},
            facts=facts,
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )


def describe_column(table: str, column: str) -> ToolResult:
    """Возвращает статистику по конкретной колонке."""
    con = _get_mock_connection()
    try:
        # Проверяем, существует ли такая колонка
        cols = con.execute(f"PRAGMA table_info('{table}')").fetchall()
        col_names = [c[1] for c in cols]

        if column not in col_names:
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(
                    code="invalid_column",
                    message=f"Колонка '{column}' не найдена в таблице '{table}'.",
                    suggestion=f"Доступные колонки: {', '.join(col_names)}",
                ),
            )

        # Считаем статистику: всего строк, заполненных, уникальных
        stats = con.execute(
            f"SELECT COUNT(*), COUNT({column}), COUNT(DISTINCT {column}) FROM {table}"
        ).fetchone()
        total_rows, non_null, distinct = stats

        facts = [
            {"text": f"Всего строк в таблице: {total_rows}"},
            {"text": f"Заполненных значений в колонке '{column}': {non_null}"},
            {"text": f"Уникальных значений: {distinct}"},
        ]

        return ToolResult(
            result_id=f"r_{uuid.uuid4().hex[:8]}",
            status="ok",
            tool="describe_column",
            data={"total": total_rows, "non_null": non_null, "distinct": distinct},
            facts=facts,
        )
    except Exception as e:
        return ToolResult(
            result_id=str(uuid.uuid4()),
            status="error",
            error=ToolError(code="db_error", message=str(e)),
        )


def value_counts(table: str, column: str, top_n: int = 10) -> ToolResult:
    """Возвращает топ-N самых частых значений в колонке."""
    con = _get_mock_connection()
    try:
        # Проверяем колонку
        cols = con.execute(f"PRAGMA table_info('{table}')").fetchall()
        if column not in [c[1] for c in cols]:
            return ToolResult(
                result_id=str(uuid.uuid4()),
                status="error",
                error=ToolError(code="invalid_column", message="Колонка не найдена"),
            )

        # Получаем частоты
        result = con.execute(
            f"SELECT {column}, COUNT(*) as cnt FROM {
                table} GROUP BY {column} ORDER BY cnt DESC LIMIT {top_n}"
        ).fetchall()

        if not result:
            return ToolResult(
                result_id=f"r_{uuid.uuid4().hex[:8]}",
                status="empty",
                tool="value_counts",
                facts=[{"text": "Данные не найдены."}],
            )

        # Формируем факты для нейросети
        facts = []
        for row in result:
            val = row[0] if row[0] is not None else "ПУСТО"
            cnt = row[1]
            facts.append({"text": f"Значение '{val}' встречается {cnt} раз."})

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
