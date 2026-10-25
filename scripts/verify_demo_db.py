"""Сверка демо-PostgreSQL и демо-ClickHouse с контрольными значениями (#100).

Под read-only ролями ``djedai_reader`` проверяет состав таблиц, колонки, число строк и
агрегаты из ``eval/demo_db_expectations.json``, а также что читатель не может создать
таблицу. PostgreSQL читается через DuckDB ``ATTACH`` — тем же путём, что в DjedAI;
ClickHouse — через его HTTP-интерфейс в собственном диалекте. Можно проверить одну БД:
PostgreSQL готов раньше полного demo-профиля.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

import duckdb
from demo_dataset import DATABASE, ROOT

from djedai.data.clickhouse import ClickHouseError, ClickHouseHttp
from djedai.data.postgres import PostgresError, parse_dsn
from djedai.data.typed_views import _lit

EXPECTATIONS = ROOT / "eval" / "demo_db_expectations.json"
SYNTHETIC = ROOT / "eval" / "synthetic_expectations.json"
PROBE_TABLE = "djedai_write_probe"


class Backend(Protocol):
    name: str

    def rows(self, sql: str) -> list[list[str]]: ...
    def tables(self) -> list[str]: ...
    def columns(self, table: str) -> list[str]: ...
    def can_write(self) -> bool: ...


def _text(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class PostgresBackend:
    name = "postgres"

    def __init__(self, dsn: str) -> None:
        self._info = parse_dsn(dsn)
        self._connection = duckdb.connect()
        self._connection.execute("LOAD postgres")
        self._connection.execute(
            "CREATE TEMPORARY SECRET demo_pg_secret "
            f"(TYPE postgres, PASSWORD {_lit(self._info.password)})"
        )
        self._connection.execute(
            f"ATTACH {_lit(self._info.libpq())} AS demo_pg (TYPE postgres, SECRET demo_pg_secret)"
        )
        self._connection.execute("USE demo_pg.public")

    def rows(self, sql: str) -> list[list[str]]:
        return [[_text(value) for value in row] for row in self._connection.execute(sql).fetchall()]

    def tables(self) -> list[str]:
        return [row[0] for row in self.rows(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = 'demo_pg' AND schema_name = 'public' ORDER BY table_name"
        )]  # fmt: skip

    def columns(self, table: str) -> list[str]:
        return [row[0] for row in self.rows(
            "SELECT column_name FROM duckdb_columns() WHERE database_name = 'demo_pg' "
            f"AND schema_name = 'public' AND table_name = {_lit(table)} ORDER BY column_index"
        )]  # fmt: skip

    def can_write(self) -> bool:
        # сессия без клиентского READ ONLY: запрет должен идти от самой роли
        self._connection.execute(
            f"ATTACH {_lit(self._info.libpq(read_only=False))} AS demo_pg_rw "
            "(TYPE postgres, SECRET demo_pg_secret)"
        )
        target = f"demo_pg_rw.public.{PROBE_TABLE}"
        try:
            self._connection.execute(f"CREATE TABLE {target} (x INTEGER)")
        except duckdb.Error:
            return False
        self._connection.execute(f"DROP TABLE {target}")
        return True

    def close(self) -> None:
        self._connection.close()


class ClickHouseBackend:
    name = "clickhouse"

    def __init__(self, url: str) -> None:
        self._client = ClickHouseHttp.from_url(url, default_database=DATABASE)

    def rows(self, sql: str) -> list[list[str]]:
        text = self._client.query(f"{sql} FORMAT TabSeparated")
        return [line.split("\t") for line in text.splitlines()]

    def tables(self) -> list[str]:
        return [row[0] for row in self.rows(
            "SELECT name FROM system.tables WHERE database = currentDatabase() ORDER BY name"
        )]  # fmt: skip

    def columns(self, table: str) -> list[str]:
        quoted = "'" + table.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return [row[0] for row in self.rows(
            "SELECT name FROM system.columns WHERE database = currentDatabase() "
            f"AND table = {quoted} ORDER BY position"
        )]  # fmt: skip

    def can_write(self) -> bool:
        try:
            self._client.query(f"CREATE TABLE {PROBE_TABLE} (x UInt8) ENGINE = Memory")
        except ClickHouseError:
            return False
        self._client.query(f"DROP TABLE {PROBE_TABLE}")
        return True

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class Outcome:  # одна проверка одной БД
    check: str
    expected: list[str]
    actual: list[str] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and same(self.actual or [], self.expected)


def same(actual: list[str], expected: list[str]) -> bool:
    """Числа сравниваются как Decimal (ClickHouse печатает ``1268254502.4``), прочее — строкой."""
    if len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected, strict=True):
        try:
            if Decimal(left) != Decimal(right):
                return False
        except InvalidOperation:
            if left != right:
                return False
    return True


def verify(backend: Backend, spec: dict) -> list[Outcome]:
    """Все проверки ``spec`` для одной БД; ошибка запроса — неуспех этой проверки."""
    tables = spec["tables"]
    plan: list[tuple[str, list[str], object]] = [
        ("tables", sorted(tables), backend.tables),
    ]
    for table, info in tables.items():
        plan.append((f"{table}.columns", info["columns"], lambda t=table: backend.columns(t)))
        plan.append((
            f"{table}.rows", [str(info["rows"])],
            lambda t=table: backend.rows(f"SELECT count(*) FROM {t}")[0],
        ))  # fmt: skip
    for name, check in spec["checks"].items():
        plan.append((name, check["expected"], lambda sql=check["sql"]: backend.rows(sql)[0]))
    plan.append(("reader_read_only", ["read-only"], lambda: [
        "writable" if backend.can_write() else "read-only"
    ]))  # fmt: skip
    outcomes = []
    for name, expected, run in plan:
        try:
            outcomes.append(Outcome(name, expected, run()))
        except (duckdb.Error, ClickHouseError, IndexError) as error:
            outcomes.append(Outcome(name, expected, None, str(error).splitlines()[0]))
    return outcomes


def resolve(document: dict, path: str) -> str:
    """Значение по пути ``a.b.0.c``; у метрик берётся ``expected_value``."""
    node: object = document
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    if isinstance(node, dict):
        node = node["expected_value"]
    return str(node)


def reference_mismatches(spec: dict, synthetic: dict) -> list[str]:
    """Расхождения эталонов демо-БД с ``synthetic_expectations.json``."""
    problems = []
    pairs = [
        (f"tables.{name}.rows", [str(info["rows"])], [info["reference"]])
        for name, info in spec["tables"].items()
        if "reference" in info
    ]
    pairs += [
        (f"checks.{name}", check["expected"], check["reference"])
        for name, check in spec["checks"].items()
        if "reference" in check
    ]
    for where, expected, references in pairs:
        referenced = [resolve(synthetic, path) for path in references]
        if not same(expected, referenced):
            problems.append(f"{where}: {expected} != {referenced} ({', '.join(references)})")
    return problems


def _report(results: dict[str, list[Outcome]]) -> bool:
    names = [outcome.check for outcome in next(iter(results.values()))]
    header = ["проверка", "эталон", *results]
    lines = [header]
    passed = True
    for index, name in enumerate(names):
        row = [name, ", ".join(results[next(iter(results))][index].expected)]
        for outcomes in results.values():
            outcome = outcomes[index]
            passed &= outcome.ok
            shown = outcome.error or ", ".join(outcome.actual or [])
            row.append(("ok  " if outcome.ok else "FAIL ") + shown)
        lines.append(row)
    widths = [max(len(line[i]) for line in lines) for i in range(len(header))]
    for line in lines:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)))
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сверка демо-БД с контрольными значениями.")
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("DJEDAI_DEMO_PG_READER_DSN"),
        help="DSN djedai_reader в PostgreSQL (по умолчанию DJEDAI_DEMO_PG_READER_DSN)",
    )
    parser.add_argument(
        "--ch-url",
        default=os.environ.get("DJEDAI_DEMO_CH_READER_URL"),
        help="http://djedai_reader:<пароль>@host:port (по умолчанию DJEDAI_DEMO_CH_READER_URL)",
    )
    parser.add_argument("--expectations", type=Path, default=EXPECTATIONS)
    args = parser.parse_args(argv)
    if not args.pg_dsn and not args.ch_url:
        parser.error("укажите --pg-dsn и/или --ch-url (или переменные окружения)")
    spec = json.loads(args.expectations.read_text(encoding="utf-8"))
    problems = reference_mismatches(spec, json.loads(SYNTHETIC.read_text(encoding="utf-8")))
    if problems:
        print("Эталоны демо-БД расходятся с synthetic_expectations.json:", *problems, sep="\n")
        return 1
    results: dict[str, list[Outcome]] = {}
    for name, target, factory in (
        ("postgres", args.pg_dsn, PostgresBackend),
        ("clickhouse", args.ch_url, ClickHouseBackend),
    ):
        if not target:
            continue
        try:
            backend = factory(target)
        except (PostgresError, ClickHouseError, duckdb.Error) as error:
            print(f"{name}: {str(error).splitlines()[0]}")
            return 1
        try:
            results[name] = verify(backend, spec)
        finally:
            backend.close()
    passed = _report(results)
    print("status:", "passed" if passed else "failed", f"({', '.join(results)})")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
