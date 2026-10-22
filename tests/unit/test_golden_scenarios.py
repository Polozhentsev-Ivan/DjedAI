"""Regression contracts for the complete golden suite; no agent or full CSV required."""

import copy
import csv
import importlib.util
import io
import json
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest
import yaml

from djedai.analytics.registry import TOOLS_REGISTRY
from djedai.contracts.chart import ChartType

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "verify_golden_scenarios", ROOT / "scripts/verify_golden.py"
)
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)

LEGACY_IDS = {
    "s1_overview_01",
    "s1_volume_01",
    "s1_period_01",
    "s1_fields_01",
    "s1_relations_01",
    "s2_status_01",
    "s2_environment_01",
    "s2_processed_profile_01",
    "s2_error_codes_01",
    "s2_chain_identity_01",
    "s2_production_errors_01",
    "edge_ambiguous_measure_01",
    "edge_ambiguous_population_01",
    "edge_missing_field_01",
    "edge_missing_relation_01",
    "edge_destructive_01",
}


@pytest.fixture
def suite():
    return golden.load_suite()


@pytest.fixture
def dialect():
    manifest = json.loads(golden.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    return manifest["csv"]


def write_suite(tmp_path, cases, manifest):
    questions = tmp_path / "questions.yaml"
    metadata = tmp_path / "manifest.json"
    questions.write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    metadata.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return questions, metadata


def test_full_suite_covers_each_scenario_without_losing_v1_cases(suite):
    cases, manifest = suite
    counts = Counter(case["scenario"] for case in cases)
    assert len(cases) >= 52
    assert all(counts[number] >= 5 for number in range(1, 7))
    assert all(counts[number] >= 3 for number in range(7, 11))
    assert counts["edge"] >= 10
    assert LEGACY_IDS <= {case["id"] for case in cases}
    assert set(manifest["scenario_source"]["covered"]) == set(range(1, 11))
    assert {case["expect"]["outcome"] for case in cases if case["scenario"] == "edge"} == {
        "clarify",
        "reject",
        "input_error",
    }


def test_stale_coverage_and_scalar_line_rubrics_cannot_claim_a_valid_suite(tmp_path, suite):
    cases, manifest = copy.deepcopy(suite)
    manifest["scenario_source"]["covered"] = [1, 2]
    with pytest.raises(golden.GoldenError):
        golden.load_suite(*write_suite(tmp_path, cases, manifest))
    cases, manifest = copy.deepcopy(suite)
    case = next(case for case in cases if case["expect"].get("chart"))
    case["expect"]["chart"].update(types_any=["line"], min_points=2)
    key = case["expect"]["chart"]["data_fact"]
    next(f for f in case["expect"]["facts"] if f["key"] == key).update(comparison="exact", value=42)
    with pytest.raises(golden.GoldenError):
        golden.load_suite(*write_suite(tmp_path, cases, manifest))


def test_exact_nested_count_never_accepts_boolean_substitution():
    assert not golden.fact_matches(
        {"comparison": "exact", "value": [["2026-10-01", 1]]},
        [["2026-10-01", True]],
    )


def test_full_reference_report_is_valid_json_in_a_legacy_windows_console(
    tmp_path, suite, monkeypatch
):
    cases, manifest = copy.deepcopy(suite)
    source = write_rows(tmp_path, manifest["csv"], [attempt("one", "2025-01-01 00:00:00")])
    manifest["source"].update(golden.file_identity(source))
    questions_path, manifest_path = write_suite(tmp_path, cases, manifest)

    class LegacyConsole(io.StringIO):
        encoding = "cp1251"

        def write(self, text):
            text.encode(self.encoding)  # Reproduce the actual encoding failure.
            return super().write(text)

    console = LegacyConsole()
    output = tmp_path / "report.json"
    with monkeypatch.context() as patch:
        patch.setattr(golden.sys, "stdout", console)
        result = golden.main(
            [
                "--questions",
                str(questions_path),
                "--manifest",
                str(manifest_path),
                "--data-file",
                str(source),
                "--output",
                str(output),
            ]
        )
    assert result == 1  # Small input intentionally disagrees with full-file references.
    report = json.loads(console.getvalue())
    assert report["status"] == "reference_mismatch"
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert "\\u00d7" in console.getvalue()
    assert "×" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario", [*range(1, 11), "edge"])
def test_missing_scenario_questions_cannot_pass_structure_validation(tmp_path, suite, scenario):
    cases, manifest = copy.deepcopy(suite)
    minimum = 10 if scenario == "edge" else 5 if scenario <= 6 else 3
    group = [case for case in cases if case["scenario"] == scenario]
    dropped = {case["id"] for case in group[minimum - 1 :]}
    cases = [case for case in cases if case["id"] not in dropped]
    paths = write_suite(tmp_path, cases, manifest)
    with pytest.raises(golden.GoldenError):
        golden.load_suite(*paths)


def test_future_tools_are_explicit_and_do_not_impersonate_registered_tools(suite):
    cases, manifest = suite
    registered = {definition.name for definition in TOOLS_REGISTRY}
    planned = set(manifest["planned_tools"])
    assert planned
    # Later feature PRs may register these tools; the suite must not regress
    # merely because its requirements have become available to the runner.
    assert not planned & golden.TOOLS
    assert any(case["expect"].get("tools_planned") for case in cases)
    for case in cases:
        expected = case["expect"]
        assert set(expected["tools_any"]) <= registered
        assert set(expected.get("tools_forbidden", [])) <= registered
        assert set(expected.get("tools_planned", [])) <= planned


def test_followups_replay_real_previous_turns_and_reuse_only_available_results(suite):
    cases, _ = suite
    seen = set()
    followups = []
    for case in cases:
        if context := case.get("context"):
            followups.append(case)
            assert context["same_chat"] is True
            assert set(context["previous_cases"]) <= seen
            assert context["previous_cases"]
            assert set(context) == {"same_chat", "previous_cases"}
            if reused := case["expect"].get("reuse_result_from"):
                assert reused in context["previous_cases"]
        seen.add(case["id"])
    assert len(followups) >= 5
    assert any(case["expect"].get("reuse_result_from") for case in followups)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_previous",
        "self_previous",
        "forward_previous",
        "duplicate_previous",
        "new_chat",
        "injected_assistant",
        "reuse_without_context",
        "reuse_unavailable_result",
        "planned_as_registered",
        "unknown_planned_tool",
    ],
)
def test_unreplayable_context_and_undeclared_tools_are_rejected(tmp_path, suite, change):
    cases, manifest = copy.deepcopy(suite)
    followup = next(case for case in cases if case.get("context"))
    if change == "unknown_previous":
        followup["context"]["previous_cases"] = ["missing_question"]
    elif change == "self_previous":
        followup["context"]["previous_cases"] = [followup["id"]]
    elif change == "forward_previous":
        followup["context"]["previous_cases"] = [cases[-1]["id"]]
    elif change == "duplicate_previous":
        prior = followup["context"]["previous_cases"][0]
        followup["context"]["previous_cases"] = [prior, prior]
    elif change == "new_chat":
        followup["context"]["same_chat"] = False
    elif change == "injected_assistant":
        followup["context"]["assistant"] = "Prepared answer: 500000 attempts."
    elif change == "reuse_without_context":
        cases[0]["expect"]["reuse_result_from"] = cases[1]["id"]
    elif change == "reuse_unavailable_result":
        followup["expect"]["reuse_result_from"] = cases[-1]["id"]
    elif change == "planned_as_registered":
        cases[0]["expect"]["tools_any"] = [next(iter(manifest["planned_tools"]))]
    else:
        cases[0]["expect"]["tools_planned"] = ["imaginary_future_tool"]
    with pytest.raises(golden.GoldenError):
        golden.load_suite(*write_suite(tmp_path, cases, manifest))


