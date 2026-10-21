"""Адаптер PostgreSQL (B1-8) без сервера PostgreSQL.

Разбор строки подключения и тексты ошибок проверяются напрямую. Построение представлений,
профиль и связи — на подключённом файле DuckDB под тем же именем ``pg_src``: адаптер
читает схему через ``duckdb_columns()``, поэтому для него это та же подключённая БД.
Подключение к настоящему PostgreSQL — в ``tests/integration/test_postgres_adapter.py``.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from djedai.data.postgres import (
    ALIAS,
    PostgresError,
    _explain,
    create_postgres_view,
    create_postgres_views,
    list_tables,
    parse_dsn,
    read_db_sources,
    select_tables,
)
from djedai.data.profiler import profile_tables
from djedai.data.relations import find_relations
from djedai.data.typed_views import read_journal, read_typed_views

PASSWORD = "s3cr'et p@ss"


@pytest.fixture
def source(tmp_path):
    """Файл DuckDB в роли PostgreSQL: таблицы клиентов и заказов с типами БД."""
    path = tmp_path / "source.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("CREATE TYPE mood AS ENUM ('ok', 'bad')")
        db.execute(
            """CREATE TABLE clients (
                id INTEGER, "Имя клиента" VARCHAR, uid UUID, active BOOLEAN,
                registered TIMESTAMPTZ, born DATE, mood mood, tags INTEGER[])"""
        )
        db.execute(
            """INSERT INTO clients VALUES
            (1, 'Анна', '00000000-0000-0000-0000-000000000001', true,
             '2024-01-01 03:00:00+03', '1990-05-01', 'ok', [1, 2]),
            (2, '  ', '00000000-0000-0000-0000-000000000002', false,
             '2024-02-01 12:00:00+00', NULL, 'bad', NULL),
            (3, NULL, '00000000-0000-0000-0000-000000000003', NULL,
             NULL, '1985-01-01', NULL, [])"""
        )
        db.execute(
            "CREATE TABLE orders (order_id BIGINT, client_id INTEGER, amount DECIMAL(12,2), "
            "created_at TIMESTAMP)"
        )
        db.execute(
            """INSERT INTO orders VALUES
            (10, 1, 100.50, '2024-03-01 10:00'), (11, 1, 20.00, '2024-03-02 11:30'),
            (12, 2, 5.25, '2024-03-03 09:15'), (13, 3, 1.00, '2024-03-04 08:00')"""
        )
        db.execute("CREATE SCHEMA crm")
        db.execute("CREATE TABLE crm.orders (note VARCHAR)")
        db.execute("INSERT INTO crm.orders VALUES ('x'), ('y')")
    return path


@pytest.fixture
def con(source):
    connection = duckdb.connect(":memory:")
    connection.execute(f"ATTACH '{source}' AS {ALIAS} (READ_ONLY)")
    yield connection
    connection.close()


class TestParseDsn:
    def test_uri(self):
        info = parse_dsn(
            "postgresql://reader:p%40ss%20w@db.local:6543/sales?sslmode=require"
            "&connect_timeout=5&application_name=djedai"
        )
        assert (info.user, info.password, info.host, info.port, info.dbname) == (
            "reader", "p@ss w", "db.local", 6543, "sales",
        )  # fmt: skip
        assert (info.sslmode, info.connect_timeout, info.application_name) == (
            "require", 5, "djedai",
        )  # fmt: skip

    def test_keywords_with_quotes(self):
        info = parse_dsn(r"host=h port=5433 dbname='my db' user=u password='it\'s'")
        assert (info.host, info.port, info.dbname, info.password) == ("h", 5433, "my db", "it's")

    def test_defaults(self):
        info = parse_dsn("postgres://u:p@/")
        assert (info.host, info.port, info.dbname, info.connect_timeout) == (
            "localhost", 5432, "u", 10,
        )  # fmt: skip

    @pytest.mark.parametrize(
        ("dsn", "reason"),
        [
            ("", "пуста"),
            ("host=h user=u", "не указан пароль"),
            ("postgresql://:p@h/db", "не указан пользователь"),
            ("host=h user=u password=p options='-c default_transaction_read_only=off'", "options"),
            ("host=h user=u password=p passfile=/etc/passwd", "passfile"),
            ("host=h user=u password=p port=99999", "port"),
            ("postgresql://u:p@h:abc/db", "port"),
            ("host=h user=u password=p sslmode=maybe", "sslmode"),
            ("host=h user=u password=p connect_timeout=0", "connect_timeout"),
            ("host=h 'broken", "key=value"),
        ],
    )
    def test_rejected(self, dsn, reason):
        with pytest.raises(PostgresError, match=reason):
            parse_dsn(dsn)

    def test_password_never_shown(self):
        info = parse_dsn(f"host=h user=u password='{PASSWORD.replace("'", "\\'")}'")
        assert info.password == PASSWORD
        for text in (repr(info), info.describe(), info.libpq(), info.libpq(read_only=False)):
            assert PASSWORD not in text
        assert info.describe() == "u@h:5432/u"

    def test_attach_string_forces_read_only(self):
        info = parse_dsn("host=h user=u password=p")
        assert "options='-c default_transaction_read_only=on'" in info.libpq()
        assert "options" not in info.libpq(read_only=False)


class TestExplain:
    info = parse_dsn(f"postgresql://reader:{PASSWORD.replace(' ', '%20')}@db:5432/sales")

    @pytest.mark.parametrize(
        ("message", "reason"),
        [
            ('FATAL:  password authentication failed for user "reader"', "неверный пользователь"),
            ('FATAL:  role "reader" does not exist', "неверный пользователь"),
            ('FATAL:  database "sales" does not exist', "база данных «sales» не найдена"),
            ("failed: Connection refused", "сервер db:5432 недоступен"),
            ("failed: timeout expired", "сервер db:5432 недоступен"),
            ('could not translate host name "db" to address', "сервер db:5432 недоступен"),
            ('FATAL:  no pg_hba.conf entry for host "1.2.3.4"', "не разрешает подключение"),
        ],
    )
    def test_reasons(self, message, reason):
        assert reason in _explain(f"IO Error: Unable to connect: {message}", self.info)

    def test_unknown_error_masks_password(self):
        text = _explain(f"connection failed: server said {PASSWORD}", self.info)
        assert PASSWORD not in text
        assert "***" in text


class TestSelectTables:
    available = [("crm", "orders"), ("public", "clients"), ("public", "orders")]

    def test_all_by_default(self):
        assert select_tables(self.available, None) == self.available

    def test_public_by_default_and_dedup(self):
        assert select_tables(self.available, ["orders", "crm.orders", "public.orders"]) == [
            ("public", "orders"),
            ("crm", "orders"),
        ]

    def test_unknown(self):
        with pytest.raises(PostgresError, match="таблицы не найдены: secret, crm.clients"):
            select_tables(self.available, ["clients", "secret", "crm.clients"])

    def test_empty(self):
        with pytest.raises(PostgresError, match="ни одной"):
            select_tables(self.available, [])

    def test_database_without_tables(self):
        with pytest.raises(PostgresError, match="нет доступных таблиц"):
            select_tables([], None)


def test_list_tables(con):
    con.execute("CREATE TABLE local_table (a INTEGER)")
    assert list_tables(con) == [("crm", "orders"), ("main", "clients"), ("main", "orders")]


class TestViews:
    def test_types_from_database(self, con):
        view = create_postgres_view(con, "main", "clients")
        columns = {c.name: c for c in view.columns}
        assert (view.raw_table, view.view_name) == ("pg_main_clients", "clients")
        assert {name: (c.physical_type, c.semantic_type) for name, c in columns.items()} == {
            "id": ("INTEGER", "id"),
            "imya_klienta": ("VARCHAR", "text"),
            "uid": ("VARCHAR", "id"),
            "active": ("BOOLEAN", "bool"),
            "registered": ("TIMESTAMP", "datetime"),
            "born": ("DATE", "date"),
            "mood": ("VARCHAR", "text"),
            "tags": ("VARCHAR", "text"),
        }
        assert columns["imya_klienta"].original_name == "Имя клиента"
        # пустая строка из пробелов — пропуск, как в профайлере; типы БД разобраны полностью
        assert {name: c.non_empty for name, c in columns.items()} == {
            "id": 3, "imya_klienta": 1, "uid": 3, "active": 2,
            "registered": 2, "born": 2, "mood": 2, "tags": 2,
        }  # fmt: skip
        assert all(c.parsed == c.non_empty for c in view.columns)
        assert read_typed_views(con) == [view]

    def test_values_are_converted(self, con):
        create_postgres_view(con, "main", "clients")
        rows = con.execute("SELECT id, registered, uid, mood, tags FROM clients ORDER BY id")
        assert rows.fetchall() == [
            (1, datetime(2024, 1, 1, 0, 0), "00000000-0000-0000-0000-000000000001", "ok", "[1, 2]"),
            (2, datetime(2024, 2, 1, 12, 0), "00000000-0000-0000-0000-000000000002", "bad", None),
            (3, None, "00000000-0000-0000-0000-000000000003", None, "[]"),
        ]

    def test_journal(self, con):
        create_postgres_view(con, "main", "clients")
        entries = {(e.column_name, e.action): e.detail for e in read_journal(con)}
        assert entries == {
            ("imya_klienta", "rename_column"): {"from": "Имя клиента", "to": "imya_klienta"},
            ("uid", "cast_type"): {"from": "UUID", "to": "VARCHAR"},
            ("registered", "cast_type"): {
                "from": "TIMESTAMP WITH TIME ZONE",
                "to": "TIMESTAMP",
                "timezone": "UTC",
            },  # fmt: skip
            ("mood", "cast_type"): {"from": "ENUM('ok', 'bad')", "to": "VARCHAR"},
            ("tags", "keep_text"): {"reason": "unsupported_type", "type": "INTEGER[]"},
        }

    def test_same_names_in_two_schemas(self, con):
        views, errors = create_postgres_views(con, [("main", "orders"), ("crm", "orders")])
        assert errors == ()
        assert [(v.raw_table, v.view_name) for v in views] == [
            ("pg_main_orders", "orders"),
            ("pg_crm_orders", "orders_typed"),
        ]
        assert [
            (s.raw_table, s.view_name, s.source_kind, s.relation) for s in read_db_sources(con)
        ] == [
            ("pg_crm_orders", "orders_typed", "postgres", "crm.orders"),
            ("pg_main_orders", "orders", "postgres", "main.orders"),
        ]

    def test_repeat_keeps_names(self, con):
        first = create_postgres_view(con, "main", "orders")
        second = create_postgres_view(con, "main", "orders")
        assert (second.raw_table, second.view_name) == (first.raw_table, first.view_name)
        assert len(read_db_sources(con)) == 1
        assert len(read_typed_views(con)) == 1

    def test_error_of_one_table_does_not_stop_others(self, con):
        views, errors = create_postgres_views(con, [("main", "missing"), ("main", "orders")])
        assert [v.view_name for v in views] == ["orders"]
        assert [(e.table, e.reason) for e in errors] == [
            ("main.missing", "таблица не найдена или в ней нет колонок")
        ]
        assert str(errors[0]).startswith("Таблица «main.missing»")

    def test_source_stays_read_only(self, con):
        create_postgres_view(con, "main", "orders")
        with pytest.raises(duckdb.Error, match="read-only"):
            con.execute("INSERT INTO pg_src.main.orders VALUES (1, 1, 1, NULL)")


def test_shared_profiler_and_relations(con):
    create_postgres_views(con, [("main", "clients"), ("main", "orders")])
    report = profile_tables(con)
    assert report.errors == ()
    profiles = {p.view_name: p for p in report.profiles}
    orders = profiles["orders"]
    assert (orders.row_count, orders.duplicate_rows, orders.time_columns) == (4, 0, ("created_at",))
    assert orders.primary_key_candidates == ("order_id", "created_at")
    amount = next(c for c in orders.columns if c.name == "amount")
    assert amount.stats["min"] == "1.00"
    assert amount.stats["max"] == "100.50"
    assert amount.stats["mean"] == pytest.approx(31.6875)
    clients = profiles["clients"]
    name = next(c for c in clients.columns if c.name == "imya_klienta")
    assert (name.null_count, name.distinct_count) == (2, 1)
    registered = next(c for c in clients.columns if c.name == "registered")
    assert registered.stats["min"] == "2024-01-01T00:00:00"

    relations = find_relations(con).relations
    assert [(r.from_table, r.from_column, r.to_table, r.to_column) for r in relations] == [
        ("orders", "client_id", "clients", "id")
    ]
    assert relations[0].to_contract().confidence == "high"


def test_catalog_of_database_tables(con):
    from djedai.contracts.catalog import DatasetCatalog
    from djedai.data.catalog_builder import build_db_catalog
    from djedai.data.profiler import profile_table

    (view,), errors = create_postgres_views(con, [("main", "clients")])
    assert errors == ()
    (source,) = read_db_sources(con)
    catalog = build_db_catalog(con, source, view, profile_table(con, view), dataset_id="ds_pg")
    assert catalog.source.kind == "postgres"
    (table,) = catalog.tables
    assert (table.table_id, table.raw_table, table.original_name) == (
        "clients",
        None,
        "main.clients",
    )
    assert table.source.relation == "main.clients"
    assert table.source.file is None
    roles = {c.name: c.role for c in table.columns}
    assert roles == {
        "id": "id", "imya_klienta": "text", "uid": "id", "active": "dimension",
        "registered": "time", "born": "time", "mood": "text", "tags": "text",
    }  # fmt: skip
    assert table.column("imya_klienta").original_name == "Имя клиента"
    assert all(c.parse is None for c in table.columns)
    assert {(t.column, t.action) for t in catalog.transformations} == {
        ("imya_klienta", "rename_column"),
        ("uid", "cast_type"),
        ("registered", "cast_type"),
        ("mood", "cast_type"),
        ("tags", "keep_text"),
    }
    assert DatasetCatalog.model_validate_json(catalog.model_dump_json()) == catalog
