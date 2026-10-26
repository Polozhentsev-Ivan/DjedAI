"""Прогон данных организаторов HK_Datasets без LLM: загрузка, типы, профиль, ключевые запросы.

Из корня репозитория:
uv run --locked python scripts/check_hk_datasets.py
uv run --locked python scripts/check_hk_datasets.py --stages postgres,clickhouse \
    --reference artifacts/hk-check/<run>/reference.duckdb
uv run --locked python scripts/check_hk_datasets.py --compare before.json after.json

Эталон не использует код платформы: число строк считает csv.reader, контрольные ответы —
отдельная DuckDB, прочитавшая файлы с типами из README организаторов (манифест).
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import os
import resource
import statistics
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402
from hk_datasets import (  # noqa: E402
    CACHE_DIR,
    MANIFEST_PATH,
    DownloadError,
    download,
    load_manifest,
    manifest_files,
    sha256,
)

from djedai.analytics.metrics import MetricContext  # noqa: E402
from djedai.analytics.registry import execute_tool  # noqa: E402
from djedai.contracts.catalog import DatasetCatalog  # noqa: E402
from djedai.data.clickhouse import ClickHouseHttp, ClickHouseLimits  # noqa: E402
from djedai.data.postgres import attach_postgres, parse_dsn  # noqa: E402
from djedai.data.service import (  # noqa: E402
    CsvSource,
    connect_clickhouse_dataset,
    connect_postgres_dataset,
    ingest_csv_dataset,
)
from djedai.data.typed_views import _ident, _lit  # noqa: E402

STAGES = ("download", "reference", "csv", "queries", "postgres", "clickhouse")
DEFAULT_STAGES = ("download", "reference", "csv", "queries")
REPEATS = 3
# Типы README → допустимые семантические типы каталога.
ACCEPTED_TYPES = {
    "text": {"text", "category", "id"},
    "integer": {"integer"},
    "decimal": {"float", "money", "percent"},
    "date": {"date"},
    "datetime": {"datetime"},
}
SQL_TYPES = {
    "text": "VARCHAR",
    "integer": "BIGINT",
    "decimal": "DECIMAL(18,3)",
    "date": "DATE",
    "datetime": "TIMESTAMP",
}
# Колонки ключевых запросов: время, категория, мера, сегмент (имена из README).
QUERY_PLAN = {
    "01_Продажи.csv": (
        "Дата операции (МСК)", "Канал продажи", "Количество, шт.", "Тип операции",
    ),
    "02_Техническая_поддержка.csv": (
        "Создано (МСК)", "Канал обращения", "Оценка клиента", "Приоритет",
    ),
    "03_Задачи_команд.csv": (
        "Создано (МСК)", "Тип задачи", "Фактические трудозатраты, ч", "Статус",
    ),
    "04_Доставка.csv": (
        "Принято (МСК)", "Перевозчик", "Стоимость доставки, руб.", "Способ доставки",
    ),
    "05_События_приложения.csv": (
        "Время события (МСК)", "Событие", "Время обработки, мс", "Платформа",
    ),
    "06_Рекламные_кампании.csv": ("Дата", "Площадка", "Расходы, руб.", "Аудитория"),
    "07_Заявки_на_закупку.csv": (
        "Подана (МСК)", "Категория закупки", "Запрошенная сумма, руб.", "Срочность",
    ),
    "08_Фоновые_задания.csv": ("Начало (МСК)", "Статус", "Обработано записей", "Окружение"),
    "09_Лекарственные_препараты.csv": (
        "Дата", "Тип_Лекарства", "Сколько_Купили", "Действующее_Вещество",
    ),
}  # fmt: skip
CORRELATIONS = {"06_Рекламные_кампании.csv": ("Показы", "Клики")}


class CheckFailure(RuntimeError):
    """Прогон не смог выполнить этап; расхождения с эталоном фиксируются в отчёте, а не так."""


def stem(file: str) -> str:
    return file.removesuffix(".csv")


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def in_child[T](function: Callable[..., T], *args: Any) -> T:
    """Отдельный процесс: пиковая память этапа не смешивается с предыдущими."""
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        return pool.apply(function, args)


def normalize(value: Any) -> Any:
    if isinstance(value, Decimal | float):
        return round(float(value), 4)
    if isinstance(value, datetime | date):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def normalize_rows(rows: list[list[Any]] | list[tuple]) -> list[list[Any]]:
    return [[normalize(value) for value in row] for row in rows]


# --- download / reference ---------------------------------------------------------------


def stage_download(manifest: dict, cache: Path) -> dict:
    files = {}
    for name, file in manifest_files(manifest).items():
        started = time.perf_counter()
        try:
            path = download(file, cache)
        except DownloadError as error:
            raise CheckFailure(str(error)) from error
        files[name] = {
            "path": str(path),
            "size": file.size,
            "blob": file.blob,
            "sha256": sha256(path),
            "seconds": round(time.perf_counter() - started, 2),
        }
    return files


def count_csv(path: Path) -> dict:
    """Независимый подсчёт записей и пустых значений стандартным csv.reader."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, delimiter=";")
        header = next(reader)
        empty = [0] * len(header)
        widths: set[int] = set()
        rows = 0
        for row in reader:
            rows += 1
            widths.add(len(row))
            for index, value in enumerate(row):
                if not value.strip():
                    empty[index] += 1
    return {
        "rows": rows,
        "columns": len(header),
        "header": header,
        "row_widths": sorted(widths),
        "empty": dict(zip(header, empty, strict=True)),
    }


