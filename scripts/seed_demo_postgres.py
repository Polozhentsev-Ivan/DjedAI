"""Загрузка синтетического датасета в демо-PostgreSQL (#100, ранняя PG-часть).

CSV из ``tests/fixtures/synthetic`` проходят тот же конвейер, что и при загрузке в
DjedAI (``csv_ingest`` → ``typed_views``), и записываются в схему ``public`` как таблицы с
типами БД. Повторный запуск пересоздаёт таблицы. Подключение — под ролью-владельцем
(``djedai_loader`` из ``infra/postgres/roles.sql``), чтение в DjedAI — под ``djedai_reader``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
# Гарантируем доступность модулей из src/ при прямом вызове
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from djedai.data.csv_ingest import load_raw_csvs  # noqa: E402
from djedai.data.postgres import PostgresError, parse_dsn  # noqa: E402
from djedai.data.typed_views import _ident, _lit, create_typed_views  # noqa: E402

DATA_DIR = ROOT / "tests" / "fixtures" / "synthetic"
ALIAS = "demo_pg"


def seed(dsn: str, data_dir: Path, schema: str = "public") -> dict[str, int]:
    """Пересоздаёт таблицы в ``schema``; возвращает ``таблица → число строк``."""
    info = parse_dsn(dsn)
    connection = duckdb.connect()
    try:
        report = load_raw_csvs(connection, sorted(data_dir.glob("*.csv")))
        if report.errors:
            raise SystemExit("\n".join(str(error) for error in report.errors))
        views = create_typed_views(connection)
        if views.errors:
            raise SystemExit("\n".join(str(error) for error in views.errors))
        connection.execute("LOAD postgres")
        connection.execute(
            "CREATE TEMPORARY SECRET demo_pg_secret "
            f"(TYPE postgres, PASSWORD {_lit(info.password)})"
        )
        connection.execute(
            f"ATTACH {_lit(info.libpq(read_only=False))} AS {ALIAS} "
            "(TYPE postgres, SECRET demo_pg_secret)"
        )
        counts = {}
        for view in views.views:
            target = f"{ALIAS}.{_ident(schema)}.{_ident(view.view_name)}"
            connection.execute(f"DROP TABLE IF EXISTS {target}")
            connection.execute(f"CREATE TABLE {target} AS SELECT * FROM {_ident(view.view_name)}")
            counts[view.view_name] = connection.execute(
                f"SELECT count(*) FROM {target}"
            ).fetchone()[0]
        return counts
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка синтетики в демо-PostgreSQL.")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DJEDAI_DEMO_PG_LOADER_DSN"),
        help="DSN роли-владельца (по умолчанию из DJEDAI_DEMO_PG_LOADER_DSN)",
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="папка с CSV")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("укажите --dsn или DJEDAI_DEMO_PG_LOADER_DSN")
    try:
        counts = seed(args.dsn, args.data_dir)
    except PostgresError as error:
        sys.exit(str(error))
    except duckdb.Error as error:
        sys.exit(f"Не удалось загрузить данные: {error}")
    for table, rows in counts.items():
        print(f"{table}: {rows} строк")


if __name__ == "__main__":
    main()