def test_visualizations_are_bound_to_result_facts_and_persistent_dashboard_recipes(suite):
    cases, _ = suite
    chart_types = set(get_args(ChartType))
    charts = dashboards = 0
    for case in cases:
        expected = case["expect"]
        facts = {fact["key"] for fact in expected.get("facts", [])}
        if chart := expected.get("chart"):
            charts += 1
            assert chart["result_backed"] is True
            assert chart["data_fact"] in facts
            assert set(chart["types_any"]) <= chart_types
            assert chart["x_meaning"].strip() and chart["y_unit"].strip()
            assert chart["min_points"] >= 1
        if dashboard := expected.get("dashboard"):
            dashboards += 1
            assert dashboard["persisted"] is True
            assert dashboard["refresh"] == "recompute_recipes"
            assert len({widget["id"] for widget in dashboard["widgets"]}) == len(
                dashboard["widgets"]
            )
            for widget in dashboard["widgets"]:
                assert widget["facts"] and set(widget["facts"]) <= facts
                assert set(widget["chart_types_any"]) <= chart_types
    assert charts >= 5
    assert dashboards >= 5


@pytest.mark.parametrize(
    "change",
    [
        "chart_unknown_fact",
        "chart_unknown_type",
        "chart_zero_points",
        "chart_unbacked",
        "chart_empty_axis",
        "chart_unknown_axis_kind",
        "dashboard_unpersisted",
        "dashboard_cached_refresh",
        "dashboard_unknown_fact",
        "dashboard_duplicate_widget",
        "dashboard_unknown_chart_type",
    ],
)
def test_visual_rubrics_cannot_pass_with_unverifiable_or_nonpersistent_results(
    tmp_path, suite, change
):
    cases, manifest = copy.deepcopy(suite)
    if change.startswith("chart_"):
        chart = next(case["expect"]["chart"] for case in cases if "chart" in case["expect"])
        field, value = {
            "chart_unknown_fact": ("data_fact", "dataset.row_count.unavailable"),
            "chart_unknown_type": ("types_any", ["three_dimensional_pie"]),
            "chart_zero_points": ("min_points", 0),
            "chart_unbacked": ("result_backed", False),
            "chart_empty_axis": ("x_meaning", "  "),
            "chart_unknown_axis_kind": ("x_kind", "imaginary"),
        }[change]
        chart[field] = value
    else:
        dashboard = next(
            case["expect"]["dashboard"] for case in cases if "dashboard" in case["expect"]
        )
        if change == "dashboard_unpersisted":
            dashboard["persisted"] = False
        elif change == "dashboard_cached_refresh":
            dashboard["refresh"] = "return_cached_image"
        elif change == "dashboard_unknown_fact":
            dashboard["widgets"][0]["facts"] = ["revenue.total"]
        elif change == "dashboard_duplicate_widget":
            dashboard["widgets"].append(copy.deepcopy(dashboard["widgets"][0]))
        else:
            dashboard["widgets"][0]["chart_types_any"] = ["three_dimensional_pie"]
    with pytest.raises(golden.GoldenError):
        golden.load_suite(*write_suite(tmp_path, cases, manifest))