def reference_table(table: dict) -> str:
    return f"ref_{stem(table['file'])[:2]}"


def build_reference(manifest: dict, cache: Path, database: Path) -> dict:
    """Эталонная DuckDB: типы из README, десятичная запятая, без кода платформы."""
    result = {}
    database.unlink(missing_ok=True)
    with duckdb.connect(str(database)) as connection:
        for table in manifest["tables"]:
            path = cache / table["file"]
            started = time.perf_counter()
            counted = count_csv(path)
            columns = ", ".join(
                f"{_lit(column['name'])}: {_lit(SQL_TYPES[column['type']])}"
                for column in table["columns"]
            )
            connection.execute(
                f"CREATE TABLE {reference_table(table)} AS SELECT * FROM read_csv("
                f"{_lit(str(path))}, delim=';', header=true, decimal_separator=',', "
                f"timestampformat='%Y-%m-%d %H:%M:%S', dateformat='%Y-%m-%d', "
                f"columns={{{columns}}})"
            )
            loaded = connection.execute(
                f"SELECT count(*) FROM {reference_table(table)}"
            ).fetchone()[0]
            names = [column["name"] for column in table["columns"]]
            result[table["file"]] = {
                **counted,
                "loaded_rows": loaded,
                "header_matches_readme": counted["header"] == names,
                "seconds": round(time.perf_counter() - started, 2),
            }
    return result


# --- platform pipelines ------------------------------------------------------------------


class StageClock:
    """Отметки ``progress`` штатного конвейера → длительность этапов по таблицам."""

    def __init__(self) -> None:
        self.marks: list[tuple[str, str, float]] = []

    def __call__(self, stage: str, _fraction: float, message: str) -> None:
        self.marks.append((stage, message, time.perf_counter()))

    def durations(self, end: float, tables: list[str] | None = None) -> list[dict]:
        """``tables`` — порядок файлов CSV: каждое «read» начинает следующую таблицу."""
        marks = self.marks + [("done", "", end)]
        result, index = [], -1
        for (stage, message, at), after in zip(marks, marks[1:], strict=False):
            index += stage == "read"
            table = tables[index] if tables and 0 <= index < len(tables) else None
            if message == "Поиск связей":
                table = None
            result.append(
                {"stage": stage, "table": table, "message": message,
                 "seconds": round(after[2] - at, 2)}
            )  # fmt: skip
        return result


def csv_job(paths: list[str], database: str) -> dict:
    clock = StageClock()
    started = time.perf_counter()
    sources = [CsvSource(Path(path), Path(path).name) for path in paths]
    try:
        catalog = ingest_csv_dataset(
            sources,
            Path(database),
            dataset_id="hk",
            name="HK_Datasets",
            created_at=datetime.now(UTC),
            progress=clock,
        )
    except Exception as error:  # отчёт фиксирует любой сбой загрузки
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "rss_mb": peak_rss_mb()}
    end = time.perf_counter()
    return {
        "ok": True,
        "seconds": round(end - started, 2),
        "stages": clock.durations(end, [Path(path).name for path in paths]),
        "rss_mb": peak_rss_mb(),
        "catalog": catalog.model_dump(mode="json", by_alias=True),
    }


def postgres_job(dsn: str, database: str) -> dict:
    clock = StageClock()
    started = time.perf_counter()
    try:
        catalog = connect_postgres_dataset(
            dsn, None, Path(database), dataset_id="hk_pg", name="HK_Datasets PostgreSQL",
            created_at=datetime.now(UTC), progress=clock,
        )  # fmt: skip
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "rss_mb": peak_rss_mb()}
    end = time.perf_counter()
    return {
        "ok": True,
        "seconds": round(end - started, 2),
        "stages": clock.durations(end),
        "rss_mb": peak_rss_mb(),
        "catalog": catalog.model_dump(mode="json", by_alias=True),
    }


