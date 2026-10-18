"""Прогон набора «грязных» CSV (B1-3) против ожиданий из ``fixtures/csv/manifest.json``.

Манифест — единый источник ожиданий: для каждого файла описаны проблема, ожидаемая
загрузка (кодировка, разделитель, колонки, значения или текст ошибки) и типизация
(типы, записи журнала, значения представления). Типизация проверяется, когда в проекте
есть ``djedai.data.typed_views`` (B1-2).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import pytest

from djedai.data.csv_ingest import CsvReadError, RawTable, load_raw_csv, load_raw_csvs

FIXTURES = Path(__file__).parents[2] / "fixtures" / "csv"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
MIN_FILES = 10
# Крайние случаи из задачи: каждый должен быть закрыт хотя бы одним файлом.
REQUIRED_TAGS = {
    "encoding",
    "bom",
    "delimiter",
    "missing_values",
    "broken_structure",
    "duplicate_headers",
    "ragged_rows",
    "mixed_dates",
    "leading_zero_ids",
}
LOADED = [name for name, item in MANIFEST.items() if item["expect"]["status"] == "loaded"]
ERRORS = [name for name, item in MANIFEST.items() if item["expect"]["status"] == "error"]


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def load_actual(con, name: str) -> tuple[dict, RawTable | None]:
    """Загружает файл с параметрами из манифеста и возвращает фактический результат."""
    options = MANIFEST[name].get("load_options", {})
    try:
        table = load_raw_csv(con, FIXTURES / name, **options)
    except CsvReadError as error:
        return {"status": "error", "message": str(error)}, None
    rows = [list(row) for row in con.execute(f'SELECT * FROM "{table.raw_table}"').fetchall()]
    actual = {
        "status": "loaded",
        "encoding": table.encoding,
        "delimiter": table.delimiter,
        "header": table.header,
        "columns": list(table.columns),
        "row_count": table.row_count,
        "rows": rows,
    }
    return actual, table


def load_mismatches(actual: dict, expect: dict) -> list[str]:
    """Расхождения фактической загрузки с ожиданием (пусто, если всё совпало)."""
    if actual["status"] != expect["status"]:
        return [f"статус: {actual['status']} вместо {expect['status']}"]
    if expect["status"] == "error":
        return [
            f"в ошибке нет {part!r}: {actual['message']}"
            for part in expect["error_contains"]
            if part not in actual["message"]
        ]
    found = [
        f"{key}: {actual[key]!r} вместо {expect[key]!r}"
        for key in ("encoding", "delimiter", "header", "columns", "row_count")
        if actual[key] != expect[key]
    ]
    expected_rows = expect["rows"]
    if actual["rows"][: len(expected_rows)] != expected_rows:
        found.append(f"значения: {actual['rows'][: len(expected_rows)]!r} вместо {expected_rows!r}")
    return found


def typing_actual(con, table: RawTable) -> dict:
    """Фактическая типизация: колонки, журнал и первые значения представления."""
    from djedai.data.typed_views import create_typed_view, read_journal

    view = create_typed_view(con, table)
    names = ", ".join(f'CAST("{c.name}" AS VARCHAR)' for c in view.columns)
    rows = [list(r) for r in con.execute(f'SELECT {names} FROM "{view.view_name}"').fetchall()]
    return {
        "columns": {
            c.name: {"semantic_type": c.semantic_type, "physical_type": c.physical_type}
            for c in view.columns
        },
        "journal": [
            {"column": e.column_name, "action": e.action, "detail": e.detail}
            for e in read_journal(con, table.raw_table)
        ],
        "rows": rows,
    }


def typing_mismatches(actual: dict, expect: dict) -> list[str]:
    found: list[str] = []
    if list(actual["columns"]) != list(expect["columns"]):
        found.append(f"колонки: {list(actual['columns'])} вместо {list(expect['columns'])}")
    for name, wanted in expect["columns"].items():
        got = actual["columns"].get(name, {})
        found += [
            f"{name}.{key}: {got.get(key)!r} вместо {value!r}"
            for key, value in wanted.items()
            if got.get(key) != value
        ]
    for wanted in expect.get("journal", []):
        matches = [
            entry
            for entry in actual["journal"]
            if entry["column"] == wanted["column"]
            and entry["action"] == wanted["action"]
            and all(entry["detail"].get(k) == v for k, v in wanted["detail"].items())
        ]
        if not matches:
            found.append(f"нет записи журнала {wanted}")
    typed_rows = expect.get("typed_rows")
    if typed_rows is not None and actual["rows"][: len(typed_rows)] != typed_rows:
        found.append(f"значения: {actual['rows'][: len(typed_rows)]!r} вместо {typed_rows!r}")
    return found


def test_manifest_matches_files_on_disk():
    on_disk = {path.relative_to(FIXTURES).as_posix() for path in (FIXTURES / "dirty").glob("*.csv")}
    assert on_disk <= set(MANIFEST), f"файлы без ожиданий: {sorted(on_disk - set(MANIFEST))}"
    assert all((FIXTURES / name).is_file() for name in MANIFEST)


def test_set_is_large_enough_and_covers_required_cases():
    assert len(MANIFEST) >= MIN_FILES
    tags = {tag for item in MANIFEST.values() for tag in item["tags"]}
    assert REQUIRED_TAGS <= tags, f"не покрыто: {sorted(REQUIRED_TAGS - tags)}"
    assert all(item["problem"].strip() and item["expect"] for item in MANIFEST.values())


@pytest.mark.parametrize("name", MANIFEST)
def test_load_matches_expectation(con, name):
    actual, _ = load_actual(con, name)
    assert load_mismatches(actual, MANIFEST[name]["expect"]) == []


def test_set_loads_sequentially_with_expected_errors(con):
    paths = [FIXTURES / name for name in MANIFEST if "load_options" not in MANIFEST[name]]
    report = load_raw_csvs(con, paths)
    assert {e.file_name for e in report.errors} == {Path(n).name for n in ERRORS}
    assert {t.original_name for t in report.loaded} == {
        Path(n).name for n in LOADED if "load_options" not in MANIFEST[n]
    }


# # @pytest.mark.xfail(
# #     strict=True,
# #     reason="_has_header не считает заголовком строку с повторами/пустыми ячейками (#53)",
# # )


def test_duplicate_headers_are_detected_without_hint(con):
    table = load_raw_csv(con, FIXTURES / "dirty/duplicate_headers.csv")
    assert table.header is True
    assert table.row_count == 2


@pytest.mark.parametrize("name", [n for n in LOADED if "typing" in MANIFEST[n]])
def test_typing_matches_expectation(con, name):
    pytest.importorskip("djedai.data.typed_views")
    _, table = load_actual(con, name)
    assert typing_mismatches(typing_actual(con, table), MANIFEST[name]["typing"]) == []


def test_raw_values_survive_typing(con):
    pytest.importorskip("djedai.data.typed_views")
    for name in ("dirty/unparseable_values.csv", "dirty/leading_zero_ids.csv"):
        _, table = load_actual(con, name)
        before = con.execute(f'SELECT * FROM "{table.raw_table}"').fetchall()
        typing_actual(con, table)
        assert con.execute(f'SELECT * FROM "{table.raw_table}"').fetchall() == before


# Ожидания должны замечать типичные поломки: подмена результата и сверка с манифестом.
def test_expectations_detect_lost_leading_zeros(con):
    name = "dirty/leading_zero_ids.csv"
    actual, _ = load_actual(con, name)
    broken = copy.deepcopy(actual)
    broken["rows"] = [[str(int(row[0])), *row[1:]] for row in broken["rows"]]
    assert load_mismatches(actual, MANIFEST[name]["expect"]) == []
    assert load_mismatches(broken, MANIFEST[name]["expect"])


def test_expectations_detect_misread_dates(con):
    pytest.importorskip("djedai.data.typed_views")
    name = "dirty/cp1251_cyrillic_headers.csv"
    _, table = load_actual(con, name)
    actual = typing_actual(con, table)
    swapped = copy.deepcopy(actual)
    swapped["rows"] = [[r[0], r[1], f"2026-{r[2][8:]}-{r[2][5:7]}"] for r in swapped["rows"]]
    assert typing_mismatches(actual, MANIFEST[name]["typing"]) == []
    assert typing_mismatches(swapped, MANIFEST[name]["typing"])


@pytest.mark.parametrize("mutation", ["drop_field", "merge_fields", "extra_field"])
def test_expectations_detect_broken_row_structure(con, mutation):
    name = "dirty/bom_utf8.csv"
    actual, _ = load_actual(con, name)
    broken = copy.deepcopy(actual)
    first = broken["rows"][0]
    broken["rows"][0] = {
        "drop_field": first[:-1],
        "merge_fields": [first[0], f"{first[1]} {first[2]}"],
        "extra_field": [*first, "лишнее"],
    }[mutation]
    assert load_mismatches(actual, MANIFEST[name]["expect"]) == []
    assert load_mismatches(broken, MANIFEST[name]["expect"])