def test_empty_input_stops_at_transport_and_rejection_never_runs_analysis(suite):
    cases, _ = suite
    empty = next(case for case in cases if case["expect"]["outcome"] == "input_error")
    assert not empty["question"].strip()
    assert empty["expect"]["input_policy"] == {
        "ui_submit": False,
        "api_status": 422,
        "model_called": False,
    }
    assert not empty["expect"]["tools_any"]
    assert not empty["expect"].get("facts")
    rejections = [case for case in cases if case["expect"]["outcome"] == "reject"]
    assert rejections
    for case in rejections:
        expected = case["expect"]
        assert expected["intent"] in {"invalid", "off_topic"}
        assert set(expected["tools_forbidden"]) == golden.TOOLS
        assert expected["tools_any"] == []
        assert "text" in expected["blocks_include"]
        assert not expected.get("facts")


@pytest.mark.parametrize(
    "change",
    [
        "empty_submitted",
        "empty_http_success",
        "empty_model_called",
        "reject_analysis_permitted",
        "reject_requests_tool",
        "reject_numeric_result",
        "reject_without_text",
    ],
)
def test_edge_cases_cannot_be_misreported_as_successful_agent_answers(tmp_path, suite, change):
    cases, manifest = copy.deepcopy(suite)
    if change.startswith("empty_"):
        expected = next(
            case["expect"] for case in cases if case["expect"]["outcome"] == "input_error"
        )
        field, value = {
            "empty_submitted": ("ui_submit", True),
            "empty_http_success": ("api_status", 200),
            "empty_model_called": ("model_called", True),
        }[change]
        expected["input_policy"][field] = value
    else:
        expected = next(case["expect"] for case in cases if case["expect"]["outcome"] == "reject")
        if change == "reject_analysis_permitted":
            expected["tools_forbidden"].remove("run_sql")
        elif change == "reject_requests_tool":
            expected["tools_any"] = ["run_sql"]
        elif change == "reject_without_text":
            expected["blocks_include"] = []
        else:
            expected["facts"] = copy.deepcopy(cases[0]["expect"]["facts"])
    with pytest.raises(golden.GoldenError):
        golden.load_suite(*write_suite(tmp_path, cases, manifest))


