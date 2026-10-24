"""Общий датасет демо-БД (#100): синтетика через конвейер приёма DjedAI.

PostgreSQL и ClickHouse наполняются из одних и тех же typed views, поэтому в обеих БД
одинаковые таблицы, колонки и значения. Эталоны для сверки — ``eval/demo_db_expectations.json``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
# Гарантируем доступность модулей из src/ при прямом вызове
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from djedai.data.csv_ingest import load_raw_csvs  # noqa: E402
from djedai.data.typed_views import create_typed_views  # noqa: E402

DATA_DIR = ROOT / "tests" / "fixtures" / "synthetic"
DATABASE = "djedai_demo"


def load_typed_dataset(connection: duckdb.DuckDBPyConnection, data_dir: Path) -> list[str]:
    """Загружает CSV в ``connection`` и строит typed views; возвращает их имена."""
    report = load_raw_csvs(connection, sorted(data_dir.glob("*.csv")))
    if report.errors:
        raise SystemExit("\n".join(str(error) for error in report.errors))
    views = create_typed_views(connection)
    if views.errors:
        raise SystemExit("\n".join(str(error) for error in views.errors))
    return [view.view_name for view in views.views]
