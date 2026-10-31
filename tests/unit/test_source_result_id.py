"""Работа с source_result_id (#70).

Текущее поведение: execute_tool отклоняет source_result_id явно
(см. README djedai/analytics). Полноценная реализация требует контекста
владения сохранённым результатом и отдельного контракта в PL-1.

Тест фиксирует текущее поведение, чтобы случайное изменение не прошло
незамеченным. Когда реализация появится — заменить на проверки
вычисления поверх сохранённого результата.
"""

from __future__ import annotations

import pytest

from djedai.analytics.registry import execute_tool


@pytest.mark.parametrize(
    ("tool_name", "params"),
    [
        ("run_sql", {"sql": "SELECT 1", "source_result_id": "r_x"}),
        ("time_aggregate", {"table": "events", "time_col": "ts", "source_result_id": "r_x"}),
        ("correlation", {"table": "test", "columns": ["age", "id"], "source_result_id": "r_x"}),
        (
            "forecast",
            {
                "table": "events",
                "time_col": "ts",
                "measure": "amount",
                "horizon": 3,
                "source_result_id": "r_x",
            },
        ),
    ],
)
def test_source_result_id_is_explicitly_rejected(tool_name, params):
    with pytest.raises(ValueError, match="source_result_id"):
        execute_tool(tool_name, params)
