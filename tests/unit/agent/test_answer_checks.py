"""Проверки ответа кодом: длительности и числа без опоры в данных (#243)."""

from djedai.agent.answer_checks import duration_hints, unsupported_numbers

ENTRIES = [
    {
        "data": {
            "columns": ["department", "requests", "amount", "share"],
            "rows": [["a", 4158, 851088269.49, 30.321090665], ["b", 3001, 328175804.79, 0.0524]],
            "row_count": 2,
        }
    }
]


def test_duration_hints_convert_long_seconds_only():
    hints = duration_hints(["group", "p90_time_sec"], [["x", 260964.0], ["y", 12]])
    assert hints == [{"column": "p90_time_sec", "seconds": 260964.0, "hours": 72.49}]


def test_duration_hints_ignore_other_columns():
    assert duration_hints(["second_hand", "sector"], [[900000, 900000]]) == []


def test_numbers_from_data_pass_with_rounding_grouping_and_percent():
    draft = "a: 4 158 заявок на 851 088 269,49 руб., рост 30,32 %; b 5,24 %"
    assert unsupported_numbers(draft, ENTRIES) == []


def test_decimal_values_serialized_as_strings_are_data():
    entries = [{"data": {"columns": ["sum_rub"], "rows": [["851088269.49"], ["328175804.79"]]}}]
    assert unsupported_numbers("851 088 269,49 руб., всего 1 179 264 074,28 руб.", entries) == []


def test_column_total_is_supported():
    assert unsupported_numbers("Всего 7 159 заявок", ENTRIES) == []


def test_number_the_model_calculated_itself_is_reported():
    assert unsupported_numbers("Рост составил 23,34 %", ENTRIES) == ["23,34"]


def test_dates_years_and_small_integers_are_not_values():
    draft = "В марте 2025, 31 день, с 2025-03-01 по 31.03.2025, топ-5"
    assert unsupported_numbers(draft, ENTRIES) == []


def test_formula_in_code_span_is_skipped():
    assert unsupported_numbers("`(7,60 - 6,18) / 6,18`", ENTRIES) == []


def test_nothing_to_compare_with_means_no_verdict():
    assert unsupported_numbers("Рост 23,34 %", [{"facts": []}]) == []
