"""Контракт определения метрики (B2-9): согласованность полей и проверка по каталогу."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from djedai.contracts.agent import Clarification
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.metrics import MetricDefinition, MetricFilter, MetricProvenance
from djedai.contracts.tool_result import ToolResult

EXAMPLES = Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples"
CATALOG = DatasetCatalog.model_validate(
    json.loads((EXAMPLES / "dataset_catalog.json").read_text(encoding="utf-8"))
)
EXAMPLE = json.loads((EXAMPLES / "metric_definitions.json").read_text(encoding="utf-8"))
REVENUE, ORDERS_COUNT, AVERAGE_CHECK = EXAMPLE["metrics"]


def definitions() -> dict[str, MetricDefinition]:
    return {m["metric_id"]: MetricDefinition.model_validate(m) for m in EXAMPLE["metrics"]}


def changed(data: dict, **changes) -> dict:
    data = copy.deepcopy(data)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def test_example_is_valid_against_its_catalog_and_round_trips():
    metrics = definitions()
    for metric in metrics.values():
        metric.validate_against(CATALOG, metrics)
        assert MetricDefinition.model_validate_json(metric.model_dump_json()) == metric
    provenance = MetricProvenance.model_validate(EXAMPLE["provenance"])
    assert provenance.model_dump(mode="json")["checks"][0]["from"]["column"] == "client_id"


def test_new_definition_is_proposed_and_not_confirmed():
    proposal = changed(REVENUE, status=None, confirmation=None, version=None)
    metric = MetricDefinition.model_validate(proposal)
    assert (metric.status, metric.version, metric.confirmation) == ("proposed", 1, None)


INVALID = {
    "сумма без measure": changed(REVENUE, measure=None),
    "count с measure": changed(ORDERS_COUNT, measure={"table": "orders", "column": "amount"}),
    "отношение без знаменателя": changed(AVERAGE_CHECK, denominator=None),
    "отношение само на себя": changed(AVERAGE_CHECK, numerator="average_check"),
    "отношение с одинаковыми частями": changed(AVERAGE_CHECK, denominator="revenue"),
    "числитель у суммы": changed(REVENUE, numerator="orders_count"),
    "отношение суммируется": changed(AVERAGE_CHECK, additivity="additive"),
    "фильтр у отношения": changed(AVERAGE_CHECK, filters=REVENUE["filters"]),
    "календарь у отношения": changed(AVERAGE_CHECK, time_column=REVENUE["time_column"]),
    "уникальные суммируются": changed(REVENUE, kind="count_distinct", unit="count", currency=None),
    "деньги без валюты": changed(REVENUE, currency=None),
    "валюта и колонка валюты": changed(
        REVENUE, currency_column={"table": "orders", "column": "status"}
    ),
    "валюта у счёта": changed(ORDERS_COUNT, currency="RUB"),
    "единица не подходит": changed(ORDERS_COUNT, unit="money"),
    "measure не из grain": changed(REVENUE, measure={"table": "clients", "column": "is_vip"}),
    "подтверждено без confirmation": changed(REVENUE, confirmation=None),
    "предложено с confirmation": changed(REVENUE, status="proposed"),
    "неверный metric_id": changed(REVENUE, metric_id="Выручка"),
    "лишнее поле": changed(REVENUE, sql="SELECT 1"),
}


@pytest.mark.parametrize("data", INVALID.values(), ids=INVALID.keys())
def test_inconsistent_definition_is_rejected(data):
    with pytest.raises(ValidationError):
        MetricDefinition.model_validate(data)


@pytest.mark.parametrize(
    ("op", "value"),
    [("is_null", 1), ("in", []), ("in", "x"), ("eq", None), ("eq", [1])],
)
def test_filter_value_must_match_operation(op, value):
    with pytest.raises(ValidationError):
        MetricFilter.model_validate(
            {"column": {"table": "t", "column": "c"}, "op": op, "value": value}
        )


CATALOG_MISMATCH = {
    "нет таблицы": (
        changed(ORDERS_COUNT, grain={"table": "sales", "key": "id"}, filters=[], time_column=None),
        "нет таблицы",
    ),
    "ключ не уникален": (
        changed(
            REVENUE,
            grain={"table": "orders", "key": "client_id"},
        ),
        "не уникальный ключ",
    ),
    "нет колонки": (
        changed(REVENUE, measure={"table": "orders", "column": "total"}),
        "нет колонки orders.total",
    ),
    "сумма по категории": (
        changed(REVENUE, measure={"table": "orders", "column": "status"}),
        "нечисловой",
    ),
    "календарь не дата": (
        changed(REVENUE, time_column={"table": "orders", "column": "amount"}),
        "не дата",
    ),
    "фильтр без связи": (
        changed(
            REVENUE,
            grain={"table": "clients", "key": "id"},
            kind="count",
            unit="count",
            measure=None,
            currency=None,
            time_column=None,
            filters=[{"column": {"table": "orders", "column": "status"}, "op": "eq", "value": "x"}],
        ),
        "без связи N:1",
    ),
}


@pytest.mark.parametrize(
    ("data", "message"), CATALOG_MISMATCH.values(), ids=CATALOG_MISMATCH.keys()
)
def test_definition_must_match_catalog(data, message):
    with pytest.raises(ValueError, match=message):
        MetricDefinition.model_validate(data).validate_against(CATALOG, definitions())


def test_filter_through_many_to_one_relation_is_allowed():
    vip = {"column": {"table": "clients", "column": "is_vip"}, "op": "eq", "value": True}
    metric = MetricDefinition.model_validate(changed(REVENUE, filters=[vip]))
    metric.validate_against(CATALOG)


def test_ratio_needs_confirmed_parts():
    metrics = definitions()
    ratio = metrics["average_check"]
    with pytest.raises(ValueError, match="revenue не подтверждена"):
        ratio.validate_against(CATALOG, {"orders_count": metrics["orders_count"]})
    proposed = MetricDefinition.model_validate(
        changed(REVENUE, status="proposed", confirmation=None)
    )
    with pytest.raises(ValueError, match="revenue не подтверждена"):
        ratio.validate_against(CATALOG, {**metrics, "revenue": proposed})
    nested = {**metrics, "orders_count": ratio.model_copy(update={"metric_id": "orders_count"})}
    with pytest.raises(ValueError, match="отношение"):
        ratio.validate_against(CATALOG, nested)


def test_shared_contracts_carry_metric_semantics():
    provenance = MetricProvenance.model_validate(EXAMPLE["provenance"])
    result = ToolResult(result_id="r_1", status="ok", metric=provenance)
    assert ToolResult.model_validate(result.model_dump(mode="json")).metric == provenance
    assert ToolResult(result_id="r_2", status="ok").metric is None
    clarification = Clarification(
        question="Что считать выручкой?",
        options=["Сумма заказов"],
        metric_candidates=[changed(REVENUE, status=None, confirmation=None, version=None)],
    )
    assert clarification.metric_candidates[0].metric_id == "revenue"
    # Кандидат модели не может сам себя подтвердить: таких полей у него нет.
    with pytest.raises(ValidationError):
        Clarification(question="?", options=["a"], metric_candidates=[REVENUE])
    assert (
        "confirmation" not in Clarification.model_json_schema()["$defs"]["MetricSpec"]["properties"]
    )
    assert Clarification(question="?", options=[]).metric_candidates == []


def test_low_confidence_relation_does_not_define_a_dimension():
    data = CATALOG.model_dump(mode="json")
    data["relations"][0]["confidence"] = "low"
    weak = DatasetCatalog.model_validate(data)
    vip = {"column": {"table": "clients", "column": "is_vip"}, "op": "eq", "value": True}
    with pytest.raises(ValueError, match="без связи N:1"):
        MetricDefinition.model_validate(changed(REVENUE, filters=[vip])).validate_against(weak)
