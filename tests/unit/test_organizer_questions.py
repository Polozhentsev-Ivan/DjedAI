"""Эталоны вопросов организаторов: неверный результат инструмента не проходит (#243)."""

from pathlib import Path

import pytest
import yaml

from djedai.contracts.tool_result import ToolResult, ToolResultData
from scripts.eval_runner import check_fact, evaluate_case

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


def _published_case(case_id):
    """A complete result/answer, with mutations below independent of check_fact."""
    expected = CASES[case_id]["expect"]["answer_rows"]
    rows = [[item["label"], *(value["value"] for value in item["values"])] for item in expected]
    # Use actual supported quantiles rather than the midpoint of the tolerance.
    if case_id == "organizer_q3":
        rows[0][1] = 72.49
    if case_id == "organizer_q4":
        rows[0][1] = 23.15
    units = {
        "organizer_q1": "%",
        "organizer_q2": "%",
        "organizer_q3": "часы",
        "organizer_q4": "часы",
        "organizer_q5": "заявки | руб.",
        "organizer_q6": "% | попытки",
    }[case_id]
    text = (
        "Группа | "
        + units
        + "\n"
        + "\n".join(" | ".join(str(cell) for cell in row) for row in rows)
    )
    return _result(["group", *[f"value_{i}" for i in range(len(rows[0]) - 1)]], rows), text


def _evaluate(case_id, result, text, extra_blocks=()):
    return evaluate_case(
        CASES[case_id],
        {"results": [result], "answer_blocks": [{"type": "text", "markdown": text}, *extra_blocks]},
    )


@pytest.mark.parametrize("case_id", CASES)
def test_complete_published_answers_pass(case_id):
    result, text = _published_case(case_id)
    status, issues, details = _evaluate(case_id, result, text)
    assert status == "passed", issues
    assert details["automated_status"] == "passed"


@pytest.mark.parametrize("case_id", CASES)
@pytest.mark.parametrize("text", ["Расчёт не выполнен.", "Лекарство 9999: рост 99 %.", ""])
def test_correct_tool_results_cannot_validate_a_wrong_answer(case_id, text):
    result, _ = _published_case(case_id)
    status, issues, details = _evaluate(case_id, result, text)
    assert status == details["automated_status"] == "failed"
    assert issues


@pytest.mark.parametrize("case_id", CASES)
def test_units_and_group_labels_are_required(case_id):
    result, text = _published_case(case_id)
    assert _evaluate(case_id, result, text.split("\n", 1)[1])[0] == "failed"
    label = CASES[case_id]["expect"]["answer_rows"][0]["label"]
    assert _evaluate(case_id, result, text.replace(label, "Другая группа"))[0] == "failed"


@pytest.mark.parametrize("case_id", ["organizer_q5", "organizer_q6"])
def test_every_group_and_its_own_values_must_reach_the_answer(case_id):
    result, text = _published_case(case_id)
    lines = text.splitlines()
    assert _evaluate(case_id, result, "\n".join(lines[:-1]))[0] == "failed"
    first, second = (row["label"] for row in CASES[case_id]["expect"]["answer_rows"][:2])
    swapped = text.replace(first, "TEMP").replace(second, first).replace("TEMP", second)
    assert _evaluate(case_id, result, swapped)[0] == "failed"
    # A table/graph block must not disable the explicit answer requirements.
    assert _evaluate(case_id, result, "Расчёт не выполнен.", [{"type": "table"}])[0] == "failed"
    rows = [list(row) for row in result.data.rows]
    rows[0][0], rows[1][0] = rows[1][0], rows[0][0]
    assert _evaluate(case_id, _result(result.data.columns, rows), text)[0] == "failed"


def test_original_review_reproduction_rejects_false_answers():
    result = _result(["id", "growth"], [[1019, 30.3211]])
    assert _evaluate("organizer_q1", result, "Лекарство 1019: рост 30,32 %.")[0] == "passed"
    for text in ["Лекарство 9999: рост 99 %.", "Расчёт не выполнен."]:
        assert _evaluate("organizer_q1", result, text)[0] == "failed"


def test_two_groups_in_one_line_cannot_supply_each_others_numbers():
    result = _result(["city", "growth"], [["Калининград", 62.29], ["Other", 1]])
    text = "Калининград: 1 %, Other: 62.29 %."
    assert _evaluate("organizer_q2", result, text)[0] == "failed"
    assert _evaluate("organizer_q2", result, "Калининград: 62,29 %, Other: 1 %.")[0] == "passed"


def test_an_unrelated_unit_cannot_hide_the_wrong_measurement_unit():
    result = _result(["group", "seconds", "hours"], [["Доставка заказов", 261198, 72.555]])
    for text in (
        "Доставка заказов: 72.555 seconds. Units: часы.",
        "Доставка заказов | 72.555 seconds\nЕдиницы: часы",
    ):
        assert _evaluate("organizer_q3", result, text)[0] == "failed"


def test_table_units_are_bound_to_their_columns():
    result, text = _published_case("organizer_q6")
    assert (
        _evaluate("organizer_q6", result, text.replace("% | попытки", "попытки | %"))[0] == "failed"
    )


@pytest.mark.parametrize(
    ("case_id", "label", "seconds", "hours"),
    [
        ("organizer_q3", "Доставка заказов", 260964, "72,49"),
        ("organizer_q4", "Собственная доставка", 83325.6, "23,15"),
    ],
)
def test_explicit_duration_columns_can_back_an_answer_in_hours(case_id, label, seconds, hours):
    result = _result(["group", "percentile_seconds"], [[label, seconds]])
    text = f"{label}: {hours} часа."
    status, issues, details = _evaluate(case_id, result, text)
    assert status == "passed", issues
    assert details["tool_results"][0]["data"]["columns"] == ["group", "percentile_seconds"]
    assert result.data.rows == [[label, seconds]]
    assert _evaluate(case_id, _result(["group", "value"], [[label, seconds]]), text)[0] == "failed"


def test_decimal_currency_cells_are_checked_as_numbers():
    result, text = _published_case("organizer_q5")
    rows = [[label, count, str(amount)] for label, count, amount in result.data.rows]
    assert _evaluate("organizer_q5", _result(result.data.columns, rows), text)[0] == "passed"


def test_numeric_group_labels_are_separate_clauses():
    result = _result(["id", "growth"], [[1019, 30.3211], [9999, 1]])
    assert _evaluate("organizer_q1", result, "1019: 1 %, 9999: 30.3211 %.")[0] == "failed"
    assert _evaluate("organizer_q1", result, "1019: 30.3211 %, 9999: 1 %.")[0] == "passed"


def test_percentile_number_in_header_does_not_disable_unit_inheritance():
    result, text = _published_case("organizer_q3")
    text = text.replace("Группа | часы", "Группа | 90-й перцентиль, часы")
    status, issues, _ = _evaluate("organizer_q3", result, text)
    assert status == "passed", issues


def test_calculation_constants_and_next_sentence_do_not_hide_wrong_answers():
    result = _result(["city", "growth"], [["Калининград", 62.29]])
    good = "Калининград | 62.29 %."
    assert _evaluate("organizer_q2", result, good)[0] == "passed"
    assert _evaluate("organizer_q2", result, f"{good} Всего 100 заказов.")[0] == "failed"
    assert _evaluate("organizer_q2", result, f"{good} Всего 31 заказ.")[0] == "failed"
    text = "Калининград показал стабильность. Москва выросла на 62,29 %."
    assert _evaluate("organizer_q2", result, text)[0] == "failed"
