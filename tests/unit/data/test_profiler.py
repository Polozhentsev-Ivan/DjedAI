"""Профайлер (B1-4) против эталонов, посчитанных независимо от DuckDB.

Каждая таблица собирается из Python-списков, а ожидания считаются по тем же спискам
через ``statistics``, ``Counter`` и ``set``; небольшие случаи сверяются с литералами.
"""

from __future__ import annotations

import csv
import json
import random
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from djedai.data.csv_ingest import CsvReadError, load_raw_csv
from djedai.data.profiler import (
    COLUMN_PROFILES,
    TABLE_PROFILES,
    profile_table,
    profile_tables,
    read_profiles,
)
from djedai.data.typed_views import TypedViewError, create_typed_view

FIXTURES = Path(__file__).parents[2] / "fixtures" / "csv"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def write_csv(path: Path, columns: dict[str, list[str | None]]) -> Path:
    """CSV с разделителем ``;``; ``None`` — пустое поле без кавычек (NULL в raw)."""
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(columns)
        writer.writerows(zip(*columns.values(), strict=True))
    return path


def profile(con, tmp_path, columns: dict[str, list[str | None]], name: str = "t.csv"):
    raw = load_raw_csv(con, write_csv(tmp_path / name, columns), header=True)
    return profile_table(con, create_typed_view(con, raw))


def column(table, name: str):
    return next(c for c in table.columns if c.name == name)


def test_missing_values_counted_in_null_ratio(con, tmp_path):
    n = 40
    names = [f"имя{i}" for i in range(n)]
    for i in (3, 7, 11):
        names[i] = None
    for i in (13, 17):
        names[i] = "   "  # строка из пробелов — тоже пропуск
    amounts = [f"{i}.5" for i in range(n)]
    amounts[5] = "n/a"  # не распознано → NULL в представлении
    amounts[9] = amounts[21] = None
    table = profile(
        con, tmp_path, {"id": [str(i) for i in range(n)], "name": names, "amount": amounts}
    )

    expected_names = sum(v is None or not v.strip() for v in names)
    expected_amounts = sum(v is None or v == "n/a" for v in amounts)
    assert (column(table, "name").null_count, column(table, "name").null_ratio) == (
        expected_names,
        expected_names / n,
    )
    assert column(table, "amount").semantic_type == "float"
    assert column(table, "amount").null_count == expected_amounts
    assert column(table, "amount").null_ratio == pytest.approx(expected_amounts / n)
    assert column(table, "id").null_ratio == 0.0


def test_full_duplicate_rows(con, tmp_path):
    rows = [("1", "a"), ("2", "b"), ("3", "c"), ("1", "a"), ("2", "b"), ("2", "b"), ("3", "d")]
    table = profile(con, tmp_path, {"k": [r[0] for r in rows], "v": [r[1] for r in rows]})
    assert table.row_count == len(rows)
    assert table.duplicate_rows == len(rows) - len(set(rows)) == 3


def cents(value: Decimal) -> str:
    """Ожидаемая запись DECIMAL(18,2) в профиле: точная строка с двумя знаками."""
    return format(value.quantize(Decimal("0.01")), "f")


def number_columns(seed: int = 7, n: int = 60) -> tuple[dict, dict]:
    rng = random.Random(seed)
    ints = [rng.randint(-50, 500) for _ in range(n)]
    floats = [round(rng.uniform(-10, 10), 3) for _ in range(n)]
    money = [Decimal(rng.randint(0, 500_000)) / 100 for _ in range(n)]
    percents = [Decimal(rng.randint(0, 1000)) / 10 for _ in range(n)]
    text = {
        "qty": [str(v) for v in ints],
        "ratio": [repr(v) for v in floats],
        "price": [f"{v:.2f} руб." for v in money],
        "share": [f"{v}%" for v in percents],
    }
    return text, {"qty": ints, "ratio": floats, "price": money, "share": percents}


@pytest.mark.parametrize(
    ("name", "semantic", "exact"),
    [
        ("qty", "integer", lambda v: v),
        ("ratio", "float", lambda v: v),
        ("price", "money", cents),  # DECIMAL хранится точной строкой
        ("share", "percent", cents),
    ],
)
def test_number_stats_match_statistics(con, tmp_path, name, semantic, exact):
    text, values = number_columns()
    profiled = column(profile(con, tmp_path, text), name)
    data = values[name]
    assert profiled.semantic_type == semantic
    assert profiled.stats == {
        "min": exact(min(data)),
        "max": exact(max(data)),
        "mean": pytest.approx(float(statistics.mean(data))),
        "median": pytest.approx(float(statistics.median(data))),
        "std": pytest.approx(float(statistics.stdev(data))),
    }
    assert profiled.distinct_count == len(set(data))


