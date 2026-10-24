"""HTTP-сервер, отвечающий на запросы адаптера ClickHouse (#61) данными из DuckDB.

Понимает только то, что отправляет ``djedai.data.clickhouse``: ``getSetting('readonly')``,
``system.settings``/``system.tables``/``system.columns``, ``count()`` и ``SELECT … FORMAT
Parquet``. Проверяет пароль, режим ``readonly`` и ``max_result_rows`` с
``result_overflow_mode``: при ``break`` отдаёт часть строк без ошибки, как настоящий сервер.
Таблицы лежат в схеме DuckDB с именем базы ClickHouse.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from demo_dataset import DATA_DIR, DATABASE, load_typed_dataset  # noqa: E402
from seed_demo_clickhouse import clickhouse_type  # noqa: E402

_FROM = re.compile(r"FROM `([^`]+)`\.`([^`]+)`")


class FakeClickHouse:
    """Запускается как контекстный менеджер; ``url`` — строка подключения читателя."""

    def __init__(
        self,
        *,
        database: str = DATABASE,
        user: str = "djedai_reader",
        password: str = "reader-secret!",
        readonly: str = "2",
        port: int = 0,
    ) -> None:
        self.database, self.user, self.password = database, user, password
        self.readonly = readonly  # профиль пользователя: "0", "1" или "2"
        self.port = port  # 0 — свободный порт
        self.honour_readonly = True  # False — сервер «забывает» readonly из запроса
        self.table_stats = True  # False — total_rows/total_bytes равны NULL, как у VIEW
        # профиль пользователя на сервере; настройки запроса его переопределяют (кроме readonly=1)
        self.profile: dict[str, str] = {"result_overflow_mode": "throw", "limit": "0"}
        self.before_export: Callable[[str], None] | None = None  # вызов перед выгрузкой таблицы
        self.export_rows: int | None = None  # отдать не больше N строк, сообщая полный count()
        self.calls: list[dict] = []
        self.connection = duckdb.connect()
        self.connection.execute(f'CREATE SCHEMA "{database}"')
        self.connection.execute("CREATE MACRO toString(x) AS CAST(x AS VARCHAR)")
        self._server: ThreadingHTTPServer | None = None

    def load_demo(self, *, stores: int | None = None) -> FakeClickHouse:
        """Демо-датасет #100; ``stores`` — оставить только первые N магазинов."""
        for view in load_typed_dataset(self.connection, DATA_DIR):
            limit = f" ORDER BY store_id LIMIT {stores}" if view == "stores" and stores else ""
            self.add_table(view, f'SELECT * FROM "{view}"{limit}')
        return self

    def add_table(self, name: str, select: str) -> None:
        self.connection.execute(f'CREATE TABLE "{self.database}"."{name}" AS {select}')

    @property
    def url(self) -> str:
        assert self._server is not None
        password = urllib.parse.quote(self.password, safe="")
        port = self._server.server_port
        return f"http://{self.user}:{password}@127.0.0.1:{port}/{self.database}"

    def __enter__(self) -> FakeClickHouse:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
                body = self.rfile.read(int(self.headers["Content-Length"]))
                sql = params.get("query") or body.decode()
                fake.calls.append({"params": params, "sql": sql})
                status, reply = fake.answer(sql, params, self.headers.get("Authorization", ""))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        self.connection.close()

    def answer(self, sql: str, params: dict[str, str], auth: str) -> tuple[int, bytes]:
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        if auth != f"Basic {token}":
            return 516, (
                f"Code: 516. DB::Exception: {self.user}: Authentication failed: password is "
                "incorrect, or there is no user with such name. (AUTHENTICATION_FAILED)"
            ).encode()
        if "readonly" in params and self.readonly == "1":
            return 500, b"Code: 164. DB::Exception: Cannot modify 'readonly' setting. (READONLY)"
        con = self.connection.cursor()
        if "getSetting('readonly')" in sql:
            value = params.get("readonly", self.readonly) if self.honour_readonly else "0"
            return 200, f"{value}\n".encode()
        effective = {**self.profile, **params}
        if "FROM system.settings" in sql:
            names = re.findall(r"'([a-z_]+)'", sql)
            lines = [json.dumps([n, effective[n]]) for n in names if n in effective]
            return 200, "".join(f"{line}\n" for line in lines).encode()
        if "FROM system.tables" in sql:
            rows = con.execute(
                "SELECT table_name, estimated_size FROM duckdb_tables() WHERE schema_name = ? "
                "ORDER BY table_name",
                [self.database],
            ).fetchall()
            lines = []
            for name, _ in rows:
                count = self._count(con, name)
                stats = [str(count), str(count * 16)] if self.table_stats else [None, None]
                lines.append(json.dumps([name, *stats]))
            return 200, "".join(f"{line}\n" for line in lines).encode()
        if "FROM system.columns" in sql:
            rows = con.execute(
                "SELECT table_name, column_name, data_type FROM duckdb_columns() "
                "WHERE schema_name = ? ORDER BY table_name, column_index",
                [self.database],
            ).fetchall()
            lines = [json.dumps([t, c, _ch_type(d)]) for t, c, d in rows]
            return 200, "".join(f"{line}\n" for line in lines).encode()
        source = _FROM.search(sql)
        if source is None or source[1] != self.database:
            return 400, b"Code: 62. DB::Exception: Syntax error"
        if sql.startswith("SELECT count()"):
            return 200, f"{self._count(con, source[2])}\n".encode()
        if not sql.endswith(" FORMAT Parquet"):
            return 400, b"Code: 62. DB::Exception: Syntax error"
        if self.before_export is not None:
            self.before_export(source[2])
        rows = self._count(con, source[2])
        limit = int(effective.get("max_result_rows", "0"))
        keep = self.export_rows
        if limit and rows > limit:
            if effective["result_overflow_mode"] != "break":
                return 500, (
                    f"Code: 396. DB::Exception: Limit for result exceeded, max rows: {limit}. "
                    "(TOO_MANY_ROWS_OR_BYTES)"
                ).encode()
            keep = limit if keep is None else min(keep, limit)
        query = sql.removesuffix(" FORMAT Parquet").replace(f"`{self.database}`.", "")
        query = query.replace("`", '"').replace("FROM ", f'FROM "{self.database}".', 1)
        if keep is not None:
            query += f" LIMIT {keep}"
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "out.parquet"
            con.execute(f"COPY ({query}) TO '{target}' (FORMAT parquet)")
            return 200, target.read_bytes()

    def _count(self, con: duckdb.DuckDBPyConnection, table: str) -> int:
        return con.execute(f'SELECT count(*) FROM "{self.database}"."{table}"').fetchone()[0]


def _ch_type(duckdb_type: str) -> str:
    return "Nullable(Int128)" if duckdb_type == "HUGEINT" else clickhouse_type(duckdb_type)
