"""A model query can read the selected public views, never the whole connection."""

import duckdb
import pytest

from djedai.analytics.registry import execute_tool


@pytest.fixture
def connection():
    with duckdb.connect() as con:
        con.execute("CREATE TABLE sales(id INTEGER, amount INTEGER, segment VARCHAR)")
        con.execute("INSERT INTO sales VALUES (1,10,'A'),(2,20,'B')")
        con.execute("CREATE TABLE stores(id INTEGER, title VARCHAR)")
        con.execute("INSERT INTO stores VALUES (1,'first'),(2,'second')")
        con.execute("CREATE TABLE raw_sales(secret VARCHAR)")
        con.execute("INSERT INTO raw_sales VALUES ('private raw value')")
        con.execute("CREATE TABLE foreign_result(secret VARCHAR)")
        con.execute("CREATE MACRO private_macro() AS (SELECT secret FROM raw_sales)")
        yield con


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(amount) FROM sales",
        "SELECT SUM(s.amount) FROM main.sales s JOIN stores t ON s.id=t.id",
        "WITH totals AS (SELECT amount FROM sales) SELECT SUM(amount) FROM totals",
        "SELECT SUM(amount) FROM (SELECT * FROM sales) s",
        "WITH raw_sales AS (SELECT amount FROM sales) SELECT SUM(amount) FROM raw_sales",
        "SELECT (SELECT SUM(amount) FROM sales)",
        "SELECT MEDIAN(amount) * COUNT(*) FROM sales",
        "SELECT SUM(amount) FROM sales UNION ALL SELECT SUM(amount) FROM sales",
    ],
)
def test_scoped_queries_resolve_real_sources_through_aliases(connection, sql):
    result = execute_tool(
        "run_sql", {"sql": sql}, connection=connection, allowed_tables={"sales", "stores"}
    )
    assert result.status == "ok", result.error
    assert result.data.rows[0] == [30]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM raw_sales",
        "SELECT * FROM foreign_result",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT * FROM postgres_source.public.sales",
        "SELECT * FROM other.main.sales",
        "SELECT * FROM sales JOIN raw_sales ON TRUE",
        "WITH safe AS (SELECT * FROM raw_sales) SELECT * FROM safe",
        "SELECT (SELECT secret FROM raw_sales) FROM sales",
        "SELECT * FROM sales WHERE EXISTS (SELECT 1 FROM raw_sales)",
        "SELECT * FROM query_table('raw_sales')",
        "SELECT * FROM query('SELECT * FROM raw_sales')",
        "SELECT * FROM postgres_query('postgres_source','SELECT * FROM secret')",
        "SELECT postgres_execute('postgres_source','DELETE FROM sales')",
        "SELECT * FROM duckdb_secrets()",
        "SELECT * FROM duckdb_tables()",
        "SELECT current_query()",
        "SELECT current_setting('search_path')",
        "SELECT private_macro()",
        "SELECT * FROM read_csv('https://example.invalid/secrets')",
        "SELECT * FROM 'sales.parquet'",
        "SELECT * FROM sales; SELECT * FROM raw_sales",
        "SELECT * FROM sales; DROP TABLE sales",
    ],
)
def test_scoped_queries_reject_foreign_sources_and_indirect_access(connection, sql):
    result = execute_tool(
        "run_sql", {"sql": sql}, connection=connection, allowed_tables={"sales", "stores"}
    )
    assert result.status == "error"
    assert result.error.code in {"dataset_scope_violation", "forbidden_operation"}
    assert "private raw value" not in result.model_dump_json()
    assert connection.execute("SELECT COUNT(*) FROM sales").fetchone() == (2,)


def test_scoped_overview_omits_raw_foreign_and_attached_tables(connection):
    result = execute_tool("describe_dataset", {}, connection=connection, allowed_tables={"sales"})
    assert result.status == "ok"
    assert result.data.rows == [["sales", 2, "id, amount, segment"]]
    assert result.facts[0].values["table_count"] == 1
    assert "raw_sales" not in result.model_dump_json()


@pytest.mark.parametrize("tool", ["describe_column", "value_counts"])
def test_descriptive_tools_do_not_accept_foreign_table(connection, tool):
    with pytest.raises(ValueError, match="датасету"):
        execute_tool(
            tool,
            {"table": "raw_sales", "column": "secret"},
            connection=connection,
            allowed_tables={"sales"},
        )


@pytest.mark.parametrize(
    "filter_sql",
    [
        "EXISTS (SELECT 1 FROM raw_sales)",
        "segment = private_macro()",
        "EXISTS (SELECT 1 FROM postgres_query('postgres_source','SELECT * FROM secret'))",
    ],
)
def test_value_counts_filter_cannot_bypass_query_scope(connection, filter_sql):
    result = execute_tool(
        "value_counts",
        {
            "table": "sales",
            "column": "segment",
            "filter": filter_sql,
        },
        connection=connection,
        allowed_tables={"sales"},
    )
    assert result.status == "error"
    assert "raw_sales" not in result.error.message


def test_error_details_do_not_disclose_driver_context(connection):
    result = execute_tool(
        "run_sql",
        {"sql": "SELECT nonexistent FROM sales"},
        connection=connection,
        allowed_tables={"sales"},
    )
    assert result.status == "error"
    assert "nonexistent" not in result.error.message
    assert "Binder" not in result.error.message


def test_source_result_id_cannot_be_silently_ignored(connection):
    with pytest.raises(ValueError, match="source_result_id"):
        execute_tool(
            "run_sql",
            {"sql": "SELECT 1", "source_result_id": "foreign"},
            connection=connection,
            allowed_tables={"sales"},
        )
