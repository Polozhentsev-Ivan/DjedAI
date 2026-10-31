"""Детерминированная проверка чисел (#167): сценарии приёмки и границы."""

from __future__ import annotations

from decimal import Decimal

from djedai.agent.data_policy import DataTransferPolicy
from djedai.agent.graph import _answer_payload
from djedai.agent.verification import build_evidence, verify_numeric_text
from djedai.contracts.metrics import MetricFilter, MetricGrain, MetricProvenance
from djedai.contracts.tool_result import Fact, ToolResult, ToolResultData

MONEY = [
    ToolResult(
        result_id="r_money",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["amount"], rows=[[500000]], row_count=1),
        facts=[Fact(text="Выручка: 500000 RUB", kind="summary", values={"value": 500000})],
    )
]


def _evidence(results: list[ToolResult], *, policy: DataTransferPolicy | None = None):
    state = {
        "results": results,
        "history_results": [],
        "charts": [],
        "answer_blocks": [],
        "focus_result_ids": [],
    }
    payload, usable = _answer_payload(state, policy or DataTransferPolicy.sharing())
    assert usable
    return build_evidence(results, payload)


def _report(text: str, evidence, results: list[ToolResult]):
    return verify_numeric_text(text, evidence, result_ids=[r.result_id for r in results])


def _assertion_by_raw(report, raw: str):
    return next(item for item in report.assertions if item.raw_text == raw)


def test_thousands_require_an_explicit_scale_word() -> None:
    evidence = _evidence(MONEY)
    assert _report("Выручка 500 000 RUB", evidence, MONEY).status == "verified"
    assert _report("Выручка 500 тыс. RUB", evidence, MONEY).status == "verified"
    wrong = _report("Выручка 500 RUB", evidence, MONEY)
    assert wrong.status == "failed"
    assert wrong.assertions[0].reason_code == "number_not_found"


def test_wrong_currency_is_rejected_only_when_the_answer_claims_one() -> None:
    evidence = _evidence(MONEY)
    wrong = _report("Выручка 500 000 USD", evidence, MONEY)
    assert wrong.status == "failed"
    assert wrong.assertions[0].reason_code == "currency_mismatch"
    assert _report("Выручка 500 000", evidence, MONEY).status == "verified"


def _period_result() -> ToolResult:
    return ToolResult(
        result_id="r_period",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["amount"], rows=[[1000]], row_count=1),
        facts=[Fact(text="Выручка за 2023 год: 1000 RUB", kind="summary")],
    )


def test_period_must_be_confirmed_by_the_same_source() -> None:
    results = [_period_result()]
    evidence = _evidence(results)
    assert _report("Выручка 1000 RUB за 2023 год", evidence, results).status == "verified"
    wrong = _report("Выручка 1000 RUB за 2024 год", evidence, results)
    assert wrong.status == "failed"
    assert any(item.reason_code == "period_mismatch" for item in wrong.assertions)


def test_population_must_be_confirmed_not_guessed() -> None:
    evidence = _evidence(MONEY)
    wrong = _report("Выручка 500 000 RUB без отменённых заказов", evidence, MONEY)
    assert wrong.status == "failed"
    assert wrong.assertions[0].reason_code == "population_mismatch"


def _ratio_result() -> ToolResult:
    metric = MetricProvenance(
        metric_id="cancel_rate",
        version=1,
        title="Доля отмен",
        formula="cancelled / orders",
        grain=MetricGrain(table="orders", key="id"),
        additivity="recompute",
        unit="ratio",
        filters=[
            MetricFilter.model_validate(
                {"column": {"table": "orders", "column": "status"}, "op": "ne", "value": "test"}
            )
        ],
        period_start="2024-01-01",
        period_end="2025-01-01",
    )
    return ToolResult(
        result_id="r_ratio",
        status="ok",
        tool="compute_metric",
        data=ToolResultData(
            columns=["numerator", "denominator", "value"], rows=[[30, 100, 0.3]], row_count=1
        ),
        facts=[Fact(text="Доля отмен: 0.3", kind="metric", values={"value": 0.3})],
        metric=metric,
    )