def clickhouse_job(url: str, database: str, max_rows: int | None) -> dict:
    clock = StageClock()
    started = time.perf_counter()
    limits = ClickHouseLimits(max_rows=max_rows) if max_rows else None
    try:
        catalog = connect_clickhouse_dataset(
            url, None, Path(database), dataset_id="hk_ch", name="HK_Datasets ClickHouse",
            created_at=datetime.now(UTC), limits=limits, progress=clock,
        )  # fmt: skip
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "rss_mb": peak_rss_mb()}
    end = time.perf_counter()
    return {
        "ok": True,
        "seconds": round(end - started, 2),
        "stages": clock.durations(end),
        "rss_mb": peak_rss_mb(),
        "catalog": catalog.model_dump(mode="json", by_alias=True),
    }


def size_mb(path: Path) -> float:
    return round(path.stat().st_size / 1024 / 1024, 1) if path.exists() else 0.0


# --- типы и профиль ------------------------------------------------------------------


def find_table(catalog: DatasetCatalog, table: dict) -> Any:
    for candidate in catalog.tables:
        names = {candidate.original_name, Path(candidate.source.file or "").name}
        if table["file"] in names or stem(table["file"]) in names:
            return candidate
        if candidate.source.relation and candidate.source.relation.endswith(stem(table["file"])):
            return candidate
    return None


def check_catalog(manifest: dict, catalog: DatasetCatalog, reference: dict | None) -> dict:
    tables = {}
    for table in manifest["tables"]:
        found = find_table(catalog, table)
        if found is None:
            tables[table["file"]] = {"missing": True}
            continue
        columns = {column.original_name: column for column in found.columns}
        mismatches, losses = [], []
        for expected in table["columns"]:
            column = columns.get(expected["name"])
            if column is None:
                mismatches.append({"column": expected["name"], "expected": expected["type"]})
                continue
            accepted = ACCEPTED_TYPES[expected["type"]]
            if expected["name"] in table["category_codes"]:
                accepted = {"id", "category", "text"}
            if column.semantic_type not in accepted:
                mismatches.append(
                    {
                        "column": expected["name"],
                        "expected": expected["type"],
                        "actual": column.semantic_type,
                        "physical": column.physical_type,
                    }
                )
            if column.parse and column.parse.success_ratio < 1:
                losses.append(
                    {
                        "column": expected["name"],
                        "success_ratio": column.parse.success_ratio,
                        "examples": column.parse.failed_examples,
                    }
                )
        key = [columns[name].name for name in table["key"] if name in columns]
        expected_rows = table["rows"]
        counted = (reference or {}).get(table["file"], {}).get("rows")
        tables[table["file"]] = {
            "table_id": found.table_id,
            "row_count": found.row_count,
            "rows_match_readme": found.row_count == expected_rows,
            "rows_match_csv_reader": counted is None or found.row_count == counted,
            "columns": len(found.columns),
            "columns_match_readme": len(found.columns) == len(table["columns"]),
            "type_mismatches": mismatches,
            "parse_losses": losses,
            "semantic_types": {c.original_name: c.semantic_type for c in found.columns},
            "duplicate_rows": found.duplicate_rows,
            "primary_key_candidates": found.primary_key_candidates,
            "key_found": len(key) == 1 and key[0] in found.primary_key_candidates,
            "time_columns": found.time_columns,
        }
    relations = [
        {
            "from": f"{r.from_.table}.{r.from_.column}",
            "to": f"{r.to.table}.{r.to.column}",
            "overlap": r.overlap,
            "cardinality": r.cardinality,
            "confidence": r.confidence,
        }
        for r in catalog.relations
    ]
    self_refs = []
    for table in manifest["tables"]:
        found = find_table(catalog, table)
        if found is None:
            continue
        names = {column.original_name: column.name for column in found.columns}
        for source, target in table["self_refs"]:
            wanted = (f"{found.table_id}.{names[source]}", f"{found.table_id}.{names[target]}")
            self_refs.append(
                {
                    "table": table["file"],
                    "from": source,
                    "to": target,
                    "found": any((r["from"], r["to"]) in (wanted, wanted[::-1]) for r in relations),
                }
            )
    return {
        "tables": tables,
        "relations": relations,
        "self_references": self_refs,
        "warnings": catalog.warnings,
    }