@pytest.mark.parametrize(
    "values",
    [
        [2**53, 2**53 + 1],  # соседние целые, которые float не различает
        [2**53, 2**53 + 1, 2**53 + 3],
        [-(2**63) + 1, 2**63 - 1],  # разность не помещается в BIGINT
    ],
)
def test_large_integers_keep_spread(con, tmp_path, values):
    text = {"qty": [str(v) for v in values], "k": ["x"] * len(values)}
    profiled = column(profile(con, tmp_path, text), "qty")
    assert profiled.semantic_type == "integer"
    assert profiled.stats == {
        "min": min(values),
        "max": max(values),
        "mean": pytest.approx(float(statistics.mean(values))),
        "median": pytest.approx(float(statistics.median(values))),
        "std": pytest.approx(statistics.stdev(values)),
    }
    assert profiled.stats["std"] > 0


def test_money_keeps_exact_values(con, tmp_path):
    amounts = [Decimal("100000000000000.01"), Decimal("100000000000000.02")]
    text = {"price": [f"{v} RUB" for v in amounts], "k": ["x", "x"]}
    table = profile(con, tmp_path, text)
    profiled = column(table, "price")
    assert (profiled.semantic_type, profiled.physical_type) == ("money", "DECIMAL(18,2)")

    expected = [str(v) for v in amounts]
    assert profiled.distinct_count == 2
    assert [t.value for t in profiled.top_values] == expected
    assert list(profiled.sample_values) == expected
    assert (profiled.stats["min"], profiled.stats["max"]) == tuple(expected)
    assert profiled.stats["mean"] == pytest.approx(float(statistics.mean(amounts)))
    assert profiled.stats["std"] == pytest.approx(float(statistics.stdev(amounts)))
    assert read_profiles(con) == [table]  # сохранение и чтение не теряют копейки


@pytest.mark.parametrize(
    ("suffix", "semantic"),
    [(" RUB", "money"), ("%", "percent")],
)
def test_decimal_full_range_does_not_overflow(con, tmp_path, suffix, semantic):
    # разность max - min не помещается в DECIMAL(18,2): тип расширяется до вычитания
    edge = Decimal("9999999999999999.99")
    values = [-edge, Decimal("0.01"), edge]
    text = {"v": [f"{v}{suffix}" for v in values], "k": ["x"] * len(values)}
    profiled = column(profile(con, tmp_path, text), "v")
    assert (profiled.semantic_type, profiled.physical_type) == (semantic, "DECIMAL(18,2)")
    assert profiled.stats == {
        "min": str(-edge),
        "max": str(edge),
        "mean": pytest.approx(float(statistics.mean(values))),
        "median": pytest.approx(float(statistics.median(values))),
        "std": pytest.approx(float(statistics.stdev(values))),
    }


def test_single_number_has_no_std(con, tmp_path):
    profiled = column(profile(con, tmp_path, {"qty": ["5"], "k": ["x"]}), "qty")
    assert profiled.stats == {"min": 5, "max": 5, "mean": 5.0, "median": 5.0, "std": None}


def days(start: date, step: int, count: int) -> list[date]:
    return [start + timedelta(days=step * i) for i in range(count)]


def months(pairs: list[tuple[int, int]]) -> list[date]:
    return [date(year, month, 1) for year, month in pairs]


@pytest.mark.parametrize(
    ("values", "granularity"),
    [
        (days(date(2024, 1, 3), 1, 20) + [date(2024, 3, 9)], "day"),
        (days(date(2024, 1, 1), 7, 12), "week"),
        (months([(2024, m) for m in range(1, 13)]), "month"),
        (months([(2023, 1), (2023, 4), (2023, 7), (2023, 10), (2024, 1)]), "quarter"),
        (months([(y, 1) for y in range(2019, 2025)]), "year"),
    ],
)
def test_date_range_and_granularity(con, tmp_path, values, granularity):
    text = [v.strftime("%Y-%m-%d") for v in values]
    table = profile(con, tmp_path, {"day": text, "k": ["x"] * len(text)})
    profiled = column(table, "day")
    assert profiled.semantic_type == "date"
    assert profiled.stats == {
        "min": min(values).isoformat(),
        "max": max(values).isoformat(),
        "granularity": granularity,
    }
    assert table.time_columns == ("day",)


@pytest.mark.parametrize(
    ("values", "fmt", "granularity"),
    [
        (
            [datetime(2024, 5, 1, 10, 15, 7) + timedelta(minutes=13 * i) for i in range(20)],
            "%Y-%m-%d %H:%M:%S",
            "second",
        ),
        (
            [datetime(2024, 5, 1, 10, 15) + timedelta(minutes=13 * i) for i in range(20)],
            "%Y-%m-%d %H:%M",
            "minute",
        ),
        (
            [datetime(2024, 5, 1, 9) + timedelta(hours=5 * i) for i in range(20)],
            "%Y-%m-%d %H:%M",
            "hour",
        ),
        (
            [datetime(2024, 5, 1) + timedelta(days=i) for i in range(20)],
            "%Y-%m-%d %H:%M",
            "day",
        ),
    ],
)
def test_datetime_granularity(con, tmp_path, values, fmt, granularity):
    text = [v.strftime(fmt) for v in values]
    profiled = column(profile(con, tmp_path, {"at": text, "k": ["x"] * len(text)}), "at")
    assert profiled.semantic_type == "datetime"
    assert profiled.stats == {
        "min": min(values).isoformat(),
        "max": max(values).isoformat(),
        "granularity": granularity,
    }