def test_percentage_requires_a_confirmed_denominator_and_period() -> None:
    results = [_ratio_result()]
    evidence = _evidence(results)
    assert _report("Доля отмен 30% от всех заказов", evidence, results).status == "verified"
    wrong_denominator = _report("Доля отмен 30% от отменённых заказов", evidence, results)
    assert wrong_denominator.status == "failed"
    assert wrong_denominator.assertions[0].reason_code == "denominator_mismatch"
    wrong_period = _report("Доля отмен 30% от всех заказов за 2023 год", evidence, results)
    assert wrong_period.status == "failed"
    assert any(item.reason_code == "period_mismatch" for item in wrong_period.assertions)
    unknown = _assertion_by_raw(_report("Доля отмен 30%", evidence, results), "30")
    assert unknown.denominator is None
    assert unknown.evidence[0].denominator is not None


def test_percentage_denominator_evidence_is_explicit() -> None:
    evidence = _evidence([_ratio_result()])
    value = next(item for item in evidence if item.unit == "ratio" and item.denominator is not None)
    assert value.denominator.value == Decimal("100")
    assert value.denominator.cell_address == "data.rows[0].denominator"
    assert "denominator" not in value.unknown


def test_a_matching_extraneous_number_cannot_be_used() -> None:
    one_result = [
        ToolResult(
            result_id="r_revenue",
            status="ok",
            tool="run_sql",
            data=ToolResultData(columns=["revenue"], rows=[[1000]], row_count=1),
            facts=[Fact(text="Выручка 1000 RUB", kind="summary")],
        )
    ]
    evidence = _evidence(one_result)
    assert _report("Выручка 1000 RUB", evidence, one_result).status == "verified"
    wrong = _report("Прибыль 1000 RUB", evidence, one_result)
    assert wrong.status == "failed"
    assert wrong.assertions[0].reason_code == "unrelated_result"

    two_results = [
        *one_result,
        ToolResult(
            result_id="r_cost",
            status="ok",
            tool="run_sql",
            data=ToolResultData(columns=["cost"], rows=[[1000]], row_count=1),
            facts=[Fact(text="Себестоимость 1000 RUB", kind="summary")],
        ),
    ]
    evidence = _evidence(two_results)
    ambiguous = _report("Итого 1000 RUB", evidence, two_results)
    assert ambiguous.status == "failed"
    assert ambiguous.assertions[0].reason_code == "ambiguous_number"
    assert _report("Себестоимость 1000 RUB", evidence, two_results).status == "verified"


def test_2024_can_be_a_quantity_or_a_calendar_period() -> None:
    quantity = ToolResult(
        result_id="r_year_value",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["orders"], rows=[[2024]], row_count=1),
        facts=[Fact(text="Заказов: 2024", kind="summary", values={"orders": 2024})],
    )
    evidence = _evidence([quantity])
    as_quantity = _report("2024 заказа", evidence, [quantity])
    assert as_quantity.status == "verified"
    assert as_quantity.assertions[0].kind == "quantity"

    period_result = ToolResult(
        result_id="r_year_period",
        status="ok",
        tool="compute_metric",
        data=ToolResultData(columns=["value"], rows=[[1000]], row_count=1),
        metric=MetricProvenance(
            metric_id="revenue",
            version=1,
            title="Выручка",
            formula="SUM(orders.amount)",
            grain=MetricGrain(table="orders", key="id"),
            additivity="additive",
            unit="money",
            currency="RUB",
            period_start="2024-01-01",
            period_end="2025-01-01",
        ),
    )
    evidence = _evidence([period_result])
    as_year = _report("В 2024 году выручка 1000 RUB", evidence, [period_result])
    assert as_year.status == "verified"
    assert any(item.kind == "period" for item in as_year.assertions)
    wrong_year = _report("В 2023 году выручка 1000 RUB", evidence, [period_result])
    assert wrong_year.status == "failed"