# --- ключевые запросы ----------------------------------------------------------------


def query_specs(manifest: dict, catalog: DatasetCatalog) -> list[dict]:
    """Запросы по сценариям кейса: инструмент платформы и SQL эталона на ref_* таблице."""
    specs = []
    for table in manifest["tables"]:
        found = find_table(catalog, table)
        if found is None or table["file"] not in QUERY_PLAN:
            continue
        names = {column.original_name: column.name for column in found.columns}
        time_col, category, measure, segment = QUERY_PLAN[table["file"]]
        t, ref = found.table_id, reference_table(table)
        q = _ident

        def add(
            scenario: str, tool: str, params: dict, ref_sql: str | None, check: str,
            file: str = table["file"],
        ) -> None:  # fmt: skip
            specs.append(
                {
                    "table": file,
                    "scenario": scenario,
                    "tool": tool,
                    "params": params,
                    "reference_sql": ref_sql,
                    "check": check,
                }
            )

        add(
            "1. Сколько записей", "run_sql", {"sql": f"SELECT count(*) FROM {q(t)}"},
            f"SELECT count(*) FROM {ref}", "rows",
        )  # fmt: skip
        add(
            "1. За какой период",
            "run_sql",
            {"sql": f"SELECT min({q(names[time_col])}), max({q(names[time_col])}) FROM {q(t)}"},
            f"SELECT min({q(time_col)}), max({q(time_col)}) FROM {ref}",
            "rows",
        )
        add("1. Что передо мной", "describe_dataset", {}, None, "status")
        add(
            "2. Что означает поле", "describe_column", {"table": t, "column": names[category]},
            None, "status",
        )  # fmt: skip
        add(
            "2. Частые значения",
            "value_counts",
            {"table": t, "column": names[category], "top_n": 5},
            f"SELECT {q(category)}, count(*) AS n FROM {ref} GROUP BY 1 ORDER BY n DESC, 1 LIMIT 5",
            "value_counts",
        )
        add(
            "3. Динамика по месяцам",
            "time_aggregate",
            {"table": t, "time_col": names[time_col], "freq": "month", "agg": "count"},
            f"SELECT date_trunc('month', {q(time_col)}) AS m, count(*) FROM {ref} "
            f"WHERE {q(time_col)} IS NOT NULL GROUP BY 1 ORDER BY 1",
            "rows",
        )
        add(
            "5. Сумма по кварталам",
            "time_aggregate",
            {
                "table": t, "time_col": names[time_col], "measure": names[measure],
                "agg": "sum", "freq": "quarter",
            },
            f"SELECT date_trunc('quarter', {q(time_col)}) AS m, sum({q(measure)}) FROM {ref} "
            f"WHERE {q(time_col)} IS NOT NULL GROUP BY 1 ORDER BY 1",
            "rows",
        )  # fmt: skip
        key = [names[name] for name in table["key"]]
        add(
            "3. Повторяющиеся данные (ключ)",
            "find_duplicates",
            {"table": t, "columns": key, "limit": 10},
            f"SELECT count(*) FROM (SELECT 1 FROM {ref} GROUP BY "
            f"{', '.join(q(name) for name in table['key'])} HAVING count(*) > 1)",
            "duplicate_groups",
        )
        add(
            "3. Повторяющиеся данные (строки)", "find_duplicates", {"table": t, "limit": 10},
            f"SELECT count(*) FROM (SELECT * FROM {ref} GROUP BY ALL HAVING count(*) > 1)",
            "duplicate_groups",
        )  # fmt: skip
        add(
            "3. Аномалии",
            "detect_anomalies",
            {
                "table": t, "measure": names[measure], "time_col": names[time_col],
                "freq": "day", "limit": 10,
            },
            None,
            "status",
        )  # fmt: skip
        add(
            "доп. Сезонность",
            "seasonality",
            {"table": t, "time_col": names[time_col], "measure": names[measure], "freq": "month"},
            None,
            "status",
        )
        add(
            "доп. Сегменты",
            "segment_profile",
            {"table": t, "group_by": [names[segment]], "measures": [names[measure]]},
            None,
            "status",
        )
        add(
            "доп. Отличия сегментов",
            "compare_segments",
            {"table": t, "segment_col": names[segment], "a": None, "b": None,
             "measures": [names[measure]]},
            f"SELECT {q(segment)} FROM {ref} GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 2",
            "status",
        )  # fmt: skip
        for source, target in table["self_refs"]:
            add(
                "2. Что связывает записи",
                "run_sql",
                {
                    "sql": f"SELECT count(*) FROM {q(t)} a JOIN {q(t)} b "
                    f"ON a.{q(names[source])} = b.{q(names[target])}"
                },
                f"SELECT count(*) FROM {ref} a JOIN {ref} b ON a.{q(source)} = b.{q(target)}",
                "rows",
            )
        if table["file"] in CORRELATIONS:
            x, y = CORRELATIONS[table["file"]]
            add(
                "доп. Корреляция",
                "run_sql",
                {"sql": f"SELECT round(corr({q(names[x])}, {q(names[y])}), 4) FROM {q(t)}"},
                f"SELECT round(corr({q(x)}, {q(y)}), 4) FROM {ref}",
                "rows",
            )
    return specs


