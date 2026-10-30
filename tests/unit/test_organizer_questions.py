"""Эталоны вопросов организаторов: неверный результат инструмента не проходит (#243)."""

from pathlib import Path

import pytest
import yaml

from djedai.contracts.tool_result import ToolResult, ToolResultData
from scripts.eval_runner import check_fact

ROOT = Path(__file__).resolve().parents[2]
CASES = {
    case["id"]: case
    for case in yaml.safe_load((ROOT / "eval/organizer_questions.yaml").read_text(encoding="utf-8"))
}


def _result(columns, rows):
    return ToolResult(
        result_id="r_1",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=columns, rows=rows, row_count=len(rows)),
    )


def _unmet(case_id, result):
    return [
        fact["key"] for fact in CASES[case_id]["expect"]["facts"] if not check_fact(fact, [result])
    ]


def test_every_question_has_facts_and_a_manifest():
    assert len(CASES) == 6
    for number, case in enumerate(CASES.values(), start=1):
        assert case["expect"]["facts"]
        assert (ROOT / f"eval/datasets/organizer/q{number}.json").exists()


@pytest.mark.parametrize(
    ("case_id", "columns", "rows"),
    [
        ("organizer_q1", ["id", "growth"], [["1019", 30.3211]]),
        ("organizer_q2", ["city", "growth"], [["Калининград", 62.29]]),
        ("organizer_q3", ["group", "hours"], [["Доставка заказов", 72.49]]),
        ("organizer_q3", ["group", "hours"], [["Доставка заказов", 72.62]]),
        ("organizer_q4", ["carrier", "hours"], [["Собственная доставка", 23.15]]),
        ("organizer_q4", ["carrier", "hours"], [["Собственная доставка", 23.24]]),
        (
            "organizer_q5",
            ["dep", "n", "amount"],
            [["ИТ", 4158, "851088269.49"], ["Продажи", 999, "116747232.61"]],
        ),
        (
            "organizer_q6",
            ["job", "n", "avg"],
            [["Очистка", 581, 2.3201], ["Архивирование", 337, 2.279]],
        ),
    ],
)
def test_correct_results_pass(case_id, columns, rows):
    assert _unmet(case_id, _result(columns, rows)) == []


@pytest.mark.parametrize(
    ("case_id", "columns", "rows"),
    [
        # Группировка по действующему веществу вместо лекарства.
        ("organizer_q1", ["name", "growth"], [["Цетиризин", 23.05]]),
        ("organizer_q2", ["city", "growth"], [["Волгоград", 49.82]]),
        ("organizer_q3", ["group", "hours"], [["Доставка заказов", 73.0]]),
        ("organizer_q4", ["carrier", "hours"], [["Почтовая доставка", 22.59]]),
        # Заявки посчитаны без суммы.
        ("organizer_q5", ["dep", "n"], [["ИТ", 4158], ["Продажи", 999]]),
        # Частоты статусов вместо расчёта по цепочкам.
        ("organizer_q6", ["status", "count"], [["Успешно", 454882], ["Ошибка", 40414]]),
    ],
)
def test_wrong_results_are_caught(case_id, columns, rows):
    assert _unmet(case_id, _result(columns, rows))
