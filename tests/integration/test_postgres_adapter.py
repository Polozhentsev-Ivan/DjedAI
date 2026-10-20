"""B1-8: подключение настоящего PostgreSQL под read-only ролью.

Нужен демо-PostgreSQL из ``infra/postgres/README.md`` с загруженной синтетикой
(``scripts/seed_demo_postgres.py``) и DSN роли ``djedai_reader`` в ``DJEDAI_TEST_PG_DSN``;
без переменной тесты пропускаются. Контрольные значения считаются запросом прямо в
PostgreSQL (``postgres_query``), мимо представлений DuckDB.
"""

from __future__ import annotations

import os
from decimal import Decimal

import duckdb
import pytest

from djedai.data.postgres import (
    ALIAS,
    PostgresError,
    attach_postgres,
    connect_postgres,
    parse_dsn,
)
from djedai.data.profiler import profile_tables
from djedai.data.relations import find_relations
from djedai.data.workspace import DatasetAccessError, create_workspace, open_workspace

DSN = os.environ.get("DJEDAI_TEST_PG_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="DJEDAI_TEST_PG_DSN не задан (infra/postgres/README.md)"),
]
TABLES = ["public.sales", "products", "stores"]


@pytest.fixture
def info():
    return parse_dsn(DSN)


@pytest.fixture
def workspace(tmp_path):
    dataset_id, connection = create_workspace(tmp_path, "alice", "postgres")
    yield tmp_path, dataset_id, connection
    connection.close()


def direct(connection, sql: str):
    """Запрос прямо в PostgreSQL, мимо представлений DuckDB."""
    quoted = sql.replace("'", "''")
    return connection.execute(f"SELECT * FROM postgres_query('{ALIAS}', '{quoted}')").fetchall()


def test_selected_tables_go_through_shared_profiler(workspace):
    _, _, con = workspace
    report = connect_postgres(con, DSN, TABLES)
    assert report.errors == ()
    assert [v.view_name for v in report.views] == ["sales", "products", "stores"]
    assert "password" not in report.source

    profiles = profile_tables(con)
    assert profiles.errors == ()
    rows = {p.view_name: p.row_count for p in profiles.profiles}
    expected = {t: direct(con, f"SELECT count(*) FROM {t}")[0][0] for t in rows}
    assert rows == expected

    relations = {
        (r.from_table, r.from_column, r.to_table, r.to_column)
        for r in find_relations(con).relations
    }
    assert {
        ("sales", "product_id", "products", "product_id"),
        ("sales", "store_id", "stores", "store_id"),
    } <= relations


def test_control_query_matches_postgres(workspace):
    _, _, con = workspace
    connect_postgres(con, DSN, ["sales"])
    through_view = con.execute(
        "SELECT count(*), sum(total_amount), count(DISTINCT store_id) FROM sales"
    ).fetchone()
    in_postgres = direct(
        con,
        "SELECT count(*) AS n, sum(total_amount)::text AS total, "
        "count(DISTINCT store_id) AS stores FROM sales",
    )[0]
    assert through_view == (in_postgres[0], Decimal(in_postgres[1]), in_postgres[2])


def test_only_selected_tables_are_exposed(workspace):
    _, _, con = workspace
    report = connect_postgres(con, DSN, ["stores"])
    assert [v.view_name for v in report.views] == ["stores"]
    with pytest.raises(PostgresError, match="таблицы не найдены: nope"):
        connect_postgres(con, DSN, ["stores", "nope"])


@pytest.mark.parametrize(
    "statement",
    [
        f"INSERT INTO {ALIAS}.public.stores VALUES ('999', 'x', 'y')",
        f"CREATE TABLE {ALIAS}.public.hacked (a INTEGER)",
        f"DROP TABLE {ALIAS}.public.stores",
        f"CALL postgres_execute('{ALIAS}', 'DELETE FROM stores')",
        f"CALL postgres_execute('{ALIAS}', "
        "'COMMIT; SET default_transaction_read_only = off; DELETE FROM stores')",
    ],
)
def test_writes_are_rejected(workspace, statement):
    _, _, con = workspace
    connect_postgres(con, DSN, ["stores"])
    before = direct(con, "SELECT count(*) FROM stores")
    with pytest.raises(duckdb.Error, match="read-only|read only"):
        con.execute(statement)
    assert direct(con, "SELECT count(*) FROM stores") == before


def test_session_is_read_only(workspace):
    _, _, con = workspace
    attach_postgres(con, DSN)
    assert direct(con, "SELECT current_setting('transaction_read_only')") == [("on",)]


def test_password_is_not_exposed(workspace, info):
    _, _, con = workspace
    attach_postgres(con, DSN)
    visible = con.execute("SELECT * FROM duckdb_databases()").fetchall()
    visible += con.execute("SELECT * FROM duckdb_secrets()").fetchall()
    assert info.password not in repr(visible)


def test_wrong_password(workspace, info):
    _, _, con = workspace
    wrong = parse_dsn(
        f"host={info.host} port={info.port} dbname={info.dbname} user={info.user} "
        "password=definitely-wrong-password"
    )
    with pytest.raises(PostgresError, match="неверный пользователь или пароль") as error:
        attach_postgres(con, wrong)
    assert "definitely-wrong-password" not in str(error.value)
    assert error.value.__cause__ is None
    attached = con.execute(
        "SELECT count(*) FROM duckdb_databases() WHERE database_name = ?", [ALIAS]
    ).fetchone()
    assert attached == (0,)


def test_unknown_database(workspace, info):
    _, _, con = workspace
    other = parse_dsn(
        f"host={info.host} port={info.port} dbname=djedai_no_such_db user={info.user} "
        f"password='{info.password}'"
    )
    with pytest.raises(PostgresError, match="djedai_no_such_db.*не найдена"):
        attach_postgres(con, other)


def test_unreachable_server(workspace):
    _, _, con = workspace
    with pytest.raises(PostgresError, match="сервер 127.0.0.1:1 недоступен"):
        attach_postgres(con, "host=127.0.0.1 port=1 user=u password=p connect_timeout=2")


def test_owner_reopens_and_reattaches_other_user_is_refused(workspace):
    root, dataset_id, con = workspace
    connect_postgres(con, DSN, ["stores"])
    con.close()

    with open_workspace(root, "alice", dataset_id) as reopened:
        # подключение и пароль в файле не сохраняются: без ATTACH представление не читается
        with pytest.raises(duckdb.Error, match=ALIAS):
            reopened.execute("SELECT count(*) FROM stores").fetchall()
        attach_postgres(reopened, DSN)
        assert reopened.execute("SELECT count(*) FROM stores").fetchone()[0] > 0

    with pytest.raises(DatasetAccessError):
        open_workspace(root, "bob", dataset_id)