def run_query(spec: dict, connection, catalog: DatasetCatalog, reference) -> dict:
    params = dict(spec["params"])
    expected = None
    if spec["reference_sql"] is not None and reference is not None:
        expected = normalize_rows(reference.execute(spec["reference_sql"]).fetchall())
    if spec["tool"] == "compare_segments":
        if expected is None or len(expected) < 2:
            return {**spec, "status": "skipped", "match": None}
        params["a"], params["b"] = expected[0][0], expected[1][0]
        expected = None
    allowed = frozenset(table.table_id for table in catalog.tables)
    timings, result = [], None
    for _ in range(REPEATS):
        started = time.perf_counter()
        result = execute_tool(
            spec["tool"], params, connection=connection, allowed_tables=allowed,
            semantics=MetricContext(catalog, {}),
        )  # fmt: skip
        timings.append(round(time.perf_counter() - started, 3))
    actual, match = None, None
    if result.status == "empty" and spec["check"] == "duplicate_groups" and expected is not None:
        actual, match = [[0]], [[0]] == expected
    if result.status == "ok" and result.data is not None:
        actual = normalize_rows(result.data.rows)
        facts = {f.kind: f.values for f in result.facts}
        if spec["check"] == "rows" and expected is not None:
            match = actual == expected
        elif spec["check"] == "value_counts" and expected is not None:
            match = [row[:2] for row in actual[: len(expected)]] == expected
        elif spec["check"] == "duplicate_groups" and expected is not None:
            groups = facts.get("duplicate_summary", {}).get("duplicate_groups")
            actual, match = [[groups]], [[groups]] == expected
    return {
        "table": spec["table"],
        "scenario": spec["scenario"],
        "tool": spec["tool"],
        "params": params,
        "status": result.status,
        "error": result.error.message if result.error else None,
        "match": match,
        "expected": expected[:12] if expected else expected,
        "actual": actual[:12] if actual else actual,
        "row_count": result.data.row_count if result.data else None,
        "facts": [f.text for f in result.facts[:4]],
        "cold_seconds": timings[0],
        "warm_median_seconds": round(statistics.median(timings[1:]), 3) if REPEATS > 1 else None,
    }


def queries_job(
    manifest: dict, catalog_json: dict, database: str, reference: str | None, pg_dsn: str | None
) -> dict:
    catalog = DatasetCatalog.model_validate(catalog_json)
    specs = query_specs(manifest, catalog)
    ref = duckdb.connect(reference, read_only=True) if reference else None
    if pg_dsn:
        connection = duckdb.connect(":memory:")
        connection.execute(f"ATTACH {_lit(database)} AS owned_dataset (READ_ONLY)")
        connection.execute("USE owned_dataset")
        attach_postgres(connection, pg_dsn)
    else:
        connection = duckdb.connect(database, read_only=True)
    connection.execute("SET enable_external_access=false")
    started = time.perf_counter()
    try:
        results = [run_query(spec, connection, catalog, ref) for spec in specs]
    finally:
        connection.close()
        if ref is not None:
            ref.close()
    return {
        "seconds": round(time.perf_counter() - started, 2),
        "rss_mb": peak_rss_mb(),
        "results": results,
    }


# --- PostgreSQL / ClickHouse --------------------------------------------------------


def load_postgres(manifest: dict, reference: Path, loader_dsn: str) -> dict:
    """Таблицы в демо-PostgreSQL с типами из README; имена — как у файлов, без .csv."""
    info = parse_dsn(loader_dsn)
    loaded = {}
    with duckdb.connect() as connection:
        connection.execute(f"ATTACH {_lit(str(reference))} AS ref (READ_ONLY)")
        connection.execute("LOAD postgres")
        connection.execute(
            f"CREATE TEMPORARY SECRET hk_pg (TYPE postgres, PASSWORD {_lit(info.password)})"
        )
        connection.execute(
            f"ATTACH {_lit(info.libpq(read_only=False))} AS pg (TYPE postgres, SECRET hk_pg)"
        )
        for table in manifest["tables"]:
            started = time.perf_counter()
            target = f"pg.public.{_ident(stem(table['file']))}"
            connection.execute(f"DROP TABLE IF EXISTS {target}")
            connection.execute(
                f"CREATE TABLE {target} AS SELECT * FROM ref.{reference_table(table)}"
            )
            loaded[table["file"]] = round(time.perf_counter() - started, 2)
    return loaded


