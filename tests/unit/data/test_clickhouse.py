"""B1-9 / #61: адаптер ClickHouse без сервера — разбор DSN, read-only, порог и копия в DuckDB."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

from djedai.data.clickhouse import (
    ClickHouseError,
    ClickHouseHttp,
    ClickHouseLimits,
    SourceTooLargeError,
    connect_clickhouse,
    list_tables,
    materialize_table,
    open_session,
    parse_dsn,
)
from djedai.data.postgres import read_db_sources

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

from fake_clickhouse import FakeClickHouse  # noqa: E402

SPEC = json.loads((ROOT / "eval" / "demo_db_expectations.json").read_text(encoding="utf-8"))


@pytest.fixture
def fake():
    with FakeClickHouse().load_demo() as server:
        yield server


class TestParseDsn:
    def test_http_url_with_escaped_password(self):
        info = parse_dsn("http://reader:p%40ss%3Aw@ch.local:58123/analytics")
        assert (info.host, info.port, info.user, info.password) == (
            "ch.local",
            58123,
            "reader",
            "p@ss:w",
        )
        assert (info.database, info.secure) == ("analytics", False)
        assert "p@ss" not in repr(info)
        assert info.describe() == "reader@ch.local:58123/analytics"

    @pytest.mark.parametrize(
        ("dsn", "port", "secure"),
        [
            ("http://u:p@h", 8123, False),
            ("https://u:p@h", 8443, True),
            ("clickhouse://u:p@h", 8123, False),
            ("clickhouse://u:p@h?secure=1", 8443, True),
            ("clickhouse://u:p@h:9440?secure=true", 9440, True),
        ],
    )
    def test_schemes_and_default_ports(self, dsn, port, secure):
        info = parse_dsn(dsn)
        assert (info.port, info.secure, info.database) == (port, secure, "default")

    def test_connect_timeout(self):
        assert parse_dsn("http://u:p@h/db?connect_timeout=3").connect_timeout == 3

    @pytest.mark.parametrize(
        ("dsn", "message"),
        [
            ("", "пуста"),
            ("postgresql://u:p@h/db", "http://"),
            ("http://h:8123/db", "пользователь"),
            ("http://u@h:8123/db", "пароль"),
            ("http://u:p@h/db?session_id=1", "session_id"),
            ("http://u:p@h/db?secure=maybe", "secure"),
            ("http://u:p@h:99999/db", "port"),
            ("http://u:p@h/db?connect_timeout=0", "connect_timeout"),
            ("http://u:p@h/db/other", "одну базу"),
        ],
    )
    def test_rejected(self, dsn, message):
        with pytest.raises(ClickHouseError, match=message):
            parse_dsn(dsn)

    def test_default_database_for_scripts(self):
        assert ClickHouseHttp.from_url("http://u:p@h", default_database="demo").database == "demo"


class TestSession:
    def test_reader_gets_read_only_limits(self, fake):
        session = open_session(fake.url, ClickHouseLimits(max_rows=10, max_bytes=20))
        assert session.settings["readonly"] == "2"
        assert session.settings["max_result_rows"] == "10"
        assert session.settings["result_overflow_mode"] == "throw"
        assert session.settings["read_overflow_mode"] == "throw"
        assert fake.calls[-1]["params"]["readonly"] == "2"

    def test_writer_is_forced_to_read_only(self, fake):
        fake.readonly = "0"
        assert open_session(fake.url).settings["readonly"] == "2"

    def test_readonly_1_user_cannot_change_settings(self, fake):
        fake.readonly = "1"
        session = open_session(fake.url)
        assert session.settings == {}
        assert session.query("SELECT getSetting('readonly')").strip() == "1"

    @pytest.mark.parametrize(
        "profile",
        [
            {"result_overflow_mode": "break", "max_result_rows": "2"},
            {"read_overflow_mode": "break", "max_rows_to_read": "2"},
            {"timeout_overflow_mode": "break", "max_execution_time": "1"},
            {"limit": "3"},
            {"offset": "1"},
        ],
    )
    def test_profile_that_can_truncate_answers_is_rejected(self, fake, profile):
        fake.readonly = "1"
        fake.profile |= profile
        with pytest.raises(ClickHouseError, match="допускает усечённые ответы"):
            open_session(fake.url)

    def test_break_with_no_limit_and_overridden_profile_are_accepted(self, fake):
        fake.readonly = "1"
        fake.profile |= {"result_overflow_mode": "break", "max_result_rows": "0"}
        open_session(fake.url)
        # readonly=2 переопределяет профиль сервера настройками сессии
        fake.readonly = "2"
        fake.profile |= {"result_overflow_mode": "break", "max_result_rows": "2", "limit": "3"}
        assert open_session(fake.url).settings["limit"] == "0"

    def test_server_ignoring_read_only_is_rejected(self, fake):
        fake.readonly, fake.honour_readonly = "0", False
        with pytest.raises(ClickHouseError, match="только для чтения"):
            open_session(fake.url)

    def test_password_stays_out_of_requests_and_errors(self, fake):
        open_session(fake.url)
        assert fake.password not in json.dumps([call["params"] for call in fake.calls])
        wrong = fake.url.replace("reader-secret%21", "wrong-secret")
        with pytest.raises(ClickHouseError) as error:
            open_session(wrong)
        assert "неверный пользователь или пароль" in str(error.value)
        assert "wrong-secret" not in str(error.value)

    def test_unreachable_server(self):
        with pytest.raises(ClickHouseError, match="недоступен") as error:
            open_session("http://u:hidden@127.0.0.1:1/db?connect_timeout=1")
        assert "hidden" not in str(error.value)


class TestMaterialize:
    def test_demo_tables_are_copied_typed_and_registered(self, fake, tmp_path):
        with duckdb.connect(str(tmp_path / "dataset.duckdb")) as connection:
            report = connect_clickhouse(connection, fake.url, None, tmp_path)
            assert report.source.startswith("djedai_reader@127.0.0.1:")
            assert report.source.endswith("/djedai_demo") and fake.password not in report.source
            views = {view.view_name: view for view in report.views}
            assert set(views) == set(SPEC["tables"])
            for name, table in SPEC["tables"].items():
                assert [column.name for column in views[name].columns] == table["columns"]
                count = connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                assert count == table["rows"]
            sources = {source.view_name: source for source in read_db_sources(connection)}
            assert sources["sales"].raw_table == "ch_djedai_demo_sales"
            assert sources["sales"].source_kind == "clickhouse"
            assert sources["sales"].relation == "djedai_demo.sales"
            raw = connection.execute(
                "SELECT table_type FROM information_schema.tables "
                "WHERE table_name = 'ch_djedai_demo_sales'"
            ).fetchone()
            assert raw == ("BASE TABLE",)
            for check in SPEC["checks"].values():
                row = connection.execute(check["sql"]).fetchone()
                assert [str(value) for value in row] == check["expected"]
        assert sorted(path.name for path in tmp_path.iterdir()) == ["dataset.duckdb"]

    def test_selected_tables(self, fake, tmp_path):
        with duckdb.connect() as connection:
            report = connect_clickhouse(
                connection, fake.url, ["stores", "djedai_demo.sales", "stores"], tmp_path
            )
            assert [view.view_name for view in report.views] == ["stores", "sales"]

    @pytest.mark.parametrize("tables", [["missing"], ["other_db.sales"], []])
    def test_unknown_tables_fail_before_copy(self, fake, tmp_path, tables):
        with duckdb.connect() as connection, pytest.raises(ClickHouseError):
            connect_clickhouse(connection, fake.url, tables, tmp_path)
        assert not any("FORMAT Parquet" in call["sql"] for call in fake.calls)

    def test_volume_limit_fails_before_any_rows_are_read(self, fake, tmp_path):
        limits = ClickHouseLimits(max_rows=4000)
        with duckdb.connect() as connection, pytest.raises(SourceTooLargeError, match="4801"):
            connect_clickhouse(connection, fake.url, None, tmp_path, limits)
        assert not any("FORMAT Parquet" in call["sql"] for call in fake.calls)
        with duckdb.connect() as connection:
            report = connect_clickhouse(connection, fake.url, ["stores"], tmp_path, limits)
            assert len(report.views) == 1

    def test_byte_limit(self, fake, tmp_path):
        with duckdb.connect() as connection, pytest.raises(SourceTooLargeError, match="байт"):
            connect_clickhouse(connection, fake.url, None, tmp_path, ClickHouseLimits(max_bytes=10))

    def test_views_without_statistics_are_counted(self, fake, tmp_path):
        fake.table_stats = False
        with duckdb.connect() as connection, pytest.raises(SourceTooLargeError):
            connect_clickhouse(connection, fake.url, None, tmp_path, ClickHouseLimits(max_rows=100))
        assert any(call["sql"].startswith("SELECT count()") for call in fake.calls)

    def test_server_side_overflow_is_an_error_not_a_partial_copy(self, fake, tmp_path):
        # предварительный подсчёт пропущен: лимит сервера всё равно не отдаёт усечённую копию
        session = open_session(fake.url, ClickHouseLimits(max_rows=5))
        sales = next(table for table in list_tables(session) if table.name == "sales")
        with duckdb.connect() as connection, pytest.raises(SourceTooLargeError, match="порога"):
            materialize_table(connection, session, sales, tmp_path)
            assert not connection.execute("SELECT * FROM duckdb_tables()").fetchall()
        assert not list(tmp_path.glob("*.parquet"))

    @pytest.mark.parametrize("readonly", ["2", "1"])
    def test_total_limit_counts_rows_that_appeared_after_the_precheck(
        self, fake, tmp_path, readonly
    ):
        # ревью #61: 2 + 2 строки проходят порог 5, к выгрузке обе таблицы вырастают до 4
        fake.readonly = readonly
        for name in ("a", "b"):
            fake.add_table(name, "SELECT * FROM range(2) AS t(n)")

        def grow(table):
            fake.connection.cursor().execute(
                f'INSERT INTO "djedai_demo"."{table}" SELECT * FROM "djedai_demo"."{table}"'
            )

        fake.before_export = grow
        with (
            duckdb.connect() as connection,
            pytest.raises(SourceTooLargeError, match="порога загрузки"),
        ):
            connect_clickhouse(connection, fake.url, ["a", "b"], tmp_path, ClickHouseLimits(5))
        assert not list(tmp_path.glob("*.parquet"))

    def test_rows_loaded_by_earlier_tables_reduce_the_server_limit(self, fake, tmp_path):
        with duckdb.connect() as connection:
            connect_clickhouse(
                connection, fake.url, ["stores", "products"], tmp_path, ClickHouseLimits(60)
            )
        exports = [call for call in fake.calls if call["sql"].endswith("FORMAT Parquet")]
        assert [call["params"]["max_result_rows"] for call in exports] == ["60", "50"]

    def test_short_export_is_not_published_as_a_complete_table(self, fake, tmp_path):
        # ревью #61: сервер сообщает 4376 строк, но без ошибки отдаёт Parquet с частью
        fake.readonly = "1"
        fake.export_rows = 2
        with duckdb.connect() as connection:
            with pytest.raises(
                ClickHouseError, match="выгрузка неполная: получено 2 строк из 4376"
            ):
                connect_clickhouse(connection, fake.url, ["sales"], tmp_path)
            assert connection.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE 'ch_%'"
            ).fetchone() == (0,)
        assert not list(tmp_path.glob("*.parquet"))

    def test_source_that_shrinks_during_export_is_accepted(self, fake, tmp_path):
        fake.before_export = lambda table: fake.connection.cursor().execute(
            f'DELETE FROM "djedai_demo"."{table}" WHERE store_id > (SELECT min(store_id) '
            f'FROM "djedai_demo"."{table}")'
        )
        with duckdb.connect() as connection:
            connect_clickhouse(connection, fake.url, ["stores"], tmp_path)
            assert connection.execute("SELECT count(*) FROM stores").fetchone() == (1,)

    def test_download_size_cap_removes_partial_file(self, fake, tmp_path):
        target = tmp_path / "out.parquet"
        client = parse_dsn(fake.url)
        with pytest.raises(SourceTooLargeError):
            client.download(
                "SELECT * FROM `djedai_demo`.`sales` FORMAT Parquet", target, max_bytes=10
            )
        assert not target.exists()

    def test_wide_integers_are_exported_as_text(self, fake, tmp_path):
        fake.add_table("wide", "SELECT 170141183460469231731687303715884105727::HUGEINT AS big")
        with duckdb.connect() as connection:
            connect_clickhouse(connection, fake.url, ["wide"], tmp_path)
            assert connection.execute("SELECT big FROM wide").fetchone() == (
                "170141183460469231731687303715884105727",
            )
            assert "toString(`big`) AS `big`" in fake.calls[-1]["sql"]
