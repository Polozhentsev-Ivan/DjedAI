"""The acceptance model must depend on real context and result IDs, not an oracle."""

import json
import runpy
from pathlib import Path

import pytest

from djedai.contracts.agent import VerifyResponse

fixture = runpy.run_path(str(Path(__file__).parents[1] / "e2e/serve_journey.py"))
respond = fixture["response_for"]


def test_verifier_returns_the_required_string_without_changing_the_draft():
    draft = "Результат показан в таблице."
    value = respond("VerifyResponse", f"Drafted Response:\n{draft}\n\nRules:\n", "Verify")
    assert VerifyResponse.model_validate(value).corrected_markdown == draft


def test_follow_up_requires_period_and_current_chat_result():
    question = "А теперь то же самое по регионам."
    for context in ("", "январь 2025", "1. result_id=r_a; tool=run_sql"):
        with pytest.raises(ValueError):
            respond("Understanding", context, question)
    context = "Пользователь: за январь 2025\n1. result_id=r_a; tool=run_sql"
    value = respond("Understanding", context, question)
    assert "region" in value["resolved_question"]
    assert "январь 2025" in value["resolved_question"]
    value = respond("Understanding", context, "Покажи линейным графиком.")
    assert value["references"] == ["r_a"]
    assert respond("PlanResponse", context, "Покажи линейным графиком.")["plan"] == []


def test_new_chat_cannot_silently_use_another_chats_history():
    with pytest.raises(ValueError, match="another chat"):
        respond("Understanding", "1. result_id=foreign; tool=run_sql", "График category")


def test_plan_uses_catalog_and_period_and_does_not_embed_expected_totals():
    value = respond(
        "PlanResponse",
        "Таблица orders (4 строки)\nТаблица targets (2 строки)",
        "Покажи сумму amount по region за январь 2025.",
    )
    sql = value["plan"][0]["args"]["sql"]
    assert 'FROM "orders"' in sql
    assert "GROUP BY region" in sql
    assert "ordered_at >= DATE '2025-01-01'" in sql
    assert "ordered_at < DATE '2025-02-01'" in sql
    assert "SUM(amount)" in sql
    with pytest.raises(ValueError, match="catalog table"):
        respond("PlanResponse", "Таблица foreign (1 строка)", "за январь 2025")


@pytest.mark.parametrize("result_id", ["r_real", "r_other_owner"])
def test_chart_and_table_reference_the_actual_runtime_result(result_id):
    data = {"columns": ["region", "total"], "rows": [["West", 987654]]}
    chart = respond(
        "VisualizeResponse",
        "Tool Results (Data & Hints):\n"
        + json.dumps([{"result_id": result_id, **data}])
        + "\nRules:",
        "линейным графиком",
    )["charts"][0]
    assert chart["result_id"] == result_id
    assert chart["type"] == "line"
    assert "987654" not in json.dumps(chart)
    answer = respond(
        "ComposeResponse",
        "Facts and Data:\n" + json.dumps([{"result_id": result_id, "data": data}]),
        "Покажи результат",
    )
    assert answer["answer_blocks"][1] == {"type": "table", "result_id": result_id}