def load_clickhouse(manifest: dict, reference: Path, loader_url: str, folder: Path) -> dict:
    from seed_demo_clickhouse import DATABASE, ch_ident, create_table_sql

    client = ClickHouseHttp.from_url(loader_url, default_database=DATABASE)
    loaded = {}
    with duckdb.connect(str(reference), read_only=True) as connection:
        for table in manifest["tables"]:
            started = time.perf_counter()
            name = stem(table["file"])
            columns = [
                (row[0], row[1])
                for row in connection.execute(f"DESCRIBE {reference_table(table)}").fetchall()
            ]
            parquet = folder / f"{reference_table(table)}.parquet"
            connection.execute(
                f"COPY {reference_table(table)} TO {_lit(str(parquet))} (FORMAT parquet)"
            )
            target = f"{ch_ident(client.database)}.{ch_ident(name)}"
            client.query(f"DROP TABLE IF EXISTS {target}")
            client.query(create_table_sql(client.database, name, columns))
            client.query(
                f"INSERT INTO {target} FORMAT Parquet", data=parquet.read_bytes(), timeout=600
            )
            parquet.unlink()
            loaded[table["file"]] = round(time.perf_counter() - started, 2)
    return loaded


# --- отчёт -------------------------------------------------------------------------------


def summarize(report: dict) -> list[str]:
    lines = [f"# HK_Datasets: прогон {report['started_at']}", ""]
    lines.append(f"Ревизия `{report['revision']}`, этапы: {', '.join(report['stages'])}.")
    for source in ("csv", "postgres", "clickhouse"):
        run = report.get(source)
        if not run:
            continue
        lines += ["", f"## {source}", ""]
        if not run.get("ok"):
            lines.append(f"Ошибка: `{run.get('error')}` (RSS {run.get('rss_mb')} МБ)")
            continue
        lines.append(
            f"Всего {run['seconds']} с, пик RSS {run['rss_mb']} МБ, файл {run.get('file_mb')} МБ."
        )
        lines += ["", "| Этап | Таблица | Сообщение | с |", "| --- | --- | --- | ---: |"]
        lines += [
            f"| {s['stage']} | {s.get('table') or '—'} | {s['message']} | {s['seconds']} |"
            for s in run["stages"]
        ]
        checks = run.get("checks", {})
        if checks:
            lines += ["", "| Таблица | Строк | README | Типы ≠ README | Потери | Ключ |"]
            lines.append("| --- | ---: | --- | --- | --- | --- |")
            for file, item in checks["tables"].items():
                if item.get("missing"):
                    lines.append(f"| {file} | — | нет таблицы | | | |")
                    continue
                types = "; ".join(
                    f"{m['column']}: {m['expected']}→{m.get('actual')}"
                    for m in item["type_mismatches"]
                )
                losses = "; ".join(
                    f"{x['column']}: {x['success_ratio']}" for x in item["parse_losses"]
                )
                rows_ok = "да" if item["rows_match_readme"] else "НЕТ"
                key_ok = "да" if item["key_found"] else "нет"
                lines.append(
                    f"| {file} | {item['row_count']} | {rows_ok} | {types or '—'} "
                    f"| {losses or '—'} | {key_ok} |"
                )
            lines += ["", f"Связей найдено: {len(checks['relations'])}."]
            for ref in checks["self_references"]:
                lines.append(
                    f"- самоссылка {ref['table']}: {ref['from']} → {ref['to']}: "
                    f"{'найдена' if ref['found'] else 'НЕ найдена'}"
                )
        queries = run.get("queries")
        if queries:
            lines += [
                "",
                f"Запросы: {queries['seconds']} с, RSS {queries['rss_mb']} МБ.",
                "",
                "| Таблица | Сценарий | Инструмент | Статус | Эталон | Холодный, с | Медиана, с |",
                "| --- | --- | --- | --- | --- | ---: | ---: |",
            ]
            for item in queries["results"]:
                match = {True: "совпал", False: "РАСХОЖДЕНИЕ", None: "—"}[item["match"]]
                status = item["status"] + (f": {item['error']}" if item.get("error") else "")
                lines.append(
                    f"| {stem(item['table'])} | {item['scenario']} | {item['tool']} | {status} "
                    f"| {match} | {item.get('cold_seconds', '')} "
                    f"| {item.get('warm_median_seconds', '')} |"
                )
    return lines