def test_list_numbers_and_identifiers_are_not_analytical_assertions() -> None:
    evidence = _evidence(MONEY)
    report = _report("1. Выручка 500 000 RUB\n2. Версия v2\n3. См. рис. 3", evidence, MONEY)
    assert report.status == "verified"
    ignored = [item for item in report.assertions if item.status == "ignored"]
    assert {item.kind for item in ignored} >= {"list_number", "constant"}
    assert all(item.reason_code == "non_analytical" for item in ignored)
    assert not any(item.raw_text == "2" and item.status != "ignored" for item in report.assertions)


def _table_result() -> ToolResult:
    return ToolResult(
        result_id="r_table",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["total", "parts"], rows=[[1000, 6]], row_count=1),
        facts=[Fact(text="Итого 1000, частей 6", values={"total": 1000, "parts": 6})],
    )


def test_formula_is_checked_with_operands_units_and_rounding() -> None:
    results = [_table_result()]
    evidence = _evidence(results)
    assert _report("1000 / 6 = 166,67", evidence, results).status == "verified"
    assert _report("1000 / 6 ≈ 166,7", evidence, results).status == "verified"
    rounding = _report("1000 / 6 = 166,4", evidence, results)
    assert rounding.status == "failed"
    assert rounding.assertions[0].reason_code == "rounding_mismatch"
    missing = _report("1000 / 7 = 142,86", evidence, results)
    assert missing.status == "failed"
    assert missing.assertions[0].reason_code == "number_not_found"
    zero = _report("1000 / 0 = 0", evidence, results)
    assert zero.status == "failed"
    assert zero.assertions[0].reason_code == "zero_denominator"


def test_formula_operands_keep_their_provenance() -> None:
    results = [_table_result()]
    report = _report("1000 / 6 = 166,67", _evidence(results), results)
    (assertion,) = report.assertions
    assert assertion.kind == "formula"
    assert assertion.formula == "1000 / 6"
    assert [operand.role for operand in assertion.operands] == ["numerator", "denominator"]
    assert all(operand.evidence for operand in assertion.operands)


def test_evidence_marks_unknown_provenance_explicitly() -> None:
    evidence = _evidence(MONEY)
    data_cell = next(item for item in evidence if item.source == "data_cell")
    assert "period" in data_cell.unknown
    assert "population" in data_cell.unknown
    assert "denominator" in data_cell.unknown


def test_unrelated_number_inside_a_cell_does_not_confirm_a_label() -> None:
    results = [
        ToolResult(
            result_id="r_customers",
            status="ok",
            tool="run_sql",
            data=ToolResultData(columns=["customers"], rows=[[500000]], row_count=1),
        )
    ]
    wrong = _report("Выручка 500000 RUB", _evidence(results), results)
    assert wrong.status == "failed"
    assert wrong.assertions[0].reason_code == "unrelated_result"


def test_no_numbers_is_not_applicable_and_bare_numbers_need_evidence() -> None:
    evidence = _evidence(MONEY)
    assert _report("Готово", evidence, MONEY).status == "not_applicable"
    missing = _report("999", evidence, MONEY)
    assert missing.status == "failed"
    assert missing.assertions[0].reason_code == "number_not_found"


def test_period_grain_is_not_widened_by_a_larger_source_period() -> None:
    results = [_period_result()]
    report = _report("В октябре 2024 выручка 1000 RUB", _evidence(results), results)
    assert report.status == "failed"
    assert any(item.reason_code == "period_mismatch" for item in report.assertions)


def _month_bucket(*, freq_in_fact: bool) -> ToolResult:
    return ToolResult(
        result_id="r_month",
        status="ok",
        tool="time_aggregate",
        params={} if freq_in_fact else {"freq": "month"},
        data=ToolResultData(
            columns=["time_bucket", "value"], rows=[["2024-10-01T00:00:00", 2]], row_count=1
        ),
        facts=(
            [Fact(text="Бакетов: 1", kind="time_summary", values={"freq": "month"})]
            if freq_in_fact
            else []
        ),
    )


