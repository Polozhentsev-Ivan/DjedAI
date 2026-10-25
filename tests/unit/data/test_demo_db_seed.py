"""#100: эталоны демо-БД, загрузка в ClickHouse и сверка — без серверов PostgreSQL/ClickHouse."""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import seed_demo_clickhouse as ch  # noqa: E402
import verify_demo_db  # noqa: E402
from demo_dataset import DATA_DIR, load_typed_dataset  # noqa: E402

SPEC = json.loads(verify_demo_db.EXPECTATIONS.read_text(encoding="utf-8"))


class DuckBackend:  # те же typed views, из которых наполняются обе демо-БД
    name = "duckdb"

    def __init__(self) -> None:
        self.connection = duckdb.connect()
        self.views = load_typed_dataset(self.connection, DATA_DIR)

    def rows(self, sql):
        return [
            [verify_demo_db._text(v) for v in row]
            for row in self.connection.execute(sql).fetchall()
        ]

    def tables(self):
        return sorted(self.views)

    def columns(self, table):
        return [row[0] for row in self.connection.execute(f'DESCRIBE "{table}"').fetchall()]

    def can_write(self):
        return False


def test_expectations_match_synthetic_references():
    synthetic = json.loads(verify_demo_db.SYNTHETIC.read_text(encoding="utf-8"))
    assert verify_demo_db.reference_mismatches(SPEC, synthetic) == []


def test_reference_mismatch_is_reported():
    spec = json.loads(json.dumps(SPEC))
    spec["checks"]["sales_duplicate_rows"]["expected"] = ["207"]
    synthetic = json.loads(verify_demo_db.SYNTHETIC.read_text(encoding="utf-8"))
    assert verify_demo_db.reference_mismatches(spec, synthetic) == [
        "checks.sales_duplicate_rows: ['207'] != ['208'] (metrics.duplicate_rows)"
    ]


def test_expectations_recomputed_from_csv():
    outcomes = verify_demo_db.verify(DuckBackend(), SPEC)
    assert [o.check for o in outcomes if not o.ok] == []
    assert len(outcomes) == 1 + 2 * len(SPEC["tables"]) + len(SPEC["checks"]) + 1


def test_failed_query_and_wrong_value_fail_only_their_check():
    backend = DuckBackend()
    spec = json.loads(json.dumps(SPEC))
    spec["checks"]["sales_missing_quantity"]["expected"] = ["211"]
    spec["checks"]["broken"] = {"sql": "SELECT missing FROM sales", "expected": ["1"]}
    failed = {o.check: o for o in verify_demo_db.verify(backend, spec) if not o.ok}
    assert set(failed) == {"sales_missing_quantity", "broken"}
    assert failed["broken"].error and failed["sales_missing_quantity"].actual == ["210"]


@pytest.mark.parametrize(
    ("actual", "expected", "result"),
    [
        (["1268254502.4"], ["1268254502.40"], True),
        (["12", "861"], ["12", "861"], True),
        (["2023-01-01"], ["2023-01-01"], True),
        (["12"], ["12", "861"], False),
        (["NULL"], ["0"], False),
    ],
)
def test_same_compares_numbers_as_decimal(actual, expected, result):
    assert verify_demo_db.same(actual, expected) is result


def test_clickhouse_types():
    assert ch.clickhouse_type("VARCHAR") == "Nullable(String)"
    assert ch.clickhouse_type("DECIMAL(18,2)") == "Nullable(Decimal(18, 2))"
    assert ch.clickhouse_type("DATE") == "Nullable(Date)"
    with pytest.raises(ch.ClickHouseError, match="INTERVAL"):
        ch.clickhouse_type("INTERVAL")
    assert ch.create_table_sql("djedai_demo", "a`b", [("x", "BIGINT")]) == (
        "CREATE TABLE `djedai_demo`.`a\\`b` (`x` Nullable(Int64)) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )


def test_clickhouse_url_parsing_hides_password():
    client = ch.ClickHouseHttp.from_url(
        "http://djedai_loader:p%40ss%3Aw@127.0.0.1:58123", default_database=ch.DATABASE
    )
    assert (client.user, client.password, client.port) == ("djedai_loader", "p@ss:w", 58123)
    assert client.database == "djedai_demo"
    assert (
        "p@ss" not in repr(client)
        and client.describe() == "djedai_loader@127.0.0.1:58123/djedai_demo"
    )
    for url in ("postgresql://u:p@h/db", "http://u@h:8123"):
        with pytest.raises(ch.ClickHouseError):
            ch.ClickHouseHttp.from_url(url)


@pytest.fixture
def fake_clickhouse():
    """HTTP-сервер, принимающий запросы как ClickHouse и хранящий число строк вставок."""
    calls, counts = [], {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            sql = params.get("query") or body.decode()
            calls.append({"params": params, "sql": sql, "auth": self.headers["Authorization"]})
            if sql.startswith("INSERT"):
                with tempfile.NamedTemporaryFile(suffix=".parquet") as file:
                    file.write(body)
                    file.flush()
                    counts[sql.split()[2]] = duckdb.sql(
                        f"SELECT count(*) FROM read_parquet('{file.name}')"
                    ).fetchone()[0]
                reply = b""
            elif sql.startswith("SELECT count()"):
                reply = f"{counts[sql.split()[-1]]}\n".encode()
            elif "fail" in sql:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Code: 62. DB::Exception: Syntax error")
                return
            else:
                reply = b""
            self.send_response(200)
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://djedai_loader:secret%21@127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()


def test_seed_clickhouse_recreates_tables_from_parquet(fake_clickhouse):
    url, calls = fake_clickhouse
    counts = ch.seed(ch.ClickHouseHttp.from_url(url, default_database=ch.DATABASE), DATA_DIR)
    assert counts == {name: info["rows"] for name, info in SPEC["tables"].items()}
    token = base64.b64encode(b"djedai_loader:secret!").decode()
    assert {call["auth"] for call in calls} == {f"Basic {token}"}
    assert all(call["params"]["database"] == "djedai_demo" for call in calls)
    assert all("secret" not in json.dumps(call["params"]) for call in calls)
    sales = [call["sql"] for call in calls if "`sales`" in call["sql"]]
    assert sales[0] == "DROP TABLE IF EXISTS `djedai_demo`.`sales`"
    assert sales[1].startswith("CREATE TABLE `djedai_demo`.`sales` (`sale_id` Nullable(String)")
    assert sales[2] == "INSERT INTO `djedai_demo`.`sales` FORMAT Parquet"


def test_clickhouse_errors_carry_server_text_without_password(fake_clickhouse):
    url, _ = fake_clickhouse
    with pytest.raises(ch.ClickHouseError) as error:
        ch.ClickHouseHttp.from_url(url).query("SELECT fail")
    assert "Syntax error" in str(error.value) and "secret" not in str(error.value)
    with pytest.raises(ch.ClickHouseError, match="недоступен"):
        ch.ClickHouseHttp.from_url("http://u:secret@127.0.0.1:1").query("SELECT 1")