def compare(before_path: Path, after_path: Path) -> list[str]:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    lines = [
        f"# Сравнение `{before_path}` → `{after_path}`",
        "",
        "| Что | До | После |",
        "| --- | ---: | ---: |",
    ]
    for source in ("csv", "postgres", "clickhouse"):
        a, b = before.get(source) or {}, after.get(source) or {}
        if not a and not b:
            continue
        for field in ("seconds", "rss_mb", "file_mb"):
            lines.append(f"| {source}.{field} | {a.get(field, '—')} | {b.get(field, '—')} |")
        for x, y in zip(a.get("stages", []), b.get("stages", []), strict=False):
            if (x["message"], x.get("table")) == (y["message"], y.get("table")):
                label = f"{source}: {x['message']} {stem(x.get('table') or '')}".strip()
                lines.append(f"| {label} | {x['seconds']} | {y['seconds']} |")
        ta = (a.get("checks") or {}).get("tables", {})
        tb = (b.get("checks") or {}).get("tables", {})
        for file in sorted(set(ta) | set(tb)):
            ma = len(ta.get(file, {}).get("type_mismatches", []))
            mb = len(tb.get(file, {}).get("type_mismatches", []))
            if ma or mb:
                lines.append(f"| {source}: типы ≠ README, {stem(file)} | {ma} | {mb} |")
        qa = {
            (q["table"], q["scenario"], q["tool"]): q
            for q in (a.get("queries") or {}).get("results", [])
        }
        for q in (b.get("queries") or {}).get("results", []):
            old = qa.get((q["table"], q["scenario"], q["tool"]))
            if old is None:
                continue
            if (old["status"], old["match"]) != (q["status"], q["match"]) or abs(
                (old.get("cold_seconds") or 0) - (q.get("cold_seconds") or 0)
            ) > 0.5:
                label = f"{source}: {stem(q['table'])} {q['scenario']} {q['tool']}"
                lines.append(
                    f"| {label} | {old['status']}/{old['match']}/{old.get('cold_seconds')} с "
                    f"| {q['status']}/{q['match']}/{q.get('cold_seconds')} с |"
                )
    return lines


