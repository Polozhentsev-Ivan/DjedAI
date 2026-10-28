import pytest

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
    # Теперь DuckDB сам блокирует это из-за enable_external_access = false
    assert result.error.code == "database_error"


def test_run_sql_forbidden_path_read():
    # Проверяем обход через прямое имя файла
    result = run_sql("SELECT * FROM '/etc/passwd'")
    assert result.status == "error"
    assert result.error.code == "database_error"


def test_run_sql_hard_limit():
    # Проверяем, что даже если в SQL написан LIMIT 6000, мы получим только 5 (наше max_rows)
    result = run_sql("SELECT * FROM range(6000) LIMIT 6000", max_rows=5)
    assert result.status == "ok"
    assert result.data["row_count"] == 5


@pytest.mark.parametrize(
    "limit_clause",
    [
        "LIMIT 2",
        "LIMIT (1 + 1)",
        "LIMIT (SELECT 2)",
        "LIMIT CAST(2 AS INTEGER)",
        "FETCH FIRST 2 ROWS ONLY",
    ],
)
def test_preserves_small_limit_expressions(limit_clause):
    result = run_sql(f"SELECT range AS n FROM range(10) ORDER BY n {limit_clause}")
    assert result.status == "ok"
    assert result.data["columns"] == ["n"]
    assert result.data["rows"] == [[0], [1]]


@pytest.mark.parametrize(
    "limit_clause",
    [
        "",
        "LIMIT ALL",
        "LIMIT 6000",
        "LIMIT (3000 + 3000)",
        "LIMIT (SELECT 6000)",
        "LIMIT CAST(6000 AS INTEGER)",
        "FETCH FIRST 6000 ROWS ONLY",
        "LIMIT 50%",
        "LIMIT NULL",
        "LIMIT (SELECT NULL)",
    ],
)
def test_caps_large_or_unlimited_queries(limit_clause):
    result = run_sql(f"SELECT range AS n FROM range(10000) ORDER BY n {limit_clause}", max_rows=3)
    assert result.status == "ok"
    assert result.data["rows"] == [[0], [1], [2]]


@pytest.mark.parametrize("limit_clause", ["", "LIMIT ALL", "LIMIT 6000"])
def test_default_row_cap_is_5000(limit_clause):
    result = run_sql(f"SELECT range AS n FROM range(10000) ORDER BY n {limit_clause}")
    assert result.status == "ok"
    assert result.data["row_count"] == 5000
    assert len(result.data["rows"]) == 5000
    assert result.data["rows"][0] == [0]
    assert result.data["rows"][-1] == [4999]


@pytest.mark.parametrize("max_rows, expected", [(5, [[8], [7]]), (1, [[8]])])
def test_computed_limit_preserves_order_and_offset(max_rows, expected):
    result = run_sql(
        "SELECT range AS n FROM range(10) ORDER BY n DESC LIMIT (SELECT 2) OFFSET 1",
        max_rows=max_rows,
    )
    assert result.status == "ok"
    assert result.data["rows"] == expected


@pytest.mark.parametrize("limit_clause", ["LIMIT 0", "LIMIT (1 - 1)", "LIMIT (SELECT 0)"])
def test_preserves_zero_limit(limit_clause):
    result = run_sql(f"SELECT * FROM range(10) {limit_clause}")
    assert result.status == "ok"
    assert result.data["rows"] == []


@pytest.mark.parametrize(
    "limit_clause",
    [
        "LIMIT -1",
        "LIMIT (SELECT -1)",
        "LIMIT 'invalid'",
        "LIMIT 9223372036854775808",
        "LIMIT (SELECT 1 UNION ALL SELECT 2)",
    ],
)
def test_invalid_limit_is_not_replaced_by_a_successful_cap(limit_clause):
    result = run_sql(f"SELECT * FROM range(10) {limit_clause}")
    assert result.status == "error"
    assert result.error.code == "database_error"


@pytest.mark.parametrize(
    "limit_clause", ["LIMIT (1 + 1)", "LIMIT ALL", "FETCH FIRST 2 ROWS ONLY", "LIMIT 50%"]
)
def test_row_cap_preserves_duplicate_output_column_names(limit_clause):
    result = run_sql(
        f"SELECT range AS a, range + 1 AS a FROM range(10) ORDER BY range {limit_clause}",
        max_rows=2,
    )
    assert result.status == "ok"
    assert result.data["columns"] == ["a", "a"]
    assert result.data["rows"] == [[0, 1], [1, 2]]


def test_small_percentage_limit_is_not_expanded_to_row_cap():
    result = run_sql("SELECT range FROM range(10) ORDER BY range LIMIT 10%", max_rows=5)
    assert result.status == "ok"
    assert result.data["rows"] == [[0]]


def test_correlation_reports_quality_losses(quality_dataset):
    connection, catalog = quality_dataset
    allowed = {t.table_id for t in catalog.tables}
    from djedai.analytics.quality import assess

    result = run_sql(
        "SELECT amount, order_id FROM orders",
        connection=connection,
        allowed_tables=allowed,
    )
    result = assess(result, connection, catalog)
    assert result.quality is not None
    assert any(c.column == "amount" for c in result.quality.columns)
