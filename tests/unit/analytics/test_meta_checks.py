"""Meta-проверки компараторов (#70).

Требование issue: «намеренно неверное значение или единица обнаруживается,
а ошибка инструмента не засчитывается как успех».

Если компаратор перестанет ловить подмену — упадёт этот тест.
"""

from __future__ import annotations

import pytest

from djedai.analytics.registry import execute_tool


def test_contract_check_ok_rejects_error_status(contract_check):
    result = execute_tool(
        "describe_column",
        {"table": "test", "column": "nonexistent"},
    )
    with pytest.raises(AssertionError, match="status=error"):
        contract_check.ok(result)


def test_contract_check_error_accepts_expected_code(contract_check):
    result = execute_tool(
        "describe_column",
        {"table": "test", "column": "nonexistent"},
    )
    contract_check.error(result, "invalid_column")


def test_contract_check_error_rejects_wrong_code(contract_check):
    result = execute_tool(
        "describe_column",
        {"table": "test", "column": "nonexistent"},
    )
    with pytest.raises(AssertionError, match="ожидался код invalid_parameter"):
        contract_check.error(result, "invalid_parameter")


def test_contract_check_fact_detects_missing_kind(contract_check):
    result = execute_tool(
        "time_aggregate",
        {"table": "events", "time_col": "ts", "freq": "month", "agg": "sum", "measure": "amount"},
    )
    with pytest.raises(AssertionError, match="нет fact kind=nonexistent"):
        contract_check.fact(result, "nonexistent")


def test_contract_check_chart_detects_wrong_type(contract_check):
    """Намеренно неверный chart_hint должен ловиться."""
    result = execute_tool(
        "time_aggregate",
        {"table": "events", "time_col": "ts", "freq": "month", "agg": "sum", "measure": "amount"},
    )
    with pytest.raises(AssertionError, match="chart_hint.type"):
        contract_check.chart(result, "pie", "time_bucket", ["value"])


def test_contract_check_chart_accepts_correct_hint(contract_check):
    result = execute_tool(
        "time_aggregate",
        {"table": "events", "time_col": "ts", "freq": "month", "agg": "sum", "measure": "amount"},
    )
    contract_check.chart(result, "line", "time_bucket", ["value"])


def test_error_status_is_not_counted_as_success(contract_check):
    """Критерий issue: ошибка не засчитывается как успех.

    Используем кейс, где pydantic пропускает, а функция возвращает
    ToolResult(status="error"): agg="sum" без measure в time_aggregate.
    """
    result = execute_tool(
        "time_aggregate",
        {"table": "events", "time_col": "ts", "agg": "sum"},
    )
    assert result.status == "error"
    with pytest.raises(AssertionError, match="status=error"):
        contract_check.ok(result)
