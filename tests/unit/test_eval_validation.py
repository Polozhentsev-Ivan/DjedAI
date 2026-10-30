"""Failures observed in the real golden run, plus controls against false passes."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from djedai.contracts.agent import Block
from djedai.contracts.chart import ChartSpec, XAxis, YMeasure
from djedai.contracts.dashboard import (
    DashboardSnapshot,
    WidgetLayout,
    WidgetSnapshot,
    WidgetToolCall,
)
from djedai.contracts.tool_result import Fact, ToolError, ToolResult, ToolResultData

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from djedai.execution import TurnTimeout, current_budget  # noqa: E402
from scripts.eval_runner import (  # noqa: E402
    _case_skip_reason,
    check_fact,
    evaluate_case,
    process_case,
    run_eval,
)


def result(
    rows=(),
    *,
    columns=(),
    tool="run_sql",
    status="ok",
    facts=(),
    params=None,
    result_id="r_validation",
):
    return ToolResult(
        result_id=result_id,
        tool=tool,
        status=status,
        params=params or {},
        data=ToolResultData(columns=list(columns), rows=list(rows)),
        facts=list(facts),
        error=ToolError(code="test_error", message="Failed") if status == "error" else None,
    )


def case(facts=(), **expect):
    return {
        "id": "validation",
        "question": "Посчитай результат",
        "dataset": "ds_validation",
        "expect": {
            "outcome": "answer",
            "tools_any": ["run_sql"],
            "facts": list(facts),
            "answer_contains_value": {"from_result": True},
            **expect,
        },
    }


def state(tool_result, text):
    return {
        "results": [tool_result],
        "plan": [SimpleNamespace(tool=tool_result.tool)],
        "answer_blocks": [Block(type="text", markdown=text)],
    }


def july_facts(counts=None):
    return [
        {
            "key": "july.counts",
            "comparison": "exact",
            "value": counts if counts is not None else [["2025-07", 21377], ["2026-07", 21375]],
        },
        {
            "key": "july.change_percent",
            "comparison": "approx",
            "value": -0.009355849745053095,
            "abs_tolerance": 0.00005,
        },
    ]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Изменение: -0.01%", "passed"),
        ("Изменение: −0.01%", "passed"),
        ("Изменение: −0,01%", "passed"),
        ("Изменение: -0.00935585%", "passed"),
        ("Изменение: 0.01%", "failed"),
        ("Изменение: −2%", "failed"),
        ("Изменение: 2%", "failed"),
        ("Сравнение выполнено", "failed"),
    ],
)
@pytest.mark.parametrize("include_percentage", [False, True])
def test_july_percentage_uses_verified_counts_and_checks_answer(
    answer, expected, include_percentage
):
    actual = result(
        [["2025-07", 21377], ["2026-07", 21375]],
        facts=[Fact(text="SQL summary", kind="summary", values={"row_count": 2})],
    )
    graph_state = state(actual, answer)
    if include_percentage:
        graph_state["results"].append(
            result([[-0.009355849745053095]]).model_copy(update={"result_id": "r_percentage"})
        )
    status, issues, details = evaluate_case(case(july_facts()), graph_state)
    assert status == expected, issues
    assert details["fact_checks"][1]["derivation"]["sources"] == ["july.counts"]


@pytest.mark.parametrize("sign", ["-", "−"])
@pytest.mark.parametrize(
    ("source", "text", "expected"),
    [
        (500000, "{sign}500000 строк", "failed"),
        (-500000, "{sign}500000 строк", "passed"),
        (-500000, "{sign}500 тыс. строк", "passed"),
        (6.35, "Изменение: {sign}6.35%", "failed"),
        (-6.35, "Изменение: {sign}6.35%", "passed"),
    ],
)
def test_numeric_claims_preserve_the_sign(source, text, expected, sign):
    status, issues, _ = evaluate_case(case(), state(result([[source]]), text.format(sign=sign)))
    assert status == expected, issues


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["2025-07", 21375], ["2026-07", 21377]],
        [["2025-07", 21377], ["2026-07", 21376]],
        [["2025-07", 21377], ["2026-07", 21375], ["2027-07", 21375]],
    ],
)
def test_percentage_cannot_be_derived_from_wrong_or_missing_source(rows):
    facts = july_facts()
    assert not check_fact(facts[1], [result(rows)], reference_facts=facts)


def test_percentage_derivation_rejects_missing_reference_and_zero_denominator():
    facts = july_facts([["2025-07", 0], ["2026-07", 1]])
    actual = [result(facts[0]["value"])]
    assert not check_fact(facts[1], actual, reference_facts=facts)
    assert not check_fact(facts[1], actual)
    unrelated = {**facts[1], "key": "unrelated.change_percent"}
    assert not check_fact(unrelated, actual, reference_facts=facts)


@pytest.mark.parametrize(
    ("answer", "status"), [("Ошибок: 6.35%", "passed"), ("Ошибок: 2%", "failed")]
)
def test_production_percentage_has_explicit_verified_numerator_and_denominator(answer, status):
    facts = [
        {"key": "production.row_count", "comparison": "exact", "value": 407661},
        {"key": "production.error_count", "comparison": "exact", "value": 25873},
        {
            "key": "production.error_percent",
            "comparison": "approx",
            "value": 6.346694925440501,
            "abs_tolerance": 0.005,
        },
    ]
    actual = result([[407661, 25873]])
    outcome, issues, details = evaluate_case(case(facts), state(actual, answer))
    assert outcome == status, issues
    assert details["fact_checks"][2]["derivation"]["sources"] == [
        "production.error_count",
        "production.row_count",
    ]
    assert not check_fact(facts[2], [result([[407661]])], reference_facts=facts)


@pytest.mark.parametrize(
    "text",
    [
        "Таблица `08_fonovye_zadaniya`: 500000 строк",
        "2024 Q4: 500000 строк",
        "2024-Q4: 500000 строк",
    ],
)
def test_identifiers_and_calendar_labels_are_not_measurements(text):
    status, issues, _ = evaluate_case(case(), state(result([[500000]]), text))
    assert status == "passed", issues


@pytest.mark.parametrize(
    "text",
    [
        "Таблица `08_fonovye_zadaniya`: 500 строк",
        "`1500000` строк",
        "2024 Q4: 2024 строки",
        "8 таблиц и 500000 строк",
    ],
)
def test_formatting_never_hides_unbacked_measurements(text):
    status, _, _ = evaluate_case(case(), state(result([[500000]]), text))
    assert status == "failed"


@pytest.mark.parametrize("bucket", ["2024-10-01", "2024-10-01T00:00:00", "2024-09-30T21:00:00Z"])
def test_monthly_bucket_labels_are_compared_in_dataset_timezone(bucket):
    fact = {"key": "monthly.counts", "comparison": "exact", "value": [["2024-10", 123]]}
    assert check_fact(fact, [result([[bucket, 123]])])
    assert not check_fact(fact, [result([[bucket, 124]])])


def test_a_day_inside_a_month_is_not_a_monthly_bucket():
    fact = {"key": "monthly.counts", "comparison": "exact", "value": [["2024-10", 123]]}
    assert not check_fact(fact, [result([["2024-10-02", 123]])])


def test_quarterly_metrics_can_share_one_result_without_losing_pairing():
    counts = {
        "key": "quarterly.counts",
        "comparison": "exact",
        "value": [["2024-Q4", 100], ["2025-Q1", 90]],
    }
    errors = {
        "key": "quarterly.errors",
        "comparison": "exact",
        "value": [["2024-Q4", 7], ["2025-Q1", 8]],
    }
    actual = result([["2024 Q4", 100, 7], ["2025 Q1", 90, 8]])
    assert check_fact(counts, [actual])
    assert check_fact(errors, [actual])
    swapped = result([["2024 Q4", 90, 8], ["2025 Q1", 100, 7]])
    assert not check_fact(counts, [swapped])
    assert not check_fact(errors, [swapped])
    assert not check_fact(counts, [result(actual.data.rows + [["2025 Q2", 10, 1]])])


def test_top_three_can_come_from_a_complete_frequency_result():
    fact = {
        "key": "errors.top3",
        "comparison": "exact_unordered",
        "value": [["A", 8], ["B", 7], ["C", 6]],
    }
    actual = result([[None, 100], ["A", 8], ["B", 7], ["C", 6], ["D", 5]])
    assert check_fact(fact, [actual])
    assert not check_fact(fact, [result(actual.data.rows + [["E", 9]])])
    assert not check_fact(fact, [result([["A", 7], ["B", 8], ["C", 6]])])


def test_complete_collections_still_reject_extra_rows():
    fact = {"key": "status.counts", "comparison": "exact_unordered", "value": [["A", 8], ["B", 7]]}
    assert not check_fact(fact, [result([["A", 8], ["B", 7], ["C", 1]])])


def test_column_mapping_is_scoped_and_does_not_accept_unknown_columns():
    fact = {"key": "dataset.columns", "comparison": "exact_unordered", "value": ["Код", "Статус"]}
    aliases = {"Код": "kod", "Статус": "status"}
    actual = result(facts=[Fact(text="columns", values={"columns": ["status", "kod"]})])
    assert check_fact(fact, [actual], column_aliases=aliases)
    assert check_fact(fact, [result([["status"], ["kod"]])], column_aliases=aliases)
    assert not check_fact(fact, [result([["status", "kod"]])], column_aliases=aliases)
    assert not check_fact(
        fact,
        [result(facts=[Fact(text="columns", values={"columns": ["status", "other"]})])],
        column_aliases=aliases,
    )


def test_empty_result_can_explicitly_confirm_zero_but_does_not_invent_it():
    fact = {"key": "duplicates.full_rows", "comparison": "exact", "value": 0}
    empty = result(
        tool="find_duplicates",
        status="empty",
        facts=[Fact(text="No duplicates", kind="duplicate_volume", values={"extra_rows": 0})],
    )
    assert check_fact(fact, [empty])
    assert not check_fact(fact, [result(status="empty")])
    assert not check_fact(fact, [result(status="error", facts=empty.facts)])


def test_one_table_proves_no_intertable_relations():
    fact = {"key": "dataset.relation_count", "comparison": "exact", "value": 0}
    actual = result(
        tool="describe_dataset",
        facts=[
            Fact(
                text="One table",
                kind="dataset_summary",
                values={"table_count": 1, "tables": ["data"]},
            )
        ],
    )
    assert check_fact(fact, [actual])
    two_tables = result(
        tool="describe_dataset",
        facts=[
            Fact(
                text="Two tables",
                kind="dataset_summary",
                values={"table_count": 2, "tables": ["a", "b"]},
            )
        ],
    )
    assert not check_fact(fact, [two_tables])


def test_registered_planned_tool_is_a_valid_alternative():
    c = case(tools_planned=["time_aggregate"])
    status, issues, _ = evaluate_case(c, state(result([[123]], tool="time_aggregate"), "123"))
    assert status == "passed", issues


@pytest.mark.parametrize("requirement", ["seasonality", "segments"])
def test_registered_capabilities_are_not_silently_skipped(requirement):
    assert _case_skip_reason(case(requirements=[requirement])) is None


def test_one_available_tool_alternative_is_enough():
    assert _case_skip_reason(case(tools_any=["not_implemented", "run_sql"])) is None
    assert _case_skip_reason(case(tools_any=["not_implemented"])) is not None


def test_correct_rejection_intent_does_not_hide_forbidden_tool_use():
    c = case(outcome="reject", intent="invalid", tools_any=[], tools_forbidden=["run_sql"])
    s = state(result([[1]]), "Нельзя удалить таблицу")
    s["understanding"] = SimpleNamespace(intent="invalid")
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert any("forbidden" in issue.lower() for issue in issues)
    assert details["safety_checks"]["passed"] is False


def test_rejection_text_and_classification_are_reported_separately():
    c = case(outcome="reject", intent="invalid", tools_any=[], tools_forbidden=["run_sql"])
    s = {
        "understanding": SimpleNamespace(intent="off_topic"),
        "results": [],
        "plan": [],
        "answer_blocks": [Block(type="error", markdown="Запрос вне моих возможностей")],
    }
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"  # Keep the requested intent check strict.
    assert "off_topic" in issues[0]
    assert details["answer"] == "Запрос вне моих возможностей"
    assert details["safety_checks"]["passed"] is True
    assert details["intent"] == "off_topic"


def test_report_preserves_plan_results_and_blocks_for_rechecking():
    r = result([[123]], params={"sql": "SELECT 123 AS n"})
    s = state(r, "123")
    s["plan"] = [{"tool": "run_sql", "args": {"sql": "SELECT 123 AS n"}}]
    _, _, details = evaluate_case(case(must_explain=["Explain the origin"]), s)
    assert details["tool_results"][0]["params"] == r.params
    assert details["plan"] == s["plan"]
    assert details["answer_blocks"][0]["markdown"] == "123"
    assert "must_explain" in details["unchecked_rubrics"]


def test_context_report_preserves_prerequisite_details():
    dependency = case()
    dependency["id"] = "before"
    c = case()
    c["context"] = {"previous_cases": ["before"], "same_chat": True}
    graph = SimpleNamespace(invoke=lambda *a, **k: state(result([[123]]), "123"))
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation"),
        load_catalog=lambda _: None,
        dispatch=lambda *a: None,
    )
    report = process_case(c, {"before": dependency, c["id"]: c}, None, graph, dataset)
    assert report["context_results"][0]["id"] == "before"
    assert report["context_results"][0]["tool_results"][0]["data"]["rows"] == [[123]]


def test_graph_runs_with_a_turn_budget_and_diagnostic_trace():
    def invoke(input_state, config):
        assert current_budget() is config["configurable"]["budget"]
        assert 0 < current_budget().remaining <= 120
        return state(result([[123]]), "123")

    graph = SimpleNamespace(invoke=invoke)
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation"),
        load_catalog=lambda _: None,
        dispatch=lambda *a: None,
    )
    c = case()
    report = process_case(c, {c["id"]: c}, None, graph, dataset)
    assert report["status"] == "passed"
    assert report["diagnostics"][0]["event"] == "run_started"
    assert report["diagnostics"][-1]["event"] == "run_finished"
    assert current_budget() is None


def test_timeout_keeps_the_last_checkpoint_and_never_passes():
    partial = state(result([[123]], params={"sql": "SELECT 123"}), "123")

    def invoke(*args, **kwargs):
        raise TurnTimeout()

    graph = SimpleNamespace(invoke=invoke, get_state=lambda _: SimpleNamespace(values=partial))
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation"),
        load_catalog=lambda _: None,
        dispatch=lambda *a: None,
    )
    c = case()
    report = process_case(c, {c["id"]: c}, None, graph, dataset)
    assert report["status"] == "failed"
    assert report["error_type"] == "TurnTimeout"
    assert report["tool_results"][0]["params"]["sql"] == "SELECT 123"
    assert report["diagnostics"][-1]["error_type"] == "TurnTimeout"


@pytest.mark.parametrize("text", ["Q4 2024: 500000 строк", "4 квартал 2024: 500000 строк"])
def test_other_explicit_quarter_notations_are_not_measurements(text):
    assert evaluate_case(case(), state(result([[500000]]), text))[0] == "passed"


def test_projection_preserves_duplicate_rows_and_order():
    fact = {"key": "group.counts", "comparison": "exact", "value": [["A", 2], ["B", 3]]}
    assert check_fact(fact, [result([[99, "A", 2], [98, "B", 3]])])
    assert not check_fact(fact, [result([[98, "B", 3], [99, "A", 2]])])
    assert not check_fact(fact, [result([[99, "A", 2], [99, "A", 2]])])


def test_an_incidental_ratio_cannot_justify_a_wrong_count():
    actual = result([[500000], [1000]])
    assert evaluate_case(case(), state(actual, "500 строк"))[0] == "failed"
    assert evaluate_case(case(), state(actual, "Доля 0,002"))[0] == "passed"
    assert evaluate_case(case(), state(actual, "0,2 %"))[0] == "passed"
    # The same number is a valid percentage and an invalid count in this answer.
    assert evaluate_case(case(), state(actual, "0,2 % и 0,2 строки"))[0] == "failed"


@pytest.mark.parametrize(
    "options, expected",
    [([], "failed"), (["A"], "failed"), (["A", "A"], "failed"), (["A", "B"], "passed")],
)
def test_clarification_requires_the_requested_options(options, expected):
    c = case(
        outcome="clarify",
        intent="ambiguous",
        tools_any=[],
        clarification={"resolves": "population"},
    )
    s = {
        "understanding": SimpleNamespace(
            intent="ambiguous", clarification=SimpleNamespace(options=options)
        ),
        "results": [],
        "plan": [],
        "answer_blocks": [Block(type="clarification", markdown="Какая популяция?")],
    }
    assert evaluate_case(c, s)[0] == expected


def suite_files(tmp_path, cases):
    questions = tmp_path / "questions.yaml"
    manifest = tmp_path / "manifest.json"
    questions.write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    manifest.write_text(json.dumps({"dataset_id": "ds_validation"}), encoding="utf-8")
    return questions, manifest


def test_duplicate_case_ids_are_rejected_before_model_initialization(tmp_path, monkeypatch):
    questions, manifest = suite_files(tmp_path, [case(), case()])

    def forbidden_client():
        pytest.fail("No inference client should be initialized")

    monkeypatch.setattr("scripts.eval_runner.get_agent_client", forbidden_client)
    with pytest.raises(ValueError, match="unique"):
        run_eval(questions, tmp_path / "report.json", manifest_path=manifest)


def test_question_cannot_be_evaluated_against_a_different_dataset():
    dataset = SimpleNamespace(catalog=SimpleNamespace(dataset_id="other"))
    c = case()
    report = process_case(c, {c["id"]: c}, None, None, dataset)
    assert report["status"] == "blocked"
    assert "dataset" in report["reason"]


def test_report_is_saved_before_inference_and_atomically_completed(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    questions, manifest = suite_files(tmp_path, [case()])

    def invoke(*args, **kwargs):
        partial = json.loads(output.read_text("utf-8"))
        assert partial["status"] == "running"
        assert partial["summary"]["pending"] == 1
        assert partial["cases"][0]["status"] == "pending"
        return state(result([[123]]), "123")

    monkeypatch.setattr("scripts.eval_runner.get_agent_client", lambda: None)
    monkeypatch.setattr(
        "scripts.eval_runner.build_agent_graph", lambda: SimpleNamespace(invoke=invoke)
    )
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation"),
        load_catalog=lambda _: None,
        dispatch=lambda *a: None,
    )
    run_eval(questions, output, manifest_path=manifest, dataset=dataset)
    completed = json.loads(output.read_text("utf-8"))
    assert completed["status"] == "completed"
    assert completed["summary"]["passed"] == 1
    assert completed["summary"]["pending"] == 0
    assert not output.with_name("report.json.tmp").exists()


@pytest.mark.parametrize("markdown", [None, "", "   "])
def test_empty_text_block_is_a_failed_answer_with_preserved_trace(markdown):
    s = state(result([[123]]), "123")
    s["answer_blocks"] = [Block(type="text", markdown=markdown)]
    status, issues, details = evaluate_case(case(), s)
    assert status == "failed"
    assert "The answer contains no text block" in issues
    assert details["answer_blocks"][0]["markdown"] == markdown


# --- Tests for Chart, Dashboard, Forecast, Correlation, and Semantic Rubrics (#227) ---


def _make_chart_spec(
    chart_id="c1",
    chart_type="line",
    result_id="r_chart",
    x_field="date",
    x_kind="time",
    y_field="val",
    limit=20,
):
    return ChartSpec(
        chart_id=chart_id,
        type=chart_type,
        title="Chart",
        result_id=result_id,
        x=XAxis(field=x_field, kind=x_kind),
        y=[YMeasure(field=y_field, format="number")],
        limit=limit,
    )


def test_chart_missing_chart_block_fails():
    c = case(chart={"types_any": ["line"]})
    s = state(result([[1]], columns=["val"]), "1")
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert "Expected a chart block in the answer, but none was found" in issues


def test_chart_wrong_type_fails():
    c = case(chart={"types_any": ["line"]})
    res = result([["2026-01-01", 10]], columns=["date", "val"], result_id="r_chart")
    cs = _make_chart_spec(chart_type="bar", result_id="r_chart")
    s = state(res, "10")
    s["answer_blocks"].append(Block(type="chart", chart=cs.model_dump()))
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert any("Chart type 'bar' is not in expected types" in i for i in issues)


def test_chart_missing_result_id_fails():
    c = case(chart={"types_any": ["line"]})
    res = result([["2026-01-01", 10]], columns=["date", "val"], result_id="r_chart")
    cs_dict = _make_chart_spec(result_id="r_chart").model_dump()
    cs_dict["result_id"] = None
    s = state(res, "10")
    s["answer_blocks"].append(Block(type="chart", chart=cs_dict))
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert "Chart does not contain a result_id" in issues


def test_chart_result_not_reused_from_prerequisite_fails():
    c = case(chart={"types_any": ["line"]}, reuse_result_from="q_prereq")
    res_prereq = result([[1]], columns=["x"], result_id="r_prereq")
    res_new = result([["2026-01-01", 10]], columns=["date", "val"], result_id="r_chart")
    cs = _make_chart_spec(result_id="r_chart")
    s = state(res_new, "10")
    s["answer_blocks"].append(Block(type="chart", chart=cs.model_dump()))
    ctx = [{"id": "q_prereq", "tool_results": [res_prereq]}]
    status, issues, details = evaluate_case(c, s, context_results=ctx)
    assert status == "failed"
    assert any("was not reused from prerequisite 'q_prereq'" in i for i in issues)


def test_chart_backing_result_not_found_or_error_fails():
    c = case(chart={"types_any": ["line"]})
    cs = _make_chart_spec(result_id="r_missing")
    s = state(result([[1]], columns=["x"]), "1")
    s["answer_blocks"].append(Block(type="chart", chart=cs.model_dump()))
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert any("Chart result_id 'r_missing' was not found in tool results" in i for i in issues)

    # backing result with error status
    res_err = result(status="error", result_id="r_err")
    cs_err = _make_chart_spec(result_id="r_err")
    s_err = {
        "results": [res_err],
        "plan": [SimpleNamespace(tool=res_err.tool)],
        "answer_blocks": [
            Block(type="text", markdown="Текст"),
            Block(type="chart", chart=cs_err.model_dump()),
        ],
    }
    status_e, issues_e, _ = evaluate_case(c, s_err)
    assert status_e == "failed"


def test_chart_axis_mismatch_fails():
    c = case(chart={"types_any": ["line"], "x_kind": "time"})
    # X kind mismatch: expected time, provided category
    res = result([["cat_a", 10]], columns=["date", "val"], result_id="r_chart")
    cs_wrong_kind = _make_chart_spec(x_kind="category")
    s = state(res, "10")
    s["answer_blocks"].append(Block(type="chart", chart=cs_wrong_kind.model_dump()))
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("does not match expected 'time'" in i for i in issues)

    # X field missing from columns
    cs_missing_x = _make_chart_spec(x_field="unknown_col")
    s2 = state(res, "10")
    s2["answer_blocks"].append(Block(type="chart", chart=cs_missing_x.model_dump()))
    status2, issues2, _ = evaluate_case(c, s2)
    assert status2 == "failed"
    assert any(
        "Chart X axis field 'unknown_col' not in backing result columns" in i for i in issues2
    )

    # Y field missing from columns
    cs_missing_y = _make_chart_spec(y_field="unknown_y")
    s3 = state(res, "10")
    s3["answer_blocks"].append(Block(type="chart", chart=cs_missing_y.model_dump()))
    status3, issues3, _ = evaluate_case(c, s3)
    assert status3 == "failed"
    assert any(
        "Chart Y measure field 'unknown_y' not in backing result columns" in i for i in issues3
    )


def test_chart_points_truncation_or_insufficient_rows_fails():
    c = case(chart={"types_any": ["line"], "min_points": 5})
    # Limit truncating series below min_points
    res = result(
        [
            ["2026-01-01", 1],
            ["2026-01-02", 2],
            ["2026-01-03", 3],
            ["2026-01-04", 4],
            ["2026-01-05", 5],
        ],
        columns=["date", "val"],
        result_id="r_chart",
    )
    cs_limit_3 = _make_chart_spec(limit=3)
    s1 = state(res, "5")
    s1["answer_blocks"].append(Block(type="chart", chart=cs_limit_3.model_dump()))
    status1, issues1, _ = evaluate_case(c, s1)
    assert status1 == "failed"
    assert any("Chart limit 3 truncates series below required 5 points" in i for i in issues1)

    # Backing rows count fewer than min_points
    res_few = result(
        [["2026-01-01", 1], ["2026-01-02", 2]], columns=["date", "val"], result_id="r_chart"
    )
    cs_limit_20 = _make_chart_spec(limit=20)
    s2 = state(res_few, "2")
    s2["answer_blocks"].append(Block(type="chart", chart=cs_limit_20.model_dump()))
    status2, issues2, _ = evaluate_case(c, s2)
    assert status2 == "failed"
    assert any("fewer than required 5 points" in i for i in issues2)


def test_chart_data_fact_mismatch_fails():
    fact = {"key": "chart.peak", "comparison": "exact", "value": 999}
    c = case(facts=[fact], chart={"types_any": ["line"], "data_fact": "chart.peak"})
    res = result([["2026-01-01", 10]], columns=["date", "val"], result_id="r_chart")
    cs = _make_chart_spec(result_id="r_chart")
    s = state(res, "10")
    s["answer_blocks"].append(Block(type="chart", chart=cs.model_dump()))
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any(
        "Chart backing result does not confirm expected fact 'chart.peak'" in i for i in issues
    )


def test_chart_positive_passes():
    fact = {
        "key": "chart.series",
        "comparison": "exact",
        "value": [["2026-01-01", 10], ["2026-01-02", 20]],
    }
    c = case(
        facts=[fact],
        chart={
            "types_any": ["line"],
            "x_kind": "time",
            "min_points": 2,
            "data_fact": "chart.series",
        },
    )
    res = result(
        [["2026-01-01", 10], ["2026-01-02", 20]],
        columns=["date", "val"],
        result_id="r_chart",
    )
    cs = _make_chart_spec(result_id="r_chart", limit=10)
    s = state(res, "График построен: 10 и 20")
    s["answer_blocks"].append(Block(type="chart", chart=cs.model_dump()))
    status, issues, details = evaluate_case(c, s)
    assert status == "passed", issues
    assert details["automated_status"] == "passed"
    assert details["semantic_status"] == "passed"
    assert details["chart_checks"]["passed"] is True


def _make_dashboard_widget(widget_id, chart_type="line", result_id="r1", tool_params=None):
    chart = ChartSpec(
        chart_id=f"c_{widget_id}",
        type=chart_type,
        title=f"Chart {widget_id}",
        result_id=result_id,
        x=XAxis(field="date", kind="category"),
        y=[YMeasure(field="cnt", format="number")],
    )
    return WidgetSnapshot(
        widget_id=widget_id,
        chart=chart,
        result_id=result_id,
        status="ok",
        layout=WidgetLayout(x=0, y=0, w=4, h=2),
        tool_call=WidgetToolCall(tool="run_sql", params=tool_params or {"query": "SELECT 1"}),
    )


def test_dashboard_missing_link_or_snapshot_fails():
    c = case(dashboard={})
    s = state(result([[1]], columns=["val"]), "1")
    # Missing dashboard_link
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert "Dashboard outcome produced no dashboard_link block" in issues

    # Missing dashboard_id in dashboard_link
    s["answer_blocks"].append(Block(type="dashboard_link", dashboard_id=None))
    status2, issues2, _ = evaluate_case(c, s)
    assert status2 == "failed"
    assert "dashboard_link block has no dashboard_id" in issues2

    # Missing snapshot
    s["answer_blocks"][-1] = Block(type="dashboard_link", dashboard_id="d_empty")
    status3, issues3, _ = evaluate_case(c, s)
    assert status3 == "failed"
    assert "Dashboard snapshot is missing" in issues3


def test_dashboard_widget_count_bounds_fails():
    c = case(dashboard={})
    # Fewer than 3 widgets
    snap2 = DashboardSnapshot(
        dashboard_id="dash_2",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[
            _make_dashboard_widget("w1", result_id="r1"),
            _make_dashboard_widget("w2", result_id="r2"),
        ],
    )
    res = result([[1]], columns=["cnt"], result_id="r1")
    s = {
        "results": [res],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд сохранён: 1"),
            Block(type="dashboard_link", dashboard_id="dash_2"),
        ],
        "dashboard_snapshot": snap2,
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("Dashboard has 2 widgets; expected between 3 and 8" in i for i in issues)

    # More than 8 widgets
    widgets_9 = [_make_dashboard_widget(f"w_{i}", result_id=f"r_{i}") for i in range(9)]
    snap9 = DashboardSnapshot(
        dashboard_id="dash_9",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=widgets_9,
    )
    s["dashboard_snapshot"] = snap9
    s["answer_blocks"][-1] = Block(type="dashboard_link", dashboard_id="dash_9")
    status9, issues9, _ = evaluate_case(c, s)
    assert status9 == "failed"
    assert any("Dashboard has 9 widgets; expected between 3 and 8" in i for i in issues9)


def test_dashboard_recipe_temporary_source_result_id_fails():
    bad_recipe_widget = {
        "widget_id": "w1",
        "status": "ok",
        "result_id": "r1",
        "chart": {"type": "line", "result_id": "r1"},
        "recipe": {
            "steps": [{"step_id": "s1", "call": {"params": {"source_result_id": "tmp_r0"}}}]
        },
    }
    snap = {"widgets": [bad_recipe_widget, bad_recipe_widget, bad_recipe_widget]}
    s = {
        "results": [result([[1]], columns=["val"], result_id="r1")],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд: 1"),
            Block(type="dashboard_link", dashboard_id="d1"),
        ],
        "dashboard_snapshot": snap,
    }
    c = case(dashboard={})
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("contains temporary 'source_result_id'" in i for i in issues)


def test_dashboard_foreign_owner_access_fails():
    snap = DashboardSnapshot(
        dashboard_id="d_sec",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[_make_dashboard_widget(f"w_{i}", result_id=f"r_{i}") for i in range(3)],
    )
    res = result([[1]], columns=["val"], result_id="r_0")
    s = {
        "results": [res],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд: 1"),
            Block(type="dashboard_link", dashboard_id="d_sec"),
        ],
        "dashboard_snapshot": snap,
    }
    c = case(dashboard={})

    class LeakyDataset:
        def get_dashboard(self, user_id, dash_id):
            return snap  # foreign_owner receives the dashboard

    status, issues, _ = evaluate_case(c, s, dataset=LeakyDataset())
    assert status == "failed"
    assert any("foreign user was able to access dashboard" in i for i in issues)


def test_dashboard_refresh_same_result_id_fails():
    snap = DashboardSnapshot(
        dashboard_id="d_refresh",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[_make_dashboard_widget(f"w_{i}", result_id=f"r_{i}") for i in range(3)],
    )
    res = result([[1]], columns=["val"], result_id="r_0")
    s = {
        "results": [res],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд: 1"),
            Block(type="dashboard_link", dashboard_id="d_refresh"),
        ],
        "dashboard_snapshot": snap,
    }
    c = case(dashboard={})

    class RefreshCollisionDataset:
        def get_dashboard(self, user_id, dash_id):
            if user_id == "foreign_owner":
                return None
            return snap

        def refresh_dashboard(self, dash_id, owner_id):
            return snap  # returns same widgets with same result_id!

    status, issues, _ = evaluate_case(c, s, dataset=RefreshCollisionDataset())
    assert status == "failed"
    assert any("Dashboard refresh failed to generate new result_ids" in i for i in issues)


def test_dashboard_positive_passes():
    snap = DashboardSnapshot(
        dashboard_id="dash_good",
        dataset_id="ds_validation",
        title="Valid Dashboard",
        created_at=datetime.now(UTC),
        widgets=[_make_dashboard_widget(f"w_{i}", result_id=f"r_{i}") for i in range(3)],
    )
    res0 = result([[1]], columns=["cnt"], result_id="r_0")
    res1 = result([[2]], columns=["cnt"], result_id="r_1")
    res2 = result([[3]], columns=["cnt"], result_id="r_2")
    s = {
        "results": [res0, res1, res2],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд готов: 1 2 3"),
            Block(type="dashboard_link", dashboard_id="dash_good"),
        ],
        "dashboard_snapshot": snap,
    }
    c = case(dashboard={"widgets": [{"id": "w_0", "chart_types_any": ["line"]}]})

    class ValidDataset:
        def get_dashboard(self, user_id, dash_id):
            if user_id == "foreign_owner":
                return None
            return snap

        def refresh_dashboard(self, dash_id, owner_id):
            return DashboardSnapshot(
                dashboard_id=dash_id,
                dataset_id="ds_validation",
                title="Refreshed",
                created_at=datetime.now(UTC),
                widgets=[
                    _make_dashboard_widget(f"w_{i}", result_id=f"r_fresh_{i}") for i in range(3)
                ],
            )

    status, issues, details = evaluate_case(c, s, dataset=ValidDataset())
    assert status == "passed", issues
    assert details["automated_status"] == "passed"
    assert details["semantic_status"] == "passed"
    assert details["dashboard_checks"]["passed"] is True


def test_forecast_holdout_leak_detected_fails():
    c = case()
    c["id"] = "s10_holdout_mae_01"
    res_leaked = result(
        [
            ["2026-08-01", 100, 100],
            ["2026-08-02", 150, 150],
            ["2026-08-03", 200, 200],
            ["2026-08-04", 180, 180],
            ["2026-08-05", 220, 220],
            ["2026-08-06", 170, 170],
            ["2026-08-07", 190, 190],
        ],
        columns=["date", "forecast", "actual"],
        result_id="r_forecast",
    )
    s_leaked = state(res_leaked, "Прогноз MAE: 0.0")
    status, issues, _ = evaluate_case(c, s_leaked)
    assert status == "failed"
    assert any("Holdout leak detected" in i for i in issues)


def test_forecast_valid_mae_passes():
    c = case()
    c["id"] = "s10_holdout_mae_01"
    res_valid = result(
        [["2026-08-01", 105, 100], ["2026-08-02", 145, 150]],
        columns=["date", "forecast", "actual"],
        result_id="r_forecast",
    )
    res_mae = result([[5.0]], columns=["mae"], result_id="r_mae")
    s_valid = {
        "results": [res_valid, res_mae],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block(type="text", markdown="Прогноз MAE: 5.0")],
    }
    status, issues, details = evaluate_case(c, s_valid)
    assert status == "passed", issues
    assert details["automated_status"] == "passed"
    assert details["semantic_status"] == "passed"


@pytest.mark.parametrize(
    ("claim", "bad_text", "good_text"),
    [
        (
            "MAE заменена знаковой ошибкой",
            "Результат прогноза MAE: 0",
            "Результат прогноза MAE: 12.5",
        ),
        (
            "500000 строк — это 500000 разных заданий",
            "В датасете 500 000 уникальных заданий",
            "В датасете 500 000 строк попыток запуска",
        ),
        (
            "DROP выполнен",
            "Таблица удалена успешно",
            "Команда DROP отклонена",
        ),
    ],
)
def test_semantic_must_not_claim_violations_fail(claim, bad_text, good_text):
    c = case(must_not_claim=[claim])
    res = result([[12.5], [500000]], columns=["val"])
    # Negative control: triggers violation
    s_bad = state(res, bad_text)
    status_bad, issues_bad, details_bad = evaluate_case(c, s_bad)
    assert status_bad == "failed"
    assert details_bad["semantic_status"] == "failed"
    assert any("violated negative rubric" in i for i in issues_bad)

    # Positive control: valid claim passes
    s_good = state(res, good_text)
    status_good, issues_good, details_good = evaluate_case(c, s_good)
    assert status_good == "passed", issues_good
    assert details_good["semantic_status"] == "passed"


def test_semantic_must_explain_empty_fails():
    c = case(must_explain=["Объяснение методологии"])
    s = state(result([[1]], columns=["val"]), "   ")
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert details["semantic_status"] == "failed"
    assert any("Response failed explanation rubric" in i for i in issues)


def test_clarification_semantic_rubrics():
    c = case(
        outcome="clarify",
        intent="ambiguous",
        tools_any=[],
        clarification={
            "resolves": "дата начала позже окончания",
            "options": ["Поменять даты", "Отменить"],
        },
    )
    # Reason not explained
    s_no_reason = {
        "understanding": SimpleNamespace(
            intent="ambiguous", clarification=SimpleNamespace(options=["Поменять даты", "Отменить"])
        ),
        "results": [],
        "plan": [],
        "answer_blocks": [Block(type="clarification", markdown="Уточните ваш запрос")],
    }
    status1, issues1, details1 = evaluate_case(c, s_no_reason)
    assert status1 == "failed"
    assert details1["automated_status"] == "passed"
    assert details1["semantic_status"] == "failed"
    assert any("inverted date range" in i for i in issues1)

    # Valid clarification explaining inverted dates and having distinct options
    s_valid = {
        "understanding": SimpleNamespace(
            intent="ambiguous", clarification=SimpleNamespace(options=["Поменять даты", "Отменить"])
        ),
        "results": [],
        "plan": [],
        "answer_blocks": [
            Block(
                type="clarification",
                markdown="Дата начала позже даты окончания периода. Поменять местами?",
            )
        ],
    }
    status2, issues2, details2 = evaluate_case(c, s_valid)
    assert status2 == "passed", issues2
    assert details2["automated_status"] == "passed"
    assert details2["semantic_status"] == "passed"


def test_report_separates_automated_and_semantic_status():
    c = case(must_not_claim=["DROP выполнен"])
    res = result([[100]], columns=["cnt"])
    # Automated check passes (valid number 100, valid tool, etc.), but semantic rubric fails
    s = state(res, "Найдено 100 строк. DROP выполнен успешно.")
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert details["automated_status"] == "passed"
    assert details["semantic_status"] == "failed"
    assert "DROP выполнен" in str(details["semantic_issues"])


# --- Additional Tests for Thorough Review Findings ---


def test_semantic_unknown_must_not_claim_fails_closed():
    # P0: Unknown negative rubric must fail closed, not silently pass
    c = case(must_not_claim=["Неизвестная рубрика из будущей версии YAML"])
    s = state(result([[1]], columns=["cnt"]), "Текст ответа")
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert details["semantic_status"] == "failed"
    assert any("has no automated evaluator" in i for i in issues)
    rubric_entry = details["semantic_rubrics"]["must_not_claim"][0]
    assert rubric_entry["passed"] is False


@pytest.mark.parametrize(
    "bad_text",
    [
        "   ",
        "Анализ выполнен успешно",
        "Результат получен: 42",
        "Готово",
    ],
)
def test_semantic_must_explain_superficial_text_fails(bad_text):
    # P0: Superficial text cannot satisfy must_explain
    c = case(
        must_explain=["+10% — условие пользователя, не причинный эффект и не найденный тренд."]
    )
    res = result([[42]], columns=["val"])
    s = state(res, bad_text)
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert details["semantic_status"] == "failed"
    assert any("Response failed explanation rubric" in i for i in issues)


def test_semantic_must_explain_substantive_passes():
    c = case(
        must_explain=["+10% — условие пользователя, не причинный эффект и не найденный тренд."]
    )
    res = result([[42], [10]], columns=["val"])
    s = state(
        res,
        "Расчёт проведён для прогнозного сценария +10%: "
        "это условие пользователя, базис взят из предыдущего периода.",
    )
    status, issues, details = evaluate_case(c, s)
    assert status == "passed", issues
    assert details["semantic_status"] == "passed"


def test_clarification_superficial_dates_fails():
    # P1: "Уточните даты" without explaining the ordering conflict fails
    c = case(
        outcome="clarify",
        intent="ambiguous",
        tools_any=[],
        clarification={
            "resolves": "дата начала позже окончания",
            "options": ["Поменять даты", "Отменить"],
        },
    )
    s_superficial = {
        "understanding": SimpleNamespace(
            intent="ambiguous",
            clarification=SimpleNamespace(options=["Поменять даты", "Отменить"]),
        ),
        "results": [],
        "plan": [],
        "answer_blocks": [
            Block(
                type="clarification",
                markdown="Пожалуйста, уточните даты для отчёта.",
            )
        ],
    }
    status, issues, details = evaluate_case(c, s_superficial)
    assert status == "failed"
    assert details["semantic_status"] == "failed"
    assert any("date ordering conflict" in i for i in issues)


def test_chart_multiple_charts_validates_every_chart():
    # P1: Every chart in answer must be valid, not just the first
    c = case(chart={"types_any": ["line"]})
    res = result([["2026-01-01", 10]], columns=["date", "val"], result_id="r_chart")
    cs_valid = _make_chart_spec(chart_id="c1", chart_type="line", result_id="r_chart")
    cs_invalid = _make_chart_spec(chart_id="c2", chart_type="bar", result_id="r_chart")
    s = state(res, "10")
    s["answer_blocks"] = [
        Block(type="text", markdown="Два графика: 10"),
        Block(type="chart", chart=cs_valid.model_dump()),
        Block(type="chart", chart=cs_invalid.model_dump()),
    ]
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("Chart [1] type 'bar' is not in expected types" in i for i in issues)


def test_chart_missing_column_metadata_fails():
    # P1: Backing result with empty columns cannot verify axes
    c = case(chart={"types_any": ["line"], "x_kind": "time"})
    res = result([["2026-01-01", 10]], columns=[], result_id="r_chart")
    cs = _make_chart_spec(result_id="r_chart")
    s = state(res, "10")
    s["answer_blocks"].append(Block(type="chart", chart=cs.model_dump()))
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("has no column metadata to verify axes" in i for i in issues)


def test_dashboard_refresh_validates_widget_count_and_ids():
    # P1: Refresh returning fewer widgets or missing result_id fails
    snap = DashboardSnapshot(
        dashboard_id="dash_cnt",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[_make_dashboard_widget(f"w_{i}", result_id=f"r_{i}") for i in range(3)],
    )
    s = {
        "results": [result([[1]], columns=["val"], result_id="r_0")],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд: 1"),
            Block(type="dashboard_link", dashboard_id="dash_cnt"),
        ],
        "dashboard_snapshot": snap,
    }
    c = case(dashboard={})

    # Fewer widgets on refresh
    class DroppedWidgetDataset:
        def get_dashboard(self, user_id, dash_id):
            return snap if user_id != "foreign_owner" else None

        def refresh_dashboard(self, dash_id, owner_id):
            return DashboardSnapshot(
                dashboard_id=dash_id,
                dataset_id="ds_validation",
                title="Refreshed",
                created_at=datetime.now(UTC),
                widgets=[_make_dashboard_widget("w_0", result_id="r_fresh_0")],
            )

    status, issues, _ = evaluate_case(c, s, dataset=DroppedWidgetDataset())
    assert status == "failed"
    assert any("returned 1 widgets; expected 3" in i for i in issues)


def test_dashboard_foreign_owner_unexpected_error_fails():
    # P1: Unexpected database crash during foreign owner check fails
    snap = DashboardSnapshot(
        dashboard_id="dash_err",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[_make_dashboard_widget(f"w_{i}", result_id=f"r_{i}") for i in range(3)],
    )
    s = {
        "results": [result([[1]], columns=["val"], result_id="r_0")],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд: 1"),
            Block(type="dashboard_link", dashboard_id="dash_err"),
        ],
        "dashboard_snapshot": snap,
    }
    c = case(dashboard={})

    class CrashingDataset:
        def get_dashboard(self, user_id, dash_id):
            if user_id == "foreign_owner":
                raise RuntimeError("Postgres connection dropped")
            return snap

        def refresh_dashboard(self, dash_id, owner_id):
            return snap

    status, issues, _ = evaluate_case(c, s, dataset=CrashingDataset())
    assert status == "failed"
    assert any("unexpected error: Postgres connection dropped" in i for i in issues)


def test_forecast_partial_and_multitable_holdout_leak_fails():
    c = case()
    c["id"] = "s10_holdout_mae_01"

    # Single accidental match in a multi-day forecast must NOT be flagged as a leak
    res_accidental_match = result(
        [
            ["2026-08-01", 100, 100],  # row 0 matches accidentally
            ["2026-08-02", 140, 150],
            ["2026-08-03", 210, 205],
            ["2026-08-04", 180, 190],
            ["2026-08-05", 220, 230],
            ["2026-08-06", 170, 160],
            ["2026-08-07", 195, 200],
        ],
        columns=["date", "forecast", "actual"],
        result_id="r_accidental",
    )
    s_acc = state(res_accidental_match, "Прогноз")
    _, issues_acc, _ = evaluate_case(c, s_acc)
    assert not any("Holdout leak detected" in i for i in issues_acc)

    # Systematic leak: copying actual holdout values across the horizon must be caught
    res_systematic_leak = result(
        [
            ["2026-08-01", 100, 100],
            ["2026-08-02", 150, 150],
            ["2026-08-03", 205, 205],
            ["2026-08-04", 190, 190],
        ],
        columns=["date", "forecast", "actual"],
        result_id="r_systematic",
    )
    s_sys = state(res_systematic_leak, "Прогноз")
    status_s, issues_s, _ = evaluate_case(c, s_sys)
    assert status_s == "failed"
    assert any("Holdout leak detected" in i for i in issues_s)

    # Multi-table leak: leak in second tool result table must be caught
    res_clean = result(
        [["2026-08-01", 105, 100]],
        columns=["date", "forecast", "actual"],
        result_id="r_clean",
    )
    s_multi = {
        "results": [res_clean, res_systematic_leak],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block(type="text", markdown="Прогноз")],
    }
    status_m, issues_m, _ = evaluate_case(c, s_multi)
    assert status_m == "failed"
    assert any("Holdout leak detected" in i for i in issues_m)


def test_forecast_and_correlation_population_and_bounds_validation():
    # P0: Population checks and correlation bounds [-1, 1]
    c_pairs = case(
        population="complete_measure_pairs",
        facts=[{"key": "correlation.planned_processed", "comparison": "exact", "value": 0.98}],
    )
    # Missing 500k confirmation
    res_small = result([[10]], columns=["cnt"])
    s_small = state(res_small, "r = 0.98")
    status, issues, _ = evaluate_case(c_pairs, s_small)
    assert status == "failed"
    assert any("500000 complete pairs not confirmed" in i for i in issues)

    # Invalid correlation r outside [-1, 1]
    c_invalid_r = case(facts=[{"key": "correlation.invalid", "comparison": "exact", "value": 1.5}])
    status_r, issues_r, _ = evaluate_case(c_invalid_r, s_small)
    assert status_r == "failed"
    assert any("Invalid correlation coefficient 1.5" in i for i in issues_r)

    # Invalid forecast dates
    c_invalid_date = case(
        facts=[{"key": "forecast.base7", "comparison": "exact", "value": [["2026-99-99", 100]]}]
    )
    status_d, issues_d, _ = evaluate_case(c_invalid_date, s_small)
    assert status_d == "failed"
    assert any("does not match ISO YYYY-MM-DD format" in i for i in issues_d)


@pytest.mark.parametrize(
    ("pop", "row_count", "scalar_val", "should_pass"),
    [
        ("complete_measure_pairs", 1, 500000, True),
        ("calendar_attempt_rows", 730, None, True),
        ("calendar_attempt_rows", 100, None, False),
        ("forecast_last7_daily_counts", 7, None, True),
        ("forecast_last7_daily_counts", 5, None, False),
        ("forecast_last14_daily_counts", 14, None, True),
        ("forecast_last14_daily_counts", 7, None, False),
        ("forecast_last14_daily_counts", 3, None, False),
    ],
)
def test_population_verification_branches(pop, row_count, scalar_val, should_pass):
    rows = [[f"2026-08-{i:02d}", i] for i in range(1, row_count + 1)]
    if scalar_val is not None:
        rows = [[scalar_val]]
    res = result(rows, columns=["date", "val"])
    c = case(population=pop)
    s = state(res, "Текст ответа")
    status, issues, _ = evaluate_case(c, s)
    if should_pass:
        assert not any("Population check failed" in i for i in issues)
    else:
        assert any("Population check failed" in i for i in issues)


@pytest.mark.parametrize(
    ("claim", "bad_text"),
    [
        (
            "Корреляция доказывает, что изменение плана вызовет заданное изменение факта.",
            "Корреляция доказывает, что рост вызовет изменение факта",
        ),
        (
            "10% заменены 10 попытками или процентными пунктами доли.",
            "К значению добавлено 10 попыток",
        ),
        (
            "Выдуман доверительный интервал без модели и проверки.",
            "Рассчитан доверительный интервал [10, 20]",
        ),
        (
            "Прогноз объявлен наблюдаемым фактом или гарантией.",
            "Этот прогноз является наблюдаемым точным фактом",
        ),
        (
            "В файле есть поля выручки, покупателей или заказов.",
            "Найдена выручка по заказам",
        ),
        (
            "r рассчитан по сервисным средним вместо попыток.",
            "r вычислен по сервисным средним значениям",
        ),
        (
            "r рассчитан по долям ошибок вместо числа ошибок.",
            "r вычислен по долям ошибок сервисов",
        ),
        (
            "Вместо обновления создана несвязанная новая панель "
            "или возвращены старые временные result_id.",
            "Создана новая панель в системе",
        ),
        (
            "Ссылка подменена эталоном или результатом другого чата.",
            "Этот эталон подменен в системе",
        ),
    ],
)
def test_must_not_claim_all_branch_evaluations(claim, bad_text):
    c = case(must_not_claim=[claim])
    res = result([[10]], columns=["val"])
    s = state(res, bad_text)
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert details["semantic_status"] == "failed"
    assert any("violated negative rubric" in i for i in issues)


@pytest.mark.parametrize(
    ("explain", "good_text", "bad_text"),
    [
        (
            "Holdout — 24–30 сентября 2026, baseline — 17–23 сентября; "
            "holdout не участвует в прогнозах.",
            "Период holdout 24–30 сентября отделен от baseline.",
            "Прогноз успешно построен.",
        ),
        (
            "MAE = сумма абсолютных ошибок / 7; знаки ошибок не сокращаются.",
            "MAE вычислен как сумма абсолютных ошибок деленная на 7.",
            "Ошибка оценена.",
        ),
        (
            "Seasonal-naive с периодом 7 повторяет семь отдельных counts, не среднее недели.",
            "Модель seasonal-naive повторяет 7 дней прошлой недели.",
            "Прогноз завершен.",
        ),
        (
            "Использована непрерывная сетка из 730 дней; каждая пара — дневные counts.",
            "Построена непрерывная сетка из 730 дней наблюдений.",
            "Данные собраны.",
        ),
        (
            "Одна пара — одна попытка; использованы все 500000 полных пар без группировки.",
            "В выборку вошли все 500 000 полных пар без группировки.",
            "Пары рассчитаны.",
        ),
        (
            "Сильная линейная связь не доказывает причинный эффект или независимость наблюдений.",
            "Линейная связь не доказывает причинный эффект между метриками.",
            "Связь сильная.",
        ),
        (
            "Ошибки являются частью общего count; "
            "положительная связь не доказывает влияние нагрузки.",
            "Ошибки являются частью общего объема count попыток.",
            "Ошибок много.",
        ),
        (
            "k лаг, где attempts[t] и errors[t+k]; сдвиг во времени.",
            "Рассчитан временной сдвиг лаг между рядами.",
            "Ряды сопоставлены.",
        ),
        (
            "Выручку нельзя получить из имеющихся счётчиков записей.",
            "Выручка отсутствует в имеющихся данных.",
            "Данные получены.",
        ),
        (
            "Длительность — разность окончания и начала одной попытки.",
            "Длительность вычислена как разность окончания и начала.",
            "Время вычислено.",
        ),
    ],
)
def test_must_explain_all_branch_evaluations(explain, good_text, bad_text):
    c = case(must_explain=[explain])
    res = result([[7], [24], [30], [500000], [730]], columns=["val"])
    # Good text passes
    s_good = state(res, good_text)
    status_g, issues_g, details_g = evaluate_case(c, s_good)
    assert status_g == "passed", issues_g
    assert details_g["semantic_status"] == "passed"

    # Bad text fails
    s_bad = state(res, bad_text)
    status_b, issues_b, details_b = evaluate_case(c, s_bad)
    assert status_b == "failed"
    assert details_b["semantic_status"] == "failed"
    assert any("Response failed explanation rubric" in i for i in issues_b)


def test_semantic_unknown_must_explain_fails_closed():
    # P0: Unknown must_explain rubric must fail closed
    c = case(must_explain=["Неизвестная рубрика о погоде на Луне"])
    s = state(
        result([[1]], columns=["val"]), "Достаточно длинный текст ответа более чем сорок символов."
    )
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert details["semantic_status"] == "failed"
    assert any("has no automated evaluator" in i for i in issues)


@pytest.mark.parametrize(
    ("explain", "contradictory_text"),
    [
        (
            "MAE = сумма абсолютных ошибок / 7; знаки ошибок не сокращаются.",
            "Знаки ошибок сокращаются при вычислении MAE и ошибки сглаживаются.",
        ),
        (
            "MAE = сумма абсолютных ошибок / 7; знаки ошибок не сокращаются.",
            "Ошибка оценена, но MAE не рассчитывалась в анализе.",
        ),
        (
            "Сильная линейная связь не доказывает причинный эффект или независимость наблюдений.",
            "Корреляция доказывает причинный эффект между нагрузкой и ошибками.",
        ),
        (
            (
                "Holdout — 24–30 сентября 2026, baseline — 17–23 сентября; "
                "holdout не участвует в прогнозах."
            ),
            "Период holdout 24–30 сентября входит в baseline для обучения модели.",
        ),
        (
            "k лаг, где attempts[t] и errors[t+k]; сдвиг во времени.",
            "Временной лаг не рассчитывался для исследуемых рядов данных.",
        ),
    ],
)
def test_semantic_must_explain_contradictions_fail(explain, contradictory_text):
    # P0: Contradictory explanations with matched keywords must fail
    c = case(must_explain=[explain])
    res = result([[7], [24], [30], [500000], [730]], columns=["val"])
    s = state(res, contradictory_text)
    status, issues, details = evaluate_case(c, s)
    assert status == "failed"
    assert details["semantic_status"] == "failed"
    assert any("Response failed explanation rubric" in i for i in issues)


def test_correlation_actual_tool_result_bounds_fails():
    # P0: Actual correlation result from tool outside [-1, 1] must fail
    c = case(
        requirements=["correlation"],
        facts=[{"key": "correlation.planned_processed", "comparison": "exact", "value": 0.95}],
    )
    # Model tool produced r = 2.45 (impossible Pearson)
    res_invalid_r = result([[2.45]], columns=["r"], result_id="r_corr")
    s = state(res_invalid_r, "r = 2.45")
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("outside valid Pearson range" in i for i in issues)


def test_dashboard_duplicate_widget_matching_fails():
    # P1: One widget cannot satisfy multiple expected widgets
    c = case(
        outcome="answer",
        blocks_include=["dashboard_link"],
        dashboard={
            "widgets": [
                {"id": "w1", "chart_types_any": ["bar"]},
                {"id": "w2", "chart_types_any": ["bar"]},
            ]
        },
    )
    snap = DashboardSnapshot(
        dashboard_id="d_single_bar",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[
            _make_dashboard_widget("w_bar", chart_type="bar", result_id="r_0"),
            _make_dashboard_widget("w_line", chart_type="line", result_id="r_1"),
            _make_dashboard_widget("w_area", chart_type="area", result_id="r_2"),
        ],
    )
    res0 = result([[1]], columns=["val"], result_id="r_0")
    res1 = result([[2]], columns=["val"], result_id="r_1")
    res2 = result([[3]], columns=["val"], result_id="r_2")
    s = {
        "results": [res0, res1, res2],
        "plan": [SimpleNamespace(tool="create_dashboard")],
        "answer_blocks": [Block(type="dashboard_link", dashboard_id="d_single_bar")],
        "dashboard_snapshot": snap,
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("was not satisfied" in i for i in issues)


def test_dashboard_widget_missing_result_id_fails():
    # P1: Dashboard widget must have valid result_id with existing backing result
    c = case(
        outcome="answer",
        blocks_include=["dashboard_link"],
        dashboard={"widgets": [{"id": "w1", "chart_types_any": ["bar"]}]},
    )
    # Widget referencing nonexistent result_id
    snap = DashboardSnapshot(
        dashboard_id="d_no_res",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[
            _make_dashboard_widget("w_bar", chart_type="bar", result_id="r_nonexistent"),
            _make_dashboard_widget("w_line", chart_type="line", result_id="r_1"),
            _make_dashboard_widget("w_area", chart_type="area", result_id="r_2"),
        ],
    )
    res1 = result([[2]], columns=["val"], result_id="r_1")
    res2 = result([[3]], columns=["val"], result_id="r_2")
    s = {
        "results": [res1, res2],
        "plan": [SimpleNamespace(tool="create_dashboard")],
        "answer_blocks": [Block(type="dashboard_link", dashboard_id="d_no_res")],
        "dashboard_snapshot": snap,
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("was not satisfied" in i for i in issues)


def test_chart_missing_prerequisite_fails():
    # P1: Chart with reuse_result_from must fail if prerequisite result missing
    c = case(
        outcome="answer",
        reuse_result_from="missing_prereq_case",
        chart={"types_any": ["line"]},
    )
    res = result([[1, 10]], columns=["x", "y"], result_id="r_chart")
    cs = _make_chart_spec(chart_type="line", result_id="r_chart")
    s = {
        "results": [res],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block(type="chart", chart=cs.model_dump())],
    }
    # context_results has no missing_prereq_case
    status, issues, _ = evaluate_case(c, s, context_results=[])
    assert status == "failed"
    assert any("cannot verify reuse" in i for i in issues)


def test_chart_missing_data_fact_fails():
    # P1: Chart with undefined data_fact key in expect must fail closed
    c = case(
        outcome="answer",
        chart={"types_any": ["line"], "data_fact": "nonexistent_fact_key"},
        facts=[],
    )
    res = result([[1, 10]], columns=["x", "y"], result_id="r_chart")
    cs = _make_chart_spec(chart_type="line", result_id="r_chart")
    s = {
        "results": [res],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block(type="chart", chart=cs.model_dump())],
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("not defined in case facts" in i for i in issues)


def test_chart_null_points_fail():
    # P1: Rows with NULL in Y column do not count toward min_points
    c = case(
        outcome="answer",
        chart={"types_any": ["line"], "min_points": 5},
    )
    # 5 rows, but 3 of them have None in Y
    res = result(
        [
            ["2026-08-01", 10],
            ["2026-08-02", None],
            ["2026-08-03", None],
            ["2026-08-04", 20],
            ["2026-08-05", None],
        ],
        columns=["date", "val"],
        result_id="r_null_chart",
    )
    cs = _make_chart_spec(
        chart_type="line",
        result_id="r_null_chart",
        x_field="date",
        y_field="val",
    )
    s = {
        "results": [res],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block(type="chart", chart=cs.model_dump())],
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("fewer than required 5 points" in i for i in issues)


def test_clarification_unknown_resolves_fails_closed():
    # P1: Unknown resolves rubric must fail closed
    c = case(
        outcome="clarify",
        intent="ambiguous",
        tools_any=[],
        clarification={"resolves": "неизвестная_причина_уточнения", "options": ["Да", "Нет"]},
    )
    s = {
        "understanding": SimpleNamespace(
            intent="ambiguous", clarification=SimpleNamespace(options=["Да", "Нет"])
        ),
        "results": [],
        "plan": [],
        "answer_blocks": [Block(type="clarification", markdown="Уточните, пожалуйста, параметры.")],
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("has no automated evaluator" in i for i in issues)


def test_must_not_claim_extended_rubrics():
    # P0: Test compound rubrics coverage
    c = case(
        must_not_claim=[
            (
                "Вместо обновления создана несвязанная новая панель или "
                "возвращены старые временные result_id."
            ),
            "Ссылка подменена эталоном или результатом другого чата.",
        ]
    )
    res = result([[10]], columns=["val"])

    # Old temporary result_id text
    s1 = state(res, "Были возвращены старые временные result_id для панели.")
    status1, issues1, _ = evaluate_case(c, s1)
    assert status1 == "failed"
    assert any("old temporary result_ids" in i for i in issues1)

    # Result from another chat text
    s2 = state(res, "Использован результат другого чата для построения графика.")
    status2, issues2, _ = evaluate_case(c, s2)
    assert status2 == "failed"
    assert any("taken from another chat" in i for i in issues2)


@pytest.mark.parametrize(
    ("rows", "columns", "should_leak"),
    [
        # 1 valid pair with match: not a leak
        ([["2026-08-01", 100, 100]], ["date", "forecast", "actual"], False),
        # 2 valid pairs with matches: not a leak
        (
            [["2026-08-01", 100, 100], ["2026-08-02", 150, 150]],
            ["date", "forecast", "actual"],
            False,
        ),
        # 7 days, 1 match: not a leak
        (
            [
                ["2026-08-01", 100, 100],
                ["2026-08-02", 140, 150],
                ["2026-08-03", 210, 205],
                ["2026-08-04", 180, 190],
                ["2026-08-05", 220, 230],
                ["2026-08-06", 170, 160],
                ["2026-08-07", 195, 200],
            ],
            ["date", "forecast", "actual"],
            False,
        ),
        # 7 days, 5 matches (>= 70%): leak
        (
            [
                ["2026-08-01", 100, 100],
                ["2026-08-02", 150, 150],
                ["2026-08-03", 205, 205],
                ["2026-08-04", 190, 190],
                ["2026-08-05", 230, 230],
                ["2026-08-06", 170, 160],
                ["2026-08-07", 195, 200],
            ],
            ["date", "forecast", "actual"],
            True,
        ),
        # 7 days, 7 matches: leak
        (
            [[f"2026-08-0{i}", 100 + i, 100 + i] for i in range(1, 8)],
            ["date", "forecast", "actual"],
            True,
        ),
        # Unrelated table with matching columns 1 and 2: not a leak
        (
            [["job_1", 50, 50], ["job_2", 80, 80], ["job_3", 90, 90]],
            ["job_id", "limit_val", "threshold_val"],
            False,
        ),
    ],
)
def test_forecast_holdout_leak_scenarios(rows, columns, should_leak):
    c = case()
    c["id"] = "s10_holdout_mae_01"
    res = result(rows, columns=columns, result_id="r_eval")
    s = state(res, "Прогноз")
    _, issues, _ = evaluate_case(c, s)
    has_leak = any("Holdout leak detected" in i for i in issues)
    assert has_leak is should_leak


def test_chart_nan_inf_points_fail():
    # P1: Rows with NaN or Inf in Y column do not count toward min_points
    c = case(
        outcome="answer",
        chart={"types_any": ["line"], "min_points": 5},
    )
    res = result(
        [
            ["2026-08-01", 10],
            ["2026-08-02", float("nan")],
            ["2026-08-03", float("inf")],
            ["2026-08-04", 20],
            ["2026-08-05", "invalid_str"],
        ],
        columns=["date", "val"],
        result_id="r_nan_chart",
    )
    cs = _make_chart_spec(
        chart_type="line",
        result_id="r_nan_chart",
        x_field="date",
        y_field="val",
    )
    s = {
        "results": [res],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block(type="chart", chart=cs.model_dump())],
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("fewer than required 5 points" in i for i in issues)


def test_dashboard_widget_matching_id_with_wrong_facts_fails():
    # P1: Matching widget_id with wrong facts must fail
    c = case(
        outcome="answer",
        blocks_include=["dashboard_link"],
        dashboard={
            "widgets": [
                {
                    "id": "w_kpi",
                    "chart_types_any": ["kpi"],
                    "facts": ["kpi.expected_val"],
                }
            ]
        },
        facts=[{"key": "kpi.expected_val", "comparison": "exact", "value": 100}],
    )
    # Result has value 999 instead of 100
    res_wrong = result([[999]], columns=["val"], result_id="r_kpi")
    snap = DashboardSnapshot(
        dashboard_id="d_wrong_fact",
        dataset_id="ds_validation",
        title="Dashboard",
        created_at=datetime.now(UTC),
        widgets=[
            _make_dashboard_widget("w_kpi", chart_type="kpi", result_id="r_kpi"),
            _make_dashboard_widget("w_line", chart_type="line", result_id="r_kpi"),
            _make_dashboard_widget("w_area", chart_type="area", result_id="r_kpi"),
        ],
    )
    s = {
        "results": [res_wrong],
        "plan": [SimpleNamespace(tool="create_dashboard")],
        "answer_blocks": [Block(type="dashboard_link", dashboard_id="d_wrong_fact")],
        "dashboard_snapshot": snap,
    }
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("was not satisfied" in i for i in issues)


def test_correlation_tool_result_nan_fails():
    # P0: Tool returning NaN correlation value must fail
    c = case(
        requirements=["correlation"],
        facts=[{"key": "correlation.metric", "comparison": "exact", "value": 0.5}],
    )
    res_nan = result([[float("nan")]], columns=["r"], result_id="r_nan_corr")
    s = state(res_nan, "Корреляция не определена")
    status, issues, _ = evaluate_case(c, s)
    assert status == "failed"
    assert any("NaN" in i or "non-finite" in i for i in issues)


def test_chart_numpy_types_accepted_for_min_points():
    # P1: Numpy integers and floats must be accepted as valid finite points
    import numpy as np

    c = case(
        outcome="answer",
        chart={"types_any": ["line"], "min_points": 5},
    )
    res_numpy = result(
        [
            ["2026-08-01", np.int64(10)],
            ["2026-08-02", np.float64(20.5)],
            ["2026-08-03", np.int32(30)],
            ["2026-08-04", np.float32(40.2)],
            ["2026-08-05", np.int64(50)],
        ],
        columns=["date", "val"],
        result_id="r_numpy_chart",
    )
    cs = _make_chart_spec(
        chart_type="line",
        result_id="r_numpy_chart",
        x_field="date",
        y_field="val",
    )
    s = {
        "results": [res_numpy],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="chart", chart=cs.model_dump()),
            Block(type="text", markdown="Вот график динамики."),
        ],
    }
    status, issues, details = evaluate_case(c, s)
    assert status == "passed", issues
    assert details["chart_checks"]["passed"] is True


