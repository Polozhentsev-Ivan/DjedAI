"""B1-8: подключение настоящего PostgreSQL под read-only ролью.

Нужен демо-PostgreSQL из ``infra/postgres/README.md`` с загруженной синтетикой
(``scripts/seed_demo_postgres.py``) и DSN роли ``djedai_reader`` в ``DJEDAI_TEST_PG_DSN``;
без переменной тесты пропускаются. Контрольные значения считаются запросом прямо в
PostgreSQL (``postgres_query``), мимо представлений DuckDB.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
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
from djedai.data.service import connect_postgres_dataset
from djedai.data.typed_views import _lit

DSN = os.environ.get("DJEDAI_TEST_PG_DSN")
WRITER_DSN = os.environ.get("DJEDAI_DEMO_PG_LOADER_DSN")  # меняет таблицу «снаружи»
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="DJEDAI_TEST_PG_DSN не задан (infra/postgres/README.md)"),
]
TABLES = ["public.sales", "products", "stores"]


@pytest.fixture
def info():
    return parse_dsn(DSN)


@pytest.fixture
def con(tmp_path):
    connection = duckdb.connect(str(tmp_path / "dataset.duckdb"))
    yield connection
    connection.close()


def direct(connection, sql: str):
    """Запрос прямо в PostgreSQL, мимо представлений DuckDB."""
    quoted = sql.replace("'", "''")
    return connection.execute(f"SELECT * FROM postgres_query('{ALIAS}', '{quoted}')").fetchall()


def test_selected_tables_go_through_shared_profiler(con):
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


def test_control_query_matches_postgres(con):
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


def test_only_selected_tables_are_exposed(con):
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
def test_writes_are_rejected(con, statement):
    connect_postgres(con, DSN, ["stores"])
    before = direct(con, "SELECT count(*) FROM stores")
    with pytest.raises(duckdb.Error, match="read-only|read only"):
        con.execute(statement)
    assert direct(con, "SELECT count(*) FROM stores") == before


def test_session_is_read_only(con):
    attach_postgres(con, DSN)
    assert direct(con, "SELECT current_setting('transaction_read_only')") == [("on",)]


def test_password_is_not_exposed(con, info):
    attach_postgres(con, DSN)
    visible = con.execute("SELECT * FROM duckdb_databases()").fetchall()
    visible += con.execute("SELECT * FROM duckdb_secrets()").fetchall()
    assert info.password not in repr(visible)


def test_wrong_password(con, info):
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


def test_unknown_database(con, info):
    other = parse_dsn(
        f"host={info.host} port={info.port} dbname=djedai_no_such_db user={info.user} "
        f"password='{info.password}'"
    )
    with pytest.raises(PostgresError, match="djedai_no_such_db.*не найдена"):
        attach_postgres(con, other)


def test_unreachable_server(con):
    with pytest.raises(PostgresError, match="сервер 127.0.0.1:1 недоступен"):
        attach_postgres(con, "host=127.0.0.1 port=1 user=u password=p connect_timeout=2")


def test_dataset_catalog_without_copying_data(tmp_path):
    path = tmp_path / "ds_pg" / "dataset.duckdb"
    created = datetime(2026, 10, 21, tzinfo=UTC)
    stages = []
    catalog = connect_postgres_dataset(
        DSN,
        TABLES,
        path,
        dataset_id="ds_pg",
        name="Демо PostgreSQL",
        created_at=created,
        progress=lambda stage, fraction, message: stages.append(stage),
    )
    assert (catalog.source.kind, catalog.dataset_id, catalog.created_at) == (
        "postgres",
        "ds_pg",
        created,
    )
    assert [t.table_id for t in catalog.tables] == ["sales", "products", "stores"]
    sales = catalog.table("sales")
    assert (sales.raw_table, sales.source.relation, sales.original_name) == (
        None,
        "public.sales",
        "public.sales",
    )
    assert sales.column("date").role == "time"
    assert sales.column("total_amount").role == "measure"
    assert sales.column("store_id").role == "id"
    links = {(r.from_.table, r.from_.column, r.to.table, r.to.column) for r in catalog.relations}
    assert ("sales", "store_id", "stores", "store_id") in links
    assert stages[0] == "connect" and stages[-1] == "relations"
    assert type(catalog).model_validate_json(catalog.model_dump_json()) == catalog

    with duckdb.connect(str(path)) as reopened:
        # данные не скопированы, а подключение и пароль в файле не сохраняются
        assert reopened.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name IN ('sales', 'stores')"
        ).fetchone() == (0,)
        with pytest.raises(duckdb.Error, match=ALIAS):
            reopened.execute("SELECT count(*) FROM stores").fetchall()
        attach_postgres(reopened, DSN)
        assert reopened.execute("SELECT count(*) FROM sales").fetchone()[0] == sales.row_count


def test_dataset_fails_as_a_whole(tmp_path):
    with pytest.raises(PostgresError, match="таблицы не найдены: nope"):
        connect_postgres_dataset(
            DSN,
            ["stores", "nope"],
            tmp_path / "dataset.duckdb",
            dataset_id="ds_pg",
            name="x",
            created_at=datetime.now(UTC),
        )


@pytest.fixture
def writer():
    """Внешний writer под ролью-владельцем: создаёт таблицу live и меняет её."""
    if not WRITER_DSN:
        pytest.skip("DJEDAI_DEMO_PG_LOADER_DSN не задан (infra/postgres/README.md)")
    info = parse_dsn(WRITER_DSN)
    connection = duckdb.connect()
    connection.execute("LOAD postgres")
    connection.execute(f"CREATE TEMPORARY SECRET w (TYPE postgres, PASSWORD {_lit(info.password)})")
    connection.execute(f"ATTACH {_lit(info.libpq(read_only=False))} AS w (TYPE postgres, SECRET w)")

    def execute(sql: str) -> None:
        connection.execute(f"CALL postgres_execute('w', {_lit(sql)})")

    execute("DROP TABLE IF EXISTS live")
    execute("CREATE TABLE live (id integer, v text)")
    execute("INSERT INTO live VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    yield execute
    execute("DROP TABLE IF EXISTS live")
    connection.close()


def test_profile_after_source_changed(con, writer):
    """Пример ревью: из трёх непустых строк удалены две после создания представления."""
    from djedai.data.catalog_builder import build_db_catalog
    from djedai.data.postgres import read_db_sources
    from djedai.data.profiler import profile_table

    (view,) = connect_postgres(con, DSN, ["live"]).views
    writer("DELETE FROM live WHERE id > 1")
    profile = profile_table(con, view)
    v = next(c for c in profile.columns if c.name == "v")
    assert (profile.row_count, v.null_count, v.null_ratio) == (1, 0, 0.0)
    (source,) = read_db_sources(con)
    build_db_catalog(con, source, view, profile, dataset_id="ds_live")


def test_profile_uses_one_snapshot(con, writer, monkeypatch):
    """Изменение посреди профиля не видно: все счётчики из снимка, где считались строки."""
    from djedai.data import profiler

    (view,) = connect_postgres(con, DSN, ["live"]).views
    original = profiler._profile_column
    changed = []

    def change_then_profile(*args, **kwargs):
        if not changed:
            writer("DELETE FROM live WHERE id > 1")
            changed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(profiler, "_profile_column", change_then_profile)
    profile = profiler.profile_table(con, view)
    assert changed
    assert profile.row_count == 3
    assert {c.name: (c.null_count, c.distinct_count) for c in profile.columns} == {
        "id": (0, 3),
        "v": (0, 3),
    }
    assert direct(con, "SELECT count(*) FROM live") == [(1,)]  # а сама таблица изменилась
