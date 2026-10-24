"""Загрузка синтетического датасета в демо-ClickHouse (#100).

Те же typed views, что и для PostgreSQL (``demo_dataset.load_typed_dataset``), выгружаются
в Parquet и отправляются в HTTP-интерфейс ClickHouse как ``INSERT … FORMAT Parquet``.
Типы колонок переводятся из DuckDB в ClickHouse; повторный запуск пересоздаёт таблицы.
Подключение — под ``djedai_loader`` из ``infra/clickhouse/roles.sql``, чтение в DjedAI — под
``djedai_reader``. Клиентская библиотека не нужна: адаптер ClickHouse (#61) не требуется.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
from demo_dataset import DATA_DIR, DATABASE, load_typed_dataset

from djedai.data.typed_views import _ident, _lit

TIMEOUT = 60
# типы typed views → ClickHouse; DECIMAL(p,s) разбирается отдельно
CH_TYPES = {
    "VARCHAR": "String",
    "BIGINT": "Int64",
    "INTEGER": "Int32",
    "DOUBLE": "Float64",
    "DATE": "Date",
    "TIMESTAMP": "DateTime64(6)",
    "BOOLEAN": "Bool",
}
_DECIMAL = re.compile(r"DECIMAL\((\d+),\s*(\d+)\)")


class ClickHouseError(RuntimeError):
    """Ошибка ClickHouse или подключения; текст не содержит пароля."""


@dataclass(frozen=True)
class ClickHouseHttp:  # HTTP-интерфейс ClickHouse; пароль не попадает в repr и URL запроса
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str = DATABASE
    secure: bool = False

    @classmethod
    def from_url(cls, url: str) -> ClickHouseHttp:
        """Разбирает ``http://user:password@host:8123[/database]``; спецсимволы пароля — %XX."""
        parts = urllib.parse.urlsplit(url.strip())
        if parts.scheme not in ("http", "https"):
            raise ClickHouseError("адрес ClickHouse должен начинаться с http:// или https://")
        if not parts.username or not parts.password:
            raise ClickHouseError("в адресе ClickHouse нужны пользователь и пароль")
        return cls(
            host=parts.hostname or "localhost",
            port=parts.port or (8443 if parts.scheme == "https" else 8123),
            user=urllib.parse.unquote(parts.username),
            password=urllib.parse.unquote(parts.password),
            database=parts.path.strip("/") or DATABASE,
            secure=parts.scheme == "https",
        )

    def describe(self) -> str:
        """Адрес без пароля: ``user@host:port/database``."""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"

    def query(self, sql: str, data: bytes | None = None) -> str:
        """Выполняет ``sql``; при ``data`` запрос идёт в URL, а тело — входные данные."""
        params = {"database": self.database}
        if data is not None:
            params["query"] = sql
        scheme = "https" if self.secure else "http"
        url = f"{scheme}://{self.host}:{self.port}/?{urllib.parse.urlencode(params)}"
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        request = urllib.request.Request(
            url,
            data=sql.encode() if data is None else data,
            headers={"Authorization": f"Basic {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read().decode()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace").strip()
            raise ClickHouseError(f"ClickHouse {self.describe()}: {detail}") from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            message = f"не удалось подключиться к {self.describe()}: {reason}"
            raise ClickHouseError(message) from None


def ch_ident(name: str) -> str:
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def clickhouse_type(duckdb_type: str) -> str:
    """Тип колонки ClickHouse для типа DuckDB; все колонки typed views допускают NULL."""
    decimal = _DECIMAL.fullmatch(duckdb_type)
    if decimal:
        base = f"Decimal({decimal[1]}, {decimal[2]})"
    elif duckdb_type in CH_TYPES:
        base = CH_TYPES[duckdb_type]
    else:
        raise ClickHouseError(f"тип {duckdb_type} не поддерживается демо-загрузкой")
    return f"Nullable({base})"


def create_table_sql(database: str, table: str, columns: list[tuple[str, str]]) -> str:
    """``CREATE TABLE`` MergeTree без ключа сортировки: демо-таблицы малы."""
    body = ", ".join(f"{ch_ident(name)} {clickhouse_type(kind)}" for name, kind in columns)
    return (
        f"CREATE TABLE {ch_ident(database)}.{ch_ident(table)} ({body}) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )


def seed(client: ClickHouseHttp, data_dir: Path) -> dict[str, int]:
    """Пересоздаёт таблицы в базе клиента; возвращает ``таблица → число строк``."""
    connection = duckdb.connect()
    try:
        views = load_typed_dataset(connection, data_dir)
        counts = {}
        with tempfile.TemporaryDirectory() as folder:
            for view in views:
                columns = [
                    (row[0], row[1])
                    for row in connection.execute(f"DESCRIBE {_ident(view)}").fetchall()
                ]
                parquet = Path(folder) / f"{view}.parquet"
                connection.execute(f"COPY {_ident(view)} TO {_lit(str(parquet))} (FORMAT parquet)")
                target = f"{ch_ident(client.database)}.{ch_ident(view)}"
                client.query(f"DROP TABLE IF EXISTS {target}")
                client.query(create_table_sql(client.database, view, columns))
                client.query(f"INSERT INTO {target} FORMAT Parquet", data=parquet.read_bytes())
                counts[view] = int(client.query(f"SELECT count() FROM {target}"))
        return counts
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка синтетики в демо-ClickHouse.")
    parser.add_argument(
        "--url",
        default=os.environ.get("DJEDAI_DEMO_CH_LOADER_URL"),
        help="http://djedai_loader:<пароль>@host:port (по умолчанию из DJEDAI_DEMO_CH_LOADER_URL)",
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="папка с CSV")
    args = parser.parse_args()
    if not args.url:
        parser.error("укажите --url или DJEDAI_DEMO_CH_LOADER_URL")
    try:
        counts = seed(ClickHouseHttp.from_url(args.url), args.data_dir)
    except ClickHouseError as error:
        sys.exit(str(error))
    except duckdb.Error as error:
        sys.exit(f"Не удалось подготовить данные: {error}")
    for table, rows in counts.items():
        print(f"{table}: {rows} строк")


if __name__ == "__main__":
    main()
