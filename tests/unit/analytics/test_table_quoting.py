"""Таблицы с цифрой в начале имени и подсказки в ошибках SQL (#233)."""

from datetime import UTC, datetime

import duckdb
import pytest

from djedai.analytics.registry import execute_tool
from djedai.analytics.scope import quote_catalog_tables, rename_reserved_ctes
from djedai.data.service import CsvSource, ingest_csv_dataset

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
    assert renamed.endswith('FROM "pivot"') and "'pivot' AS s" in renamed
    assert duckdb.connect().execute(renamed).fetchall() == [("pivot", 1)]


def test_pivot_operator_is_left_alone():
    sql = "SELECT * FROM t PIVOT (SUM(x) FOR y IN (1, 2))"
    assert rename_reserved_ctes(sql) == sql


@pytest.mark.parametrize(
    "prefix", ["SELECT 'WITH pivot AS (' AS note,", "/* WITH pivot AS ( */ SELECT"]
)
def test_pivot_declaration_inside_literal_or_comment_does_not_change_operator(prefix):
    sql = f"{prefix} * FROM t PIVOT (SUM(x) FOR y IN ('a', 'b'))"
    assert rename_reserved_ctes(sql) == sql


@pytest.mark.parametrize("operator", ["PIVOT", "UNPIVOT"])
def test_reserved_cte_keeps_its_operator_and_does_not_collide(operator):
    if operator == "PIVOT":
        source = "SELECT * FROM t PIVOT (SUM(x) FOR y IN ('a', 'b'))"
        expected = [(1, 2, 42)]
    else:
        source = "SELECT * FROM (SELECT 1 AS a, 2 AS b) UNPIVOT (x FOR y IN (a, b))"
        expected = [("a", 1, 42), ("b", 2, 42)]
    name = operator.lower()
    sql = (
        f"WITH {name} AS ({source}), {name}_cte AS (SELECT 42 AS n) "
        f"SELECT {name}.*, n FROM {name} CROSS JOIN {name}_cte"
    )
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE t AS SELECT 'a' y, 1 x UNION ALL SELECT 'b', 2")
        result = execute_tool("run_sql", {"sql": sql}, connection=connection, allowed_tables={"t"})
    assert result.status == "ok", result.error
    assert [tuple(row) for row in result.data.rows] == expected


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        ('SELECT COUNT(*) FROM "1e3" WHERE amount < 1e3', "1e3"),
        ("SELECT 1E3 FROM t", "1e3"),
        ("SELECT 1 FROM t WHERE x < 1e-3", "1e"),
        ("SELECT 1.5e3 FROM t", "5e3"),
        ("SELECT 0x1F FROM t", "0x1f"),
        ("SELECT 1 FROM t LIMIT 1e3", "1e3"),
    ],
)
def test_numeric_constant_stays_when_a_table_reads_the_same(sql, name):
    assert quote_catalog_tables(sql, [name]) == sql


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM 1e3", 'SELECT * FROM "1e3"'),
        ("SELECT * FROM t JOIN\n 1e3 ON true", 'SELECT * FROM t JOIN\n "1e3" ON true'),
        ("SELECT * FROM 1e3 WHERE x < 1e3", 'SELECT * FROM "1e3" WHERE x < 1e3'),
    ],
)
def test_numeric_lookalike_is_a_table_after_from_or_join(sql, expected):
    assert quote_catalog_tables(sql, ["1e3"]) == expected


def test_numeric_table_name_through_real_ingest_keeps_the_constant(tmp_path):
    path = tmp_path / "1e3.csv"
    path.write_text("amount\n7\n2000\n", encoding="utf-8")
    database = tmp_path / "data.duckdb"
    catalog = ingest_csv_dataset(
        [CsvSource(path=path, original_name=path.name)],
        database,
        dataset_id="d",
        name=path.name,
        created_at=datetime.now(UTC),
    )
    table = catalog.tables[0].table_id
    assert table == "1e3"
    connection = duckdb.connect(str(database), read_only=True)
    sql = f'SELECT COUNT(*) AS n FROM "{table}" WHERE amount < 1e3'
    # Эталон считается самой DuckDB, без функции преобразования.
    assert connection.execute(sql).fetchone()[0] == 1

    result = execute_tool("run_sql", {"sql": sql}, connection=connection, allowed_tables=[table])

    assert result.status == "ok"
    assert result.data.rows == [[1]]
    assert result.sql == sql
    bare = execute_tool(
        "run_sql",
        {"sql": f"SELECT COUNT(*) AS n FROM {table} WHERE amount < 1e3"},
        connection=connection,
        allowed_tables=[table],
    )
    assert bare.data.rows == [[1]]
    denied = execute_tool(
        "run_sql", {"sql": 'DROP TABLE "1e3"'}, connection=connection, allowed_tables=[table]
    )
    assert denied.status == "error"
    connection.close()
