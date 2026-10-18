from djedai.analytics.tools import run_sql


def test_run_sql_valid_select():
    # Проверяем, что обычный SELECT работает
    result = run_sql("SELECT * FROM test")
    assert result.status == "ok"
    assert result.data["row_count"] == 2


def test_run_sql_adds_limit():
    # Проверяем, что лимит добавляется автоматически
    result = run_sql("SELECT * FROM test", max_rows=1)
    # В базе 2 строки, но из-за лимита должна остаться 1
    assert len(result.data["rows"]) == 1


def test_run_sql_forbidden_drop():
    # Проверяем, что DROP блокируется
    result = run_sql("DROP TABLE test")
    assert result.status == "error"
    assert result.error.code == "forbidden_operation"


def test_run_sql_forbidden_insert():
    # Проверяем, что INSERT блокируется
    result = run_sql("INSERT INTO test VALUES (3, 'Charlie')")
    assert result.status == "error"
    assert result.error.code == "forbidden_operation"


def test_run_sql_syntax_error():
    # Проверяем, что кривой запрос возвращает понятную ошибку
    result = run_sql("SELECT * FROMM test")
    assert result.status == "error"
    assert "Синтаксическая ошибка" in result.error.message


def test_run_sql_forbidden_file_read():
    # Проверяем, что попытка прочитать файл блокируется
    result = run_sql("SELECT * FROM read_csv('secret.csv')")
    assert result.status == "error"
    assert result.error.code == "forbidden_function"


def test_run_sql_forbidden_path_read():
    # Проверяем обход через прямое имя файла
    result = run_sql("SELECT * FROM '/etc/passwd'")
    assert result.status == "error"
    # Ошибка будет либо forbidden_function, либо database_error из-за отключенного
    # enable_external_access
    assert result.error is not None


def test_run_sql_hard_limit():
    # Проверяем, что даже если в SQL написан LIMIT 6000, мы получим только 5000
    # (range генерирует последовательность чисел)
    result = run_sql("SELECT * FROM range(6000) LIMIT 6000", max_rows=5)
    assert result.status == "ok"
    assert result.data["row_count"] == 5