def test_top_values_and_samples(con, tmp_path):
    rng = random.Random(3)
    # 15 различных значений с повторяющимися частотами: порядок при равенстве — по значению
    values = [f"v{i:02d}" for i in range(15) for _ in range(1 + i % 4)]
    rng.shuffle(values)
    values[4] = None
    profiled = column(profile(con, tmp_path, {"cat": values, "k": ["x"] * len(values)}), "cat")

    present = [v for v in values if v is not None]
    counts = Counter(present)
    expected_top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    assert [(t.value, t.count) for t in profiled.top_values] == expected_top
    assert list(profiled.sample_values) == list(dict.fromkeys(present))[:5]
    assert profiled.distinct_count == len(counts)
    assert profiled.is_unique is False


def test_unique_ignores_missing_values(con, tmp_path):
    table = profile(con, tmp_path, {"code": ["a", "b", None, "c"], "k": ["x", "x", "x", "x"]})
    assert column(table, "code").is_unique is True
    assert column(table, "k").is_unique is False
    assert table.primary_key_candidates == ()  # у code есть пропуск


def test_all_missing_column(con, tmp_path):
    table = profile(con, tmp_path, {"empty": [None, "", "  "], "k": ["1", "2", "3"]})
    profiled = column(table, "empty")
    assert (profiled.null_count, profiled.null_ratio) == (3, 1.0)
    assert (profiled.distinct_count, profiled.is_unique) == (0, False)
    assert profiled.top_values == profiled.sample_values == ()
    assert profiled.stats == {}


def test_table_without_suitable_key(con, tmp_path):
    table = profile(
        con,
        tmp_path,
        {
            "city": ["Москва", "Казань", "Москва", "Пермь"],
            "note": ["a", None, "b", "c"],
            "price": ["1.5", "2.5", "3.5", "4.5"],  # уникальна, но дробная
            "flag": ["да", "нет", "да", "нет"],
        },
    )
    assert column(table, "price").is_unique is True
    assert table.primary_key_candidates == ()


def test_id_like_key_candidates_go_first(con, tmp_path):
    table = profile(
        con,
        tmp_path,
        {
            "name": ["a", "b", "c"],
            "order_id": ["10", "11", "12"],
            "code": ["001", "002", "003"],
            "qty": ["1", "1", "2"],
        },
    )
    assert table.primary_key_candidates == ("order_id", "code", "name")


def test_header_only_table():
    connection = duckdb.connect(":memory:")
    raw = load_raw_csv(connection, FIXTURES / "dirty" / "header_only.csv")
    table = profile_table(connection, create_typed_view(connection, raw))
    assert (table.row_count, table.duplicate_rows, table.primary_key_candidates) == (0, 0, ())
    assert all(c.null_ratio == 0.0 and c.top_values == () for c in table.columns)


def test_profiles_are_stored_and_replaced(con, tmp_path):
    raw = load_raw_csv(con, write_csv(tmp_path / "t.csv", {"id": ["1", "2"], "v": ["a", "b"]}))
    view = create_typed_view(con, raw)
    table = profile_table(con, view)
    assert read_profiles(con) == [table]
    assert read_profiles(con, table.view_name) == [table]
    assert read_profiles(con, "missing") == []

    con.execute(f"INSERT INTO {raw.raw_table} VALUES ('2', 'b')")
    again = profile_table(con, view)
    assert again.row_count == 3
    assert read_profiles(con) == [again]
    assert again.duplicate_rows == 1
    assert con.execute(f"SELECT count(*) FROM {TABLE_PROFILES}").fetchone()[0] == 1
    assert con.execute(f"SELECT count(*) FROM {COLUMN_PROFILES}").fetchone()[0] == 2


def test_profile_tables_isolates_errors(con, tmp_path):
    good = profile(con, tmp_path, {"id": ["1"], "v": ["a"]}, name="good.csv")
    broken = profile(con, tmp_path, {"id": ["1"], "v": ["a"]}, name="broken.csv")
    con.execute(f'DROP VIEW "{broken.view_name}"')

    report = profile_tables(con)
    assert [p.view_name for p in report.profiles] == [good.view_name]
    assert [e.table for e in report.errors] == [broken.view_name]
    assert "не удалось посчитать профиль" in str(report.errors[0])


LOADED = [name for name, item in MANIFEST.items() if item["expect"]["status"] == "loaded"]


@pytest.mark.parametrize("name", LOADED)
def test_dirty_fixtures_profile(con, name):
    item = MANIFEST[name]
    try:
        raw = load_raw_csv(con, FIXTURES / name, **item.get("load_options", {}))
        view = create_typed_view(con, raw)
    except (CsvReadError, TypedViewError) as error:
        pytest.fail(f"файл из манифеста не загрузился: {error}")
    table = profile_table(con, view)
    assert table.row_count == item["expect"]["row_count"]
    assert [c.name for c in table.columns] == [c.name for c in view.columns]
    assert all(0.0 <= c.null_ratio <= 1.0 for c in table.columns)