def test_month_bucket_is_not_a_specific_day() -> None:
    for freq_in_fact in (False, True):
        results = [_month_bucket(freq_in_fact=freq_in_fact)]
        evidence = _evidence(results)
        assert _report("2024-10: 2", evidence, results).status == "verified"
        day = _report("2024-10-01: 2", evidence, results)
        assert day.status == "failed"
        assert any(item.reason_code == "period_mismatch" for item in day.assertions)


def test_formula_operands_follow_the_single_named_period() -> None:
    result = ToolResult(
        result_id="r_months",
        status="ok",
        tool="time_aggregate",
        params={"freq": "month"},
        data=ToolResultData(
            columns=["time_bucket", "value"],
            rows=[["2024-10-01T00:00:00", 1000], ["2024-11-01T00:00:00", 2000]],
            row_count=2,
        ),
    )
    results = [result]
    evidence = _evidence(results)
    wrong_period = _report("2024-10: 2000 / 1000 = 2", evidence, results)
    assert wrong_period.status == "failed"
    assert any(item.reason_code == "period_mismatch" for item in wrong_period.assertions)
    assert _report("2024-10: 1000 / 1000 = 1", evidence, results).status == "verified"


def test_fact_numbers_keep_their_own_caption() -> None:
    result = ToolResult(
        result_id="r_profile",
        status="ok",
        tool="describe_column",
        data=ToolResultData(columns=["column"], rows=[["amount"]], row_count=1),
        facts=[
            Fact(
                text="Мин: 10, Макс: 100, Среднее: 55, Медиана: 55, Std: 31.62",
                kind="numeric_profile",
                values={"min": 10, "max": 100, "mean": 55, "median": 55, "std": 31.62},
            )
        ],
    )
    results = [result]
    evidence = _evidence(results)
    assert _report("Мин 10", evidence, results).status == "verified"
    assert _report("Макс 100", evidence, results).status == "verified"
    assert _report("Среднее 55", evidence, results).status == "verified"
    hundreds = [item for item in evidence if item.source == "fact" and item.value == Decimal(100)]
    assert hundreds
    assert all("Среднее" not in (item.label or "") for item in hundreds)
    assert any("Макс" in (item.label or "") for item in hundreds)
    wrong = _report("Среднее 100", evidence, results)
    assert wrong.status == "failed"
    assert wrong.assertions[0].reason_code in {"unrelated_result", "number_not_found"}


def test_count_semantics_do_not_depend_on_sql_alias() -> None:
    sql = "SELECT COUNT(*) AS n, SUM(amount) AS total FROM orders"
    result = ToolResult(
        result_id="r_count",
        status="ok",
        tool="run_sql",
        sql=sql,
        params={"sql": sql},
        data=ToolResultData(columns=["n", "total"], rows=[[3, 50]], row_count=1),
    )
    results = [result]
    evidence = _evidence(results)
    cells = {item.cell_address: item for item in evidence if item.source == "data_cell"}
    assert "count" in (cells["data.rows[0].n"].label or "")
    assert "sum" in (cells["data.rows[0].total"].label or "")
    assert _report("Rows: 3; total: 50.", evidence, results).status == "verified"
    assert _report("Количество: 3; сумма: 50.", evidence, results).status == "verified"
    wrong = _report("Rows: 50; total: 3.", evidence, results)
    assert wrong.status == "failed"
    assert all(item.reason_code for item in wrong.assertions)


def test_calendar_labels_are_not_measurements() -> None:
    quarter = ToolResult(
        result_id="r_quarter",
        status="ok",
        tool="compute_metric",
        data=ToolResultData(columns=["value"], rows=[[500000]], row_count=1),
        metric=MetricProvenance(
            metric_id="revenue",
            version=1,
            title="Выручка",
            formula="SUM(orders.amount)",
            grain=MetricGrain(table="orders", key="id"),
            additivity="additive",
            unit="money",
            currency="RUB",
            period_start="2024-10-01",
            period_end="2025-01-01",
        ),
    )
    evidence = _evidence([quarter])
    for text in ("2024-Q4: 500 000 RUB", "2024 Q4: 500 тыс. RUB"):
        report = _report(text, evidence, [quarter])
        assert report.status == "verified"
        period = next(item for item in report.assertions if item.kind == "period")
        assert period.status == "verified"
    assert _report("2024-Q4: 999 RUB", evidence, [quarter]).status == "failed"