def write_rows(tmp_path, dialect, rows):
    path = tmp_path / "scenarios.csv"
    with path.open("w", encoding=dialect["encoding"], newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=dialect["columns"], delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return path


def attempt(run, start, **overrides):
    row = {
        "Код запуска": run,
        "Код цепочки запусков": run,
        "Задание": "Job",
        "Сервис": "Service",
        "Окружение": "production",
        "Способ запуска": "По расписанию",
        "Номер попытки": "1",
        "Начало (МСК)": start,
        "Окончание (МСК)": "",
        "Статус": "Успешно",
        "Записей к обработке": "",
        "Обработано записей": "",
        "Код ошибки": "",
        "Описание ошибки": "",
        "Узел выполнения": "worker-01",
    }
    row.update(overrides)
    return row


def scenario_facts(tmp_path, dialect, rows):
    path = write_rows(tmp_path, dialect, rows)
    return golden.compute_scenario_facts(path, dialect)


def test_grouping_counts_attempts_and_full_duplicates_without_collapsing_chains(tmp_path, dialect):
    first = attempt(
        "a",
        "2025-01-01 00:00:00",
        **{
            "Код цепочки запусков": "shared-chain",
            "Сервис": "Beta",
            "Статус": "Ошибка",
            "Окончание (МСК)": "2025-01-01 00:01:00",
            "Записей к обработке": "10",
            "Обработано записей": "5",
        },
    )
    second = attempt(
        "b",
        "2025-01-02 00:00:00",
        **{
            "Код цепочки запусков": "shared-chain",
            "Сервис": "Alpha",
            "Способ запуска": "Вручную",
            # Status defines an error, rather than the presence of an error code.
            "Код ошибки": "TIMEOUT",
            "Окончание (МСК)": "2025-01-02 00:02:00",
            "Записей к обработке": "20",
            "Обработано записей": "10",
        },
    )
    missing = attempt(
        "c",
        "2025-04-01 01:00:00",
        **{
            "Сервис": "Alpha",
            "Окружение": "staging",
            "Способ запуска": "Вручную",
            "Статус": "Отменено",
            "Записей к обработке": "30",
        },
    )
    zero = attempt(
        "d",
        "2025-01-02 00:00:00",
        **{
            "Код цепочки запусков": "shared-chain",
            "Сервис": "Beta",
            "Окончание (МСК)": "2025-01-02 00:03:00",
            "Записей к обработке": "0",
            "Обработано записей": "0",
        },
    )
    actual = scenario_facts(tmp_path, dialect, [first, second, missing, first.copy(), zero])
    assert actual["monthly.counts"] == [["2025-01", 4], ["2025-04", 1]]
    assert actual["quarterly.counts"] == [["2025-Q1", 4], ["2025-Q2", 1]]
    assert actual["quarterly.errors"] == [["2025-Q1", 2], ["2025-Q2", 0]]
    assert actual["env_status.counts"] == [
        ["production", "Ошибка", 2],
        ["production", "Успешно", 2],
        ["staging", "Отменено", 1],
    ]
    assert actual["env_trigger.counts"] == [
        ["production", "Вручную", 1],
        ["production", "По расписанию", 3],
        ["staging", "Вручную", 1],
    ]
    assert actual["service.top5"] == [["Beta", 3], ["Alpha", 2]]
    assert actual["duplicates.full_rows"] == 1
    assert actual["chains.repeated"] == 1
    assert actual["duration.min_seconds"] == 60
    assert actual["duration.max_seconds"] == 180
    assert actual["duration.mean_seconds"] == 105
    assert actual["correlation.planned_processed"] == pytest.approx(1)


def test_same_run_identifier_with_different_cells_is_not_a_full_duplicate(tmp_path, dialect):
    first = attempt("same-run", "2025-01-01 00:00:00")
    changed = {**first, "Номер попытки": "2"}
    actual = scenario_facts(tmp_path, dialect, [first, changed, changed.copy()])
    assert actual["duplicates.full_rows"] == 1
    assert actual["chains.repeated"] == 1


def test_top_five_breaks_frequency_ties_by_service_name(tmp_path, dialect):
    rows = [
        attempt(str(index), "2025-01-01 00:00:00", **{"Сервис": service})
        for index, service in enumerate(["Zeta", "Beta", "Gamma", "Delta", "Alpha", "Epsilon"])
    ]
    actual = scenario_facts(tmp_path, dialect, rows)
    assert actual["service.top5"] == [
        ["Alpha", 1],
        ["Beta", 1],
        ["Delta", 1],
        ["Epsilon", 1],
        ["Gamma", 1],
    ]


def daily_rows(counts, *, start=date(2025, 1, 1), errors=None):
    rows = []
    for day_index, count in enumerate(counts):
        day = start + timedelta(days=day_index)
        for index in range(count):
            status = "Ошибка" if errors and index < errors[day_index] else "Успешно"
            rows.append(attempt(f"{day}-{index}", f"{day} 00:01:00", **{"Статус": status}))
    return rows


def test_seasonal_daily_mean_uses_calendar_days_and_leap_day(tmp_path, dialect):
    rows = daily_rows([10], start=date(2024, 1, 31))
    rows += daily_rows([20], start=date(2024, 2, 1))
    rows += daily_rows([1], start=date(2024, 2, 29))
    actual = scenario_facts(tmp_path, dialect, rows)
    # January has one covered calendar day; February has 29 including 27 empty days.
    # Averaging only observed dates would incorrectly put February first (21 / 2 > 10).
    assert actual["month_of_year.peak_month"] == 1
    assert actual["month_of_year.peak_daily_mean"] == 10
    assert actual["weekday.counts"] == [[0, 0], [1, 0], [2, 10], [3, 21], [4, 0], [5, 0], [6, 0]]
    assert actual["weekday.peak_day"] == 3
    assert actual["weekday.peak_daily_mean"] == Decimal("4.2")


def test_weekday_rate_and_peak_ties_use_monday_zero_and_calendar_exposure(tmp_path, dialect):
    rows = daily_rows([3], start=date(2025, 1, 1))
    rows += daily_rows([2], start=date(2025, 1, 6))
    rows += daily_rows([1], start=date(2025, 1, 10))
    actual = scenario_facts(tmp_path, dialect, rows)
    # Monday: 2 / 1 calendar day; Wednesday: 3 / 2; Friday: 1 / 2.
    assert actual["weekday.peak_day"] == 0
    assert actual["weekday.peak_daily_mean"] == 2
    tied = scenario_facts(tmp_path, dialect, daily_rows([1] * 7, start=date(2025, 1, 6)))
    assert tied["weekday.peak_day"] == 0


def test_year_over_year_july_change_has_correct_years_and_percentage_scale(tmp_path, dialect):
    rows = daily_rows([2], start=date(2025, 7, 1))
    rows += daily_rows([3], start=date(2026, 7, 1))
    actual = scenario_facts(tmp_path, dialect, rows)
    assert actual["july.counts"] == [["2025-07", 2], ["2026-07", 3]]
    assert actual["july.change_percent"] == 50


def test_pairwise_correlation_excludes_missing_values_and_retains_zero(tmp_path, dialect):
    pairs = [(0, 3), (1, 2), (2, 1), (100, ""), ("", 100)]
    rows = [
        attempt(
            str(index),
            "2025-01-01 00:00:00",
            **{"Записей к обработке": str(planned), "Обработано записей": str(processed)},
        )
        for index, (planned, processed) in enumerate(pairs)
    ]
    actual = scenario_facts(tmp_path, dialect, rows)
    assert actual["correlation.planned_processed"] == pytest.approx(-1)


def test_zero_variance_and_short_history_are_undefined_rather_than_false_evidence(
    tmp_path, dialect
):
    rows = daily_rows([2, 2, 2], errors=[1, 1, 1])
    for row in rows:
        row["Записей к обработке"] = "10"
        row["Обработано записей"] = "5"
    actual = scenario_facts(tmp_path, dialect, rows)
    assert actual["correlation.planned_processed"] is None
    assert actual["correlation.daily_attempts_errors"] is None
    assert actual["lag.best_days"] is None
    assert actual["lag.best_r"] is None
    assert actual["lag.plus5_r"] is None
    assert actual["duration.mean_seconds"] is None
    assert actual["forecast.base7"] == []
    assert actual["forecast.holdout7"] == []
    assert actual["forecast.holdout_mae"] is None


def test_positive_lag_means_attempts_precede_errors_and_zero_days_are_filled(tmp_path, dialect):
    # Irregular counts; errors are a scaled copy five days later.
    counts = [10 * (1 + (day * day + 3 * day + 7) % 17) for day in range(35)]
    errors = [0] * 5 + [count // 10 for count in counts[:-5]]
    actual = scenario_facts(tmp_path, dialect, daily_rows(counts, errors=errors))
    assert actual["lag.plus5_r"] == pytest.approx(1)
    assert actual["lag.best_days"] == 5
    assert actual["lag.best_r"] == pytest.approx(1)
    # Jan 2 and Jan 4 are real calendar observations with zero rows, not skipped dates.
    sparse = scenario_facts(tmp_path, dialect, daily_rows([1, 0, 2, 0, 3], errors=[1, 0, 2, 0, 3]))
    assert sparse["correlation.daily_attempts_errors"] == pytest.approx(1)


def test_seasonal_naive_forecast_uses_last_seven_calendar_days_without_holdout_leakage(
    tmp_path, dialect
):
    # Two weeks have distinct profiles; interior zero dates must stay in the recipe.
    counts = [1, 0, 2, 0, 3, 0, 4, 2, 0, 4, 0, 6, 0, 8]
    actual = scenario_facts(tmp_path, dialect, daily_rows(counts))
    assert actual["forecast.base7"] == [
        ["2025-01-15", 2],
        ["2025-01-16", 0],
        ["2025-01-17", 4],
        ["2025-01-18", 0],
        ["2025-01-19", 6],
        ["2025-01-20", 0],
        ["2025-01-21", 8],
    ]
    assert actual["forecast.base7_total"] == 20
    assert actual["forecast.growth7"] == [
        ["2025-01-15", 2.2],
        ["2025-01-16", 0.0],
        ["2025-01-17", 4.4],
        ["2025-01-18", 0.0],
        ["2025-01-19", 6.6],
        ["2025-01-20", 0.0],
        ["2025-01-21", 8.8],
    ]
    assert actual["forecast.growth7_total"] == 22
    assert actual["forecast.holdout7"] == [
        ["2025-01-08", 1, 2],
        ["2025-01-09", 0, 0],
        ["2025-01-10", 2, 4],
        ["2025-01-11", 0, 0],
        ["2025-01-12", 3, 6],
        ["2025-01-13", 0, 0],
        ["2025-01-14", 4, 8],
    ]
    assert actual["forecast.holdout_mae"] == Decimal(10) / 7


def test_negative_duration_is_invalid_rather_than_an_anomaly_reference(tmp_path, dialect):
    rows = [
        attempt(
            "bad-clock",
            "2025-01-02 00:00:00",
            **{"Окончание (МСК)": "2025-01-01 23:59:59"},
        )
    ]
    with pytest.raises(golden.GoldenError, match="duration"):
        scenario_facts(tmp_path, dialect, rows)


def test_explicit_duration_anomaly_threshold_is_strictly_greater_than_one_hour(tmp_path, dialect):
    rows = [
        attempt("at-limit", "2025-01-01 00:00:00", **{"Окончание (МСК)": "2025-01-01 01:00:00"}),
        attempt("over-limit", "2025-01-01 00:00:00", **{"Окончание (МСК)": "2025-01-01 01:00:01"}),
        attempt("unknown", "2025-01-01 00:00:00"),
    ]
    actual = scenario_facts(tmp_path, dialect, rows)
    assert actual["duration.over_3600_count"] == 1
