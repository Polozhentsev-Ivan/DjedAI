"""Таблицы с цифрой в начале имени и подсказки в ошибках SQL (#233)."""

import duckdb
import pytest

from djedai.analytics.registry import execute_tool
from djedai.analytics.scope import quote_catalog_tables, rename_reserved_ctes

TABLE = "08_fonovye_zadaniya"


@pytest.fixture
def connection():
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE \"{TABLE}\" AS SELECT 1 AS id, '{TABLE}' AS label, 5 AS timeout_ms")
    con.execute('CREATE TABLE "09_other" AS SELECT 42 AS secret_value')
    yield con
    con.close()


def run(connection, sql):
    return execute_tool("run_sql", {"sql": sql}, connection=connection, allowed_tables=[TABLE])


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (f"SELECT * FROM {TABLE}", f'SELECT * FROM "{TABLE}"'),
        (f"SELECT * FROM {TABLE.upper()} t", f'SELECT * FROM "{TABLE}" t'),
        (f"SELECT {TABLE}.id FROM main.{TABLE}", f'SELECT "{TABLE}".id FROM main."{TABLE}"'),
    ],
)
def test_quotes_known_table_references(sql, expected):
    assert quote_catalog_tables(sql, [TABLE]) == expected


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT '{TABLE}'",
        f"SELECT 'it''s {TABLE}'",
        f"SELECT E'a\\' {TABLE}'",
        f"SELECT $${TABLE}$$",
        f"SELECT $tag${TABLE}$tag$",
        f'SELECT * FROM "{TABLE}"',
        f"SELECT 1 -- {TABLE}",
        f"SELECT 1 /* {TABLE} /* nested */ {TABLE} */",
        f"SELECT {TABLE}_suffix FROM t",
        "SELECT 1 FROM t LIMIT 1_000",
    ],
)
def test_keeps_literals_comments_and_quoted_names(sql):
    assert quote_catalog_tables(sql, [TABLE, "1_000"]) == sql


def test_query_runs_and_literal_is_unchanged(connection):
    result = run(connection, f"SELECT '{TABLE}' AS text, id FROM {TABLE} WHERE label = '{TABLE}'")

    assert result.status == "ok"
    assert result.data.rows == [[TABLE, 1]]
    assert result.sql == f"SELECT '{TABLE}' AS text, id FROM \"{TABLE}\" WHERE label = '{TABLE}'"


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "09_other"',
        f'SELECT * FROM {TABLE} JOIN "09_other" ON true',
        f"SELECT * FROM other_schema.{TABLE}",
        f"SELECT * FROM other_db.main.{TABLE}",
    ],
)
def test_table_outside_dataset_is_still_rejected(connection, sql):
    result = run(connection, sql)

    assert result.status == "error"
    assert result.error.code == "dataset_scope_violation"
    assert "42" not in result.error.model_dump_json()
    assert "09_other" not in result.error.suggestion
    assert TABLE in result.error.suggestion


def test_unquoted_table_outside_dataset_does_not_run(connection):
    result = run(connection, "SELECT * FROM 09_other")

    assert result.status == "error"
    assert result.error.code == "sql_parse_error"
    assert "кавычки" in result.error.suggestion
    assert "09_other" not in result.error.model_dump_json()


@pytest.mark.parametrize(
    ("sql", "code", "hint"),
    [
        (f"SELECT nope FROM {TABLE}", "database_error", "колонка"),
        (f"SELECT id, COUNT(*) FROM {TABLE}", "database_error", "GROUP BY"),
        (f"SELECT id + 'x' FROM {TABLE}", "database_error", "CAST"),
        (f"SELECT FROM WHERE {TABLE}", "sql_parse_error", "Синтаксическая"),
        (f"SELECT timeout_oops FROM {TABLE}", "database_error", "колонка"),
    ],
)
def test_sql_errors_give_specific_safe_hint(connection, sql, code, hint):
    result = run(connection, sql)

    assert result.error.code == code
    assert hint in result.error.suggestion
    assert "Binder" not in result.error.model_dump_json()
    assert "timeout_oops" not in result.error.model_dump_json()


def test_forbidden_function_hint_names_no_sql(connection):
    result = run(connection, f"SELECT toDayOfWeek(id) FROM {TABLE}")

    assert result.error.code == "dataset_scope_violation"
    assert "EXTRACT" in result.error.suggestion
    assert "toDayOfWeek" not in result.error.model_dump_json()


def test_cte_named_pivot_is_renamed_and_runs():
    sql = "WITH a AS (SELECT 1 AS x), pivot AS (SELECT x FROM a) SELECT 'pivot' AS s, x FROM pivot"
    renamed = rename_reserved_ctes(sql)
    assert renamed.endswith("FROM pivot_cte") and "'pivot' AS s" in renamed
    assert duckdb.connect().execute(renamed).fetchall() == [("pivot", 1)]


def test_pivot_operator_is_left_alone():
    sql = "SELECT * FROM t PIVOT (SUM(x) FOR y IN (1, 2))"
    assert rename_reserved_ctes(sql) == sql