def test_sanitized_payload_is_the_only_evidence_scope() -> None:
    """Строки, скрытые политикой, не могут подтвердить число."""
    result = ToolResult(
        result_id="r_hidden",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["secret"], rows=[[12345]], row_count=1),
        facts=[Fact(text="Секрет 12345", kind="summary")],
    )
    restricted = DataTransferPolicy.restricted()
    state = {
        "results": [result],
        "history_results": [],
        "charts": [],
        "answer_blocks": [],
        "focus_result_ids": [],
    }
    payload, _ = _answer_payload(state, restricted)
    # Факты разрешены, а строки — нет: значение скрыто политикой.
    assert payload[0]["data"]["rows"] == [["[REDACTED]"]]
    evidence = build_evidence([result], payload)
    assert all(item.source != "data_cell" for item in evidence)
    # Факт тоже очищен политикой: число, которого модель не видела, не
    # может быть подтверждено даже ссылкой на исходный результат.
    assert _report("12345", evidence, [result]).status == "failed"


def _sql_evidence(sql: str, name: str, value: int):
    result = ToolResult(
        result_id="r_sql_semantics",
        status="ok",
        tool="run_sql",
        sql=sql,
        data=ToolResultData(columns=[name], rows=[[value]], row_count=1),
    )
    return [result], _evidence([result])


def test_sql_aggregate_alias_cannot_supply_a_conflicting_caption() -> None:
    for alias, wrong in (("mean", "Среднее"), ("min", "Минимум")):
        results, evidence = _sql_evidence(f"SELECT MAX(amount) AS {alias} FROM orders", alias, 100)
        assert _report("Максимум 100", evidence, results).status == "verified"
        assert _report(f"{wrong} 100", evidence, results).status == "failed"
        cell = next(item for item in evidence if item.source == "data_cell")
        assert alias not in (cell.label or "").split()


def test_count_nullable_column_does_not_confirm_all_rows() -> None:
    for expression in ("amount", "DISTINCT amount", "CASE WHEN amount > 0 THEN 1 END"):
        results, evidence = _sql_evidence(f"SELECT COUNT({expression}) AS n FROM orders", "n", 2)
        assert _report("В таблице 2 строки.", evidence, results).status == "failed"
        assert _report("Rows: 2.", evidence, results).status == "failed"
        assert _report("Количество: 2.", evidence, results).status == "verified"
    results, evidence = _sql_evidence("SELECT COUNT(*) AS n FROM orders", "n", 3)
    assert _report("В таблице 3 строки.", evidence, results).status == "verified"


def test_formula_cannot_combine_operands_from_unrelated_results() -> None:
    results = [
        ToolResult(
            result_id=f"r_{value}",
            status="ok",
            tool="run_sql",
            data=ToolResultData(columns=["value"], rows=[[value]], row_count=1),
        )
        for value in (20, 30)
    ]
    assert _report("20 + 30 = 50", _evidence(results), results).status == "failed"


def test_formula_uses_units_and_currencies_from_operand_evidence() -> None:
    for second_caption in ("Расходы: 30 USD", "Заказов: 30 штук"):
        results = [
            ToolResult(
                result_id="r_units",
                status="ok",
                tool="run_sql",
                facts=[Fact(text="Выручка: 20 RUB"), Fact(text=second_caption)],
            )
        ]
        assert _report("20 + 30 = 50", _evidence(results), results).status == "failed"


def test_formula_constant_cannot_override_an_operand_period_mismatch() -> None:
    results = [_month_bucket(freq_in_fact=False)]
    results[0].data.rows[0][1] = 1000
    report = _report("2024-11: 1000 / 1000 = 1", _evidence(results), results)
    assert report.status == "failed"