def run(args: argparse.Namespace) -> Path:
    manifest = load_manifest(args.manifest)
    if args.tables:
        manifest["tables"] = [t for t in manifest["tables"] if stem(t["file"])[:2] in args.tables]
    stages = tuple(args.stages.split(","))
    unknown = set(stages) - set(STAGES)
    if unknown:
        raise CheckFailure(f"Неизвестные этапы: {', '.join(sorted(unknown))}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "revision": manifest["revision"],
        "stages": stages,
        "tables": [t["file"] for t in manifest["tables"]],
        "environment": {
            "python": sys.version.split()[0],
            "duckdb": duckdb.__version__,
            "cpus": os.cpu_count(),
            "platform": sys.platform,
        },
    }
    report_path = output / "report.json"
    reference_db = (args.reference or output / "reference.duckdb").resolve()
    try:
        if "download" in stages:
            print("Скачивание…", flush=True)
            report["download"] = stage_download(manifest, args.cache)
        if "reference" in stages:
            print("Эталон: csv.reader и DuckDB с типами README…", flush=True)
            report["reference"] = in_child(build_reference, manifest, args.cache, reference_db)
        reference_counts = report.get("reference")
        reference_arg = str(reference_db) if reference_db.exists() else None
        if "csv" in stages:
            print("CSV: штатная загрузка всех файлов одним датасетом…", flush=True)
            database = output / "csv.duckdb"
            paths = [str((args.cache / t["file"]).resolve()) for t in manifest["tables"]]
            result = in_child(csv_job, paths, str(database))
            result["file_mb"] = size_mb(database)
            if result["ok"]:
                catalog = DatasetCatalog.model_validate(result["catalog"])
                result["checks"] = check_catalog(manifest, catalog, reference_counts)
                write_json(output / "csv-catalog.json", result.pop("catalog"))
                if "queries" in stages:
                    print("CSV: ключевые запросы…", flush=True)
                    result["queries"] = in_child(
                        queries_job, manifest, catalog.model_dump(mode="json", by_alias=True),
                        str(database), reference_arg, None,
                    )  # fmt: skip
            report["csv"] = result
            write_json(report_path, report)
        if "postgres" in stages:
            require_reference(reference_arg)
            print("PostgreSQL: загрузка таблиц с типами README…", flush=True)
            report["postgres_load"] = load_postgres(manifest, reference_db, args.pg_loader_dsn)
            print("PostgreSQL: подключение и профиль…", flush=True)
            database = output / "postgres.duckdb"
            result = in_child(postgres_job, args.pg_reader_dsn, str(database))
            result["file_mb"] = size_mb(database)
            if result["ok"]:
                catalog = DatasetCatalog.model_validate(result["catalog"])
                result["checks"] = check_catalog(manifest, catalog, reference_counts)
                write_json(output / "postgres-catalog.json", result.pop("catalog"))
                print("PostgreSQL: ключевые запросы…", flush=True)
                result["queries"] = in_child(
                    queries_job, manifest, catalog.model_dump(mode="json", by_alias=True),
                    str(database), reference_arg, args.pg_reader_dsn,
                )  # fmt: skip
            report["postgres"] = result
            write_json(report_path, report)
        if "clickhouse" in stages:
            require_reference(reference_arg)
            print("ClickHouse: загрузка таблиц с типами README…", flush=True)
            report["clickhouse_load"] = load_clickhouse(
                manifest, reference_db, args.ch_loader_url, output
            )
            attempts = [None] + ([args.ch_max_rows] if args.ch_max_rows else [])
            for max_rows in attempts:
                label = "clickhouse" if max_rows is None else "clickhouse_raised_limit"
                print(f"ClickHouse: подключение (max_rows={max_rows or 'по умолчанию'})…")
                database = output / f"{label}.duckdb"
                result = in_child(clickhouse_job, args.ch_reader_url, str(database), max_rows)
                result["max_rows"] = max_rows
                result["file_mb"] = size_mb(database)
                if result["ok"]:
                    catalog = DatasetCatalog.model_validate(result["catalog"])
                    result["checks"] = check_catalog(manifest, catalog, reference_counts)
                    write_json(output / f"{label}-catalog.json", result.pop("catalog"))
                    print("ClickHouse: ключевые запросы…", flush=True)
                    result["queries"] = in_child(
                        queries_job, manifest, catalog.model_dump(mode="json", by_alias=True),
                        str(database), reference_arg, None,
                    )  # fmt: skip
                report[label] = result
                write_json(report_path, report)
                if result["ok"]:
                    break
        report["status"] = "finished"
    except CheckFailure as error:
        report["status"] = "failed"
        report["error"] = str(error)
    write_json(report_path, report)
    (output / "report.md").write_text("\n".join(summarize(report)) + "\n", encoding="utf-8")
    return report_path


def require_reference(reference: str | None) -> None:
    if reference is None:
        raise CheckFailure("Нужна эталонная DuckDB: этап reference или --reference")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stages", default=",".join(DEFAULT_STAGES), help=", ".join(STAGES))
    parser.add_argument("--tables", type=lambda v: v.split(","), help="номера файлов: 01,09")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    parser.add_argument("--reference", type=Path, help="готовая reference.duckdb прошлого прогона")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "artifacts/hk-check"
        / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}",
    )
    parser.add_argument("--pg-loader-dsn", default=os.environ.get("DJEDAI_DEMO_PG_LOADER_DSN"))
    parser.add_argument("--pg-reader-dsn", default=os.environ.get("DJEDAI_DEMO_PG_READER_DSN"))
    parser.add_argument("--ch-loader-url", default=os.environ.get("DJEDAI_DEMO_CH_LOADER_URL"))
    parser.add_argument("--ch-reader-url", default=os.environ.get("DJEDAI_DEMO_CH_READER_URL"))
    parser.add_argument(
        "--ch-max-rows", type=int, help="повторить подключение ClickHouse с этим лимитом строк"
    )
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args(argv)
    if args.compare:
        print("\n".join(compare(*args.compare)))
        return 0
    stages = set(args.stages.split(","))
    if "postgres" in stages and not (args.pg_loader_dsn and args.pg_reader_dsn):
        parser.error("для postgres задайте DJEDAI_DEMO_PG_LOADER_DSN и DJEDAI_DEMO_PG_READER_DSN")
    if "clickhouse" in stages and not (args.ch_loader_url and args.ch_reader_url):
        parser.error("для clickhouse задайте DJEDAI_DEMO_CH_LOADER_URL и DJEDAI_DEMO_CH_READER_URL")
    report_path = run(args)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(f"Отчёт: {report_path}")
    print(f"status: {report['status']}")
    return 0 if report["status"] == "finished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
