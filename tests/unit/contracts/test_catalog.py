"""Контракт ``DatasetCatalog`` (B1-7): валидация примеров и компактный ``to_prompt()``."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from djedai.contracts.catalog import DatasetCatalog

EXAMPLES = Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples"
FULL = json.loads((EXAMPLES / "dataset_catalog.json").read_text(encoding="utf-8"))
MINIMAL = json.loads((EXAMPLES / "dataset_catalog_minimal.json").read_text(encoding="utf-8"))
PROMPT = (EXAMPLES / "dataset_catalog.prompt.txt").read_text(encoding="utf-8").rstrip("\n")


@pytest.mark.parametrize("data", [FULL, MINIMAL], ids=["full", "minimal"])
def test_examples_are_valid_and_round_trip(data):
    catalog = DatasetCatalog.model_validate(data)
    dumped = catalog.model_dump(mode="json")
    assert DatasetCatalog.model_validate(dumped) == catalog
    assert DatasetCatalog.model_validate_json(catalog.model_dump_json()) == catalog


def test_relation_is_serialized_with_spec_names():
    relation = DatasetCatalog.model_validate(FULL).model_dump(mode="json")["relations"][0]
    assert relation["from"] == {"table": "orders", "column": "client_id"}
    assert "from_" not in relation


def test_optional_parts_have_defaults():
    catalog = DatasetCatalog.model_validate(MINIMAL)
    assert (catalog.summary, catalog.relations, catalog.warnings) == (None, [], [])
    column = catalog.tables[0].column("empty_col")
    assert column.description is None
    assert (column.sample_values, column.top_values, column.stats.min) == ([], [], None)


def mutated(change) -> dict:
    data = copy.deepcopy(FULL)
    change(data)
    return data


def orders(data):
    return data["tables"][0]


def column(data, name: str) -> dict:
    return next(c for c in orders(data)["columns"] if c["name"] == name)


INVALID = {
    "нет dataset_id": lambda d: d.pop("dataset_id"),
    "лишнее поле": lambda d: d.update(extra=1),
    "другая версия схемы": lambda d: d.update(schema_version=2),
    "время без часового пояса": lambda d: d.update(created_at="2026-10-20T12:00:00"),
    "неизвестный источник": lambda d: d["source"].update(kind="excel"),
    "повтор table_id": lambda d: d["tables"][1].update(table_id="orders"),
    "таблица без колонок": lambda d: orders(d).update(columns=[]),
    "повтор имени колонки": lambda d: column(d, "status").update(name="amount"),
    "ключ без колонки": lambda d: orders(d).update(primary_key_candidates=["nope"]),
    "время без колонки": lambda d: orders(d).update(time_columns=["nope"]),
    "дублей больше строк": lambda d: orders(d).update(duplicate_rows=10**9),
    "отрицательное число строк": lambda d: orders(d).update(row_count=-1),
    "доля пропусков > 1": lambda d: column(d, "amount").update(null_ratio=1.5),
    "неизвестный тип": lambda d: column(d, "amount").update(semantic_type="number"),
    "неизвестная роль": lambda d: column(d, "amount").update(role="metric"),
    "больше 10 top-values": lambda d: column(d, "status").update(
        top_values=[{"value": str(i), "count": 1} for i in range(11)]
    ),
    "больше 5 примеров": lambda d: column(d, "status").update(sample_values=list("abcdef")),
    "нулевая частота": lambda d: column(d, "status").update(
        top_values=[{"value": "a", "count": 0}]
    ),
    "среднее у текста": lambda d: column(d, "status").update(stats={"mean": 1.0}),
    "min/max у текста": lambda d: column(d, "status").update(stats={"min": "a", "max": "b"}),
    "гранулярность у числа": lambda d: column(d, "amount")["stats"].update(granularity="day"),
    "неизвестная гранулярность": lambda d: column(d, "created_at")["stats"].update(
        granularity="decade"
    ),
    "связь на нет колонки": lambda d: d["relations"][0]["to"].update(column="nope"),
    "связь на нет таблицы": lambda d: d["relations"][0]["from"].update(table="nope"),
    "неизвестная кардинальность": lambda d: d["relations"][0].update(cardinality="M:N"),
    "overlap > 1": lambda d: d["relations"][0].update(overlap=1.2),
    "преобразование без колонки": lambda d: d["transformations"][0].update(column="nope"),
}


@pytest.mark.parametrize("change", INVALID.values(), ids=INVALID.keys())
def test_invalid_catalogs_are_rejected(change):
    with pytest.raises(ValidationError):
        DatasetCatalog.model_validate(mutated(change))


def test_catalog_is_immutable():
    catalog = DatasetCatalog.model_validate(FULL)
    with pytest.raises(ValidationError):
        catalog.name = "другое"


def test_prompt_matches_reference():
    assert DatasetCatalog.model_validate(FULL).to_prompt() == PROMPT


def test_prompt_keeps_structure():
    catalog = DatasetCatalog.model_validate(FULL)
    prompt = catalog.to_prompt()
    for table in catalog.tables:
        assert f"Таблица {table.table_id}" in prompt
        for col in table.columns:
            assert f"- {col.name}: {col.semantic_type}, {col.role}" in prompt
            assert col.description in prompt
    for relation in catalog.relations:
        assert f"{relation.from_.table}.{relation.from_.column} → " in prompt
        assert relation.cardinality in prompt


def test_prompt_has_no_heavy_lists():
    catalog = DatasetCatalog.model_validate(FULL)
    prompt = catalog.to_prompt()
    heavy = {
        str(value)
        for table in catalog.tables
        for col in table.columns
        for value in [
            *col.sample_values,
            *(t.value for t in col.top_values),
            *(col.parse.failed_examples if col.parse else []),
        ]
    }
    assert [value for value in heavy if value in prompt] == []
    assert "rename_column" not in prompt  # журнал преобразований в промпт не идёт
    assert len(prompt) < len(catalog.model_dump_json()) / 3


def prompt_line(data: dict, name: str) -> str:
    prompt = DatasetCatalog.model_validate(data).to_prompt()
    return next(line for line in prompt.splitlines() if line.startswith(f"- {name}:"))


def test_currency_only_for_money():
    with pytest.raises(ValidationError):
        DatasetCatalog.model_validate(mutated(lambda d: column(d, "status").update(currency="RUB")))
    with pytest.raises(ValidationError):
        DatasetCatalog.model_validate(
            mutated(lambda d: column(d, "amount").update(currency="рубль"))
        )


def test_prompt_shows_currency_range_and_distinct():
    assert "валюта RUB" in prompt_line(FULL, "amount")
    assert ", 0 … 250000" in prompt_line(FULL, "amount")  # диапазон без «.0»
    assert "уникальных 3" in prompt_line(FULL, "status")
    assert "уникальных" not in prompt_line(FULL, "order_id")  # у id число бесполезно
    assert "уникальных" not in prompt_line(FULL, "amount")


def test_prompt_without_currency_or_stats_has_no_gaps():
    def change(data):
        col = column(data, "amount")
        col.pop("currency")
        col["stats"] = {}

    line = prompt_line(mutated(change), "amount")
    assert "валюта" not in line
    assert "…" not in line


@pytest.mark.parametrize(
    ("original", "description", "shown"),
    [
        ("Статус", "Состояние заказа", True),  # описание не передаёт заголовок
        ("Статус", "Статус заказа в системе", False),  # заголовок уже в описании
        ("статус", "СТАТУС заказа", False),  # без учёта регистра
        ("Статус", None, True),  # описания нет: заголовок единственная подсказка
        ("status", "Состояние заказа", False),  # совпадает с name
    ],
)
def test_original_name_is_shown_only_when_it_adds_meaning(original, description, shown):
    def change(data):
        col = column(data, "status")
        col["original_name"] = original
        col["description"] = description

    assert (f"«{original}»" in prompt_line(mutated(change), "status")) is shown


def test_prompt_marks_primary_and_foreign_keys():
    assert prompt_line(FULL, "order_id").startswith("- order_id: id, id, PK")
    assert "FK → clients.id" in prompt_line(FULL, "client_id")
    assert "PK" not in prompt_line(FULL, "client_id")
    assert "FK" not in prompt_line(FULL, "order_id")
    assert "PK" not in prompt_line(FULL, "amount")


def test_prompt_marks_column_that_is_both_primary_and_foreign_key():
    def change(data):
        data["tables"][1]["primary_key_candidates"] = ["id"]
        data["relations"].append(
            {
                "from": {"table": "clients", "column": "id"},
                "to": {"table": "orders", "column": "client_id"},
                "overlap": 0.5,
                "jaccard": 0.5,
                "cardinality": "1:1",
                "confidence": "low",
            }
        )

    prompt = DatasetCatalog.model_validate(mutated(change)).to_prompt()
    assert "- id: id, id, PK, FK → orders.client_id" in prompt


def test_prompt_flags_key_that_is_not_unique():
    def change(data):
        orders(data)["duplicate_rows"] = 12
        column(data, "order_id")["is_unique"] = False
        column(data, "order_id")["distinct_count"] = 125418

    prompt = DatasetCatalog.model_validate(mutated(change)).to_prompt()
    assert "ключ order_id (не уникален)" in prompt
    assert "полных дублей 12" in prompt
    assert "(не уникален)" not in DatasetCatalog.model_validate(FULL).to_prompt()


def test_reference_example_is_consistent_about_duplicates():
    table = DatasetCatalog.model_validate(FULL).tables[0]
    key = table.column(table.primary_key_candidates[0])
    assert key.is_unique and table.duplicate_rows == 0


def test_prompt_separates_empty_and_unparsed_values():
    line = prompt_line(FULL, "amount")
    assert "пропусков 0.4%" in line
    assert "не распознано 1.2%" in line  # 1.2% от непустых, т.е. 1.195% всех строк


def test_unparsed_share_is_taken_from_non_empty_values():
    def change(data):
        col = column(data, "amount")
        col["null_ratio"] = 0.5
        col["parse"]["success_ratio"] = 0.8

    assert "не распознано 10.0%" in prompt_line(mutated(change), "amount")  # 0.2 * 0.5


@pytest.mark.parametrize("name", ["order_id", "status"])
def test_prompt_has_no_unparsed_share_without_parse_info(name):
    assert "не распознано" not in prompt_line(FULL, name)


def test_prompt_skips_negligible_unparsed_share():
    def change(data):
        column(data, "amount")["parse"]["success_ratio"] = 0.99999

    assert "не распознано" not in prompt_line(mutated(change), "amount")


def test_fully_empty_column_has_no_unparsed_share():
    def change(data):
        col = column(data, "amount")
        col["null_ratio"] = 1.0
        col["parse"]["success_ratio"] = 0.0

    line = prompt_line(mutated(change), "amount")
    assert "пропусков 100.0%" in line
    assert "не распознано" not in line
