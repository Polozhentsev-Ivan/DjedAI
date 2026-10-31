"""Failures observed in the real golden run, plus controls against false passes."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import yaml

from djedai.agent.graph import MODEL_ERROR_MARKDOWN
from djedai.contracts.agent import Block
from djedai.contracts.chart import ChartSpec, XAxis, YMeasure
from djedai.contracts.dashboard import (
    DashboardSnapshot,
    WidgetLayout,
    WidgetRecipe,
    WidgetRecipeStep,
    WidgetSnapshot,
    WidgetToolCall,
)
from djedai.contracts.tool_result import Fact, ToolError, ToolResult, ToolResultData

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from djedai.execution import TurnTimeout, current_budget  # noqa: E402
from scripts.eval_runner import (  # noqa: E402
    GoldenDataset,
    _case_skip_reason,
    _evaluate_semantics,
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
    ("answer", "status"),
    [("Всего 407661, ошибок 25873: 6.35%", "passed"), ("Всего 407661, ошибок 25873: 2%", "failed")],
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
    assert details["semantic_rubrics"][0]["kind"] == "must_explain"


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
    fact = {"key": "m.counts", "comparison": "exact", "value": [["2025-01", 2], ["2025-02", 3]]}
    assert check_fact(fact, [result([[99, "2025-01", 2], [98, "2025-02", 3]])])
    assert not check_fact(fact, [result([[98, "2025-02", 3], [99, "2025-01", 2]])])
    assert not check_fact(fact, [result([[99, "2025-01", 2], [99, "2025-01", 2]])])


def test_an_incidental_ratio_cannot_justify_a_wrong_count():
    actual = result([[500000], [1000]])
    assert evaluate_case(case(), state(actual, "500 строк"))[0] == "failed"
    assert evaluate_case(case(), state(actual, "Доля 0,002"))[0] == "passed"
    assert evaluate_case(case(), state(actual, "0,2 %"))[0] == "passed"
    # The same number is a valid percentage and an invalid count in this answer.
    assert evaluate_case(case(), state(actual, "0,2 % и 0,2 строки"))[0] == "failed"


@pytest.mark.parametrize(
    "options, expected",
    [([], "failed"), (["A"], "failed"), (["A", "A"], "failed"), (["A", "B"], "needs_review")],
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
        "Chart displayed values of the chart axes do not match expected fact 'chart.peak'" in i
        for i in issues
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


# --- Additional Tests for Thorough Review Findings ---


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


def test_rubric_without_evaluator_neither_fails_nor_passes():
    rubrics = {"must_explain": ["Смысл"], "must_not_claim": ["Ложь"]}
    status, issues, details = evaluate_case(
        case(**rubrics), state(result([[1]], columns=["n"]), "Ответ 1")
    )
    assert status == "needs_review"
    assert not issues
    assert details["automated_status"] == "passed"
    assert details["semantic_status"] == "not_evaluated"
    assert [item["status"] for item in details["semantic_rubrics"]] == ["not_evaluated"] * 2


def test_automated_failure_does_not_mark_unreviewed_rubrics_as_failed():
    fact = {"key": "x", "comparison": "exact", "value": 7}
    status, _, details = evaluate_case(
        case([fact], must_explain=["Смысл"]), state(result([[1]], columns=["n"]), "Ответ 1")
    )
    assert status == "failed"
    assert details["automated_status"] == "failed"
    assert details["semantic_status"] == "not_evaluated"


def test_blocked_prerequisite_uses_automated_status_only():
    prerequisite = {**case(must_explain=["Смысл"]), "id": "first"}
    target = {**case(), "id": "second", "context": {"previous_cases": ["first"]}}
    graph = SimpleNamespace(
        invoke=lambda *_args, **_kwargs: state(result([[1]], columns=["n"]), "Ответ 1")
    )
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation", tables=[]),
        load_catalog=None,
        dispatch=None,
    )
    report = process_case(target, {"first": prerequisite, "second": target}, None, graph, dataset)
    assert report["status"] == "passed"
    assert report["context_results"][0]["status"] == "needs_review"


def test_conclusion_about_all_data_from_limited_result_fails():
    limited = result([[7, 689.55]], columns=["month", "avg"]).model_copy(
        update={"sql": "SELECT month, avg FROM t ORDER BY avg DESC LIMIT 1"}
    )
    claim = "Пик 7, среднее 689.55. В выборке представлена информация только по одному месяцу."
    status, issues, details = evaluate_case(case(), state(limited, claim))
    assert status == "failed"
    assert details["automated_status"] == "passed"
    assert details["semantic_status"] == "failed"
    assert "LIMIT 1" in issues[0]
    assert evaluate_case(case(), state(limited, "Пик 7, среднее 689.55."))[0] == "passed"


def test_distinct_code_count_cannot_be_called_rows():
    fact = {
        "key": "chains.repeated",
        "comparison": "exact",
        "value": 23685,
        "unit": "разные непустые коды цепочек с более чем одной строкой",
    }
    tool = result([[23685]], columns=["n"])
    assert evaluate_case(case([fact]), state(tool, "Найдено **23 685** строк."))[0] == "failed"
    assert evaluate_case(case([fact]), state(tool, "Кодов цепочек: 23 685."))[0] == "passed"


def test_weekday_name_must_match_monday_zero_number():
    fact = {"key": "d", "comparison": "exact", "value": 4, "unit": "день: понедельник=0"}
    tool = result([[4, 5]], columns=["day", "n"])
    assert evaluate_case(case([fact]), state(tool, "Пик: Четверг (4), 5."))[0] == "failed"
    assert evaluate_case(case([fact]), state(tool, "Пик: Пятница (4), 5."))[0] == "passed"


def test_confirmed_fact_must_reach_the_answer_text():
    fact = {"key": "total", "comparison": "exact", "value": 4723}
    tool = result([[4723]], columns=["n"])
    assert evaluate_case(case([fact]), state(tool, "Расчёт выполнен."))[0] == "failed"
    assert evaluate_case(case([fact]), state(tool, "Итого 4 723."))[0] == "passed"


def test_labelled_categories_ignore_row_order_but_series_and_rankings_do_not():
    rows = [[4, 20, 104], [0, 10, 104]]
    weekday = {"key": "weekday.counts", "comparison": "exact", "value": [[0, 10], [4, 20]]}
    assert check_fact(weekday, [result(rows, columns=["d", "n", "a"])])
    months = {
        "key": "monthly.counts",
        "comparison": "exact",
        "value": [["2025-01", 10], ["2025-02", 20]],
    }
    assert not check_fact(months, [result([["2025-02", 20], ["2025-01", 10]], columns=["m", "n"])])
    ranked = {
        "key": "rank",
        "comparison": "exact",
        "unit": "порядок count DESC",
        "value": [["a", 20], ["b", 10]],
    }
    assert not check_fact(ranked, [result([["b", 10], ["a", 20]], columns=["s", "n"])])


def test_forecast_days_accept_midnight_timestamps_and_float_noise():
    fact = {
        "key": "forecast.growth7",
        "comparison": "exact",
        "value": [["2026-10-01", 737.0]],
    }
    rows = [["2026-10-01T00:00:00+03:00", 737.0000000001]]
    assert check_fact(fact, [result(rows, columns=["time_bucket", "yhat"])])


HOLDOUT = "Факт holdout использован как его прогноз."


def _holdout_case():
    fact = {
        "key": "forecast.holdout7",
        "comparison": "exact",
        "value": [["2026-09-24", 600, 610], ["2026-09-25", 620, 630], ["2026-09-26", 640, 650]],
    }
    return case([fact], must_not_claim=[HOLDOUT])


def test_holdout_rubric_is_decided_by_structured_evidence():
    columns = ["date", "forecast", "actual"]
    good = [["2026-09-24", 600, 610], ["2026-09-25", 620, 630], ["2026-09-26", 640, 650]]
    leaked = [["2026-09-24", 610, 610], ["2026-09-25", 630, 630], ["2026-09-26", 650, 650]]
    text = "Сравнение выполнено."
    answer = {"answer_contains_value": {"from_result": False}}
    good_status, _, good_details = evaluate_case(
        {**_holdout_case(), "expect": {**_holdout_case()["expect"], **answer}},
        state(result(good, columns=columns), text),
    )
    assert good_status == "passed"
    assert good_details["semantic_rubrics"][0]["status"] == "passed"
    leak_status, leak_issues, _ = evaluate_case(
        {**_holdout_case(), "expect": {**_holdout_case()["expect"], **answer}},
        state(result(leaked, columns=columns), text),
    )
    assert leak_status == "failed"
    assert any("holdout" in issue for issue in leak_issues)


def _correlation_case():
    fact = {"key": "corr", "comparison": "approx", "value": 0.98, "abs_tolerance": 0.005}
    return case(
        [fact],
        tools_any=["correlation"],
        population="complete_measure_pairs",
        answer_contains_value={"from_result": False},
    )


def test_correlation_sample_size_is_not_checked_as_pearson_r():
    tool = result(
        [["a", "b", 0.98, 0.0, 500000]],
        columns=["from", "to", "r", "p_value", "n"],
        tool="correlation",
    )
    assert evaluate_case(_correlation_case(), state(tool, "r = 0.98"))[0] == "passed"


@pytest.mark.parametrize(
    "row",
    [["a", "b", 0.98, 0.0, 400000], ["a", "b", 1.5, 0.0, 500000], ["a", "b", None, 0, 500000]],
)
def test_wrong_correlation_population_or_invalid_r_fails(row):
    tool = result([row], columns=["from", "to", "r", "p_value", "n"], tool="correlation")
    assert evaluate_case(_correlation_case(), state(tool, "r = 0.98"))[0] == "failed"


def test_lag_value_must_belong_to_its_own_lag():
    facts = [
        {"key": "lag.plus5_r", "comparison": "approx", "value": -0.007, "abs_tolerance": 0.0005},
        {"key": "lag.best_days", "comparison": "exact", "value": 0},
        {"key": "lag.best_r", "comparison": "approx", "value": 0.539, "abs_tolerance": 0.0005},
    ]
    columns = ["lag", "r", "p_value", "n"]
    c = case(facts, tools_any=["lagged_correlation"], answer_contains_value={"from_result": False})
    good = [[0, 0.539, 0, 730], [5, -0.007, 0.8, 725]]
    swapped = [[0, -0.007, 0, 730], [5, 0.539, 0.8, 725]]
    assert (
        evaluate_case(c, state(result(good, columns=columns, tool="lagged_correlation"), "ok"))[0]
        == "passed"
    )
    assert (
        evaluate_case(c, state(result(swapped, columns=columns, tool="lagged_correlation"), "ok"))[
            0
        ]
        == "failed"
    )


def _dashboard_state():
    snap = DashboardSnapshot(
        dashboard_id="dash_good",
        dataset_id="ds_validation",
        title="Valid Dashboard",
        created_at=datetime.now(UTC),
        widgets=[_make_dashboard_widget(f"w_{i}", result_id=f"r_{i}") for i in range(3)],
    )
    return {
        "results": [result([[7]], columns=["cnt"], result_id=f"r_{i}") for i in range(3)],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block(type="text", markdown="Дашборд готов"),
            Block(type="dashboard_link", dashboard_id="dash_good"),
        ],
        "dashboard_snapshot": snap,
    }


def _dashboard_case():
    fact = {"key": "total", "comparison": "exact", "value": 7}
    widget = {"id": "w_0", "chart_types_any": ["line"], "facts": ["total"]}
    return case([fact], dashboard={"widgets": [widget]})


def test_dashboard_refresh_must_recompute_the_same_values():
    fresh = SimpleNamespace(
        recompute_widget=lambda w: result([[7]], columns=["cnt"], result_id=f"new_{w.widget_id}")
    )
    status, issues, details = evaluate_case(_dashboard_case(), _dashboard_state(), dataset=fresh)
    assert status == "passed", issues
    assert details["dashboard_checks"]["not_evaluated"] == [
        "restart",
        "foreign_owner",
        "widget_chart_axes",
    ]

    wrong = SimpleNamespace(
        recompute_widget=lambda w: result([[8]], columns=["cnt"], result_id=f"new_{w.widget_id}")
    )
    assert any(
        "does not confirm" in issue
        for issue in evaluate_case(_dashboard_case(), _dashboard_state(), dataset=wrong)[1]
    )
    stale = SimpleNamespace(
        recompute_widget=lambda w: result([[7]], columns=["cnt"], result_id=w.result_id)
    )
    assert any(
        "new result_id" in issue
        for issue in evaluate_case(_dashboard_case(), _dashboard_state(), dataset=stale)[1]
    )


def test_dashboard_without_recompute_reports_refresh_as_not_evaluated():
    status, _, details = evaluate_case(_dashboard_case(), _dashboard_state())
    assert status == "passed"
    assert "refresh" in details["dashboard_checks"]["not_evaluated"]


def test_recipe_recompute_uses_production_tools_and_source_result(tmp_path):
    database = tmp_path / "golden.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE t AS SELECT 1 AS n UNION ALL SELECT 2")
    golden = GoldenDataset(
        SimpleNamespace(dataset_id="ds", tables=[SimpleNamespace(table_id="t")]),
        database,
        SimpleNamespace(cleanup=lambda: None),
    )
    parent = WidgetToolCall(tool="run_sql", params={"sql": "SELECT n FROM t"})
    child = WidgetToolCall(tool="run_sql", params={"sql": "SELECT SUM(n) AS s FROM source_result"})
    recipe = WidgetRecipe(
        steps=[
            WidgetRecipeStep(step_id="a", call=parent),
            WidgetRecipeStep(step_id="b", call=child, source_step_id="a"),
        ],
        output_step_id="b",
    )
    widget = _make_dashboard_widget("w").model_copy(update={"tool_call": None, "recipe": recipe})
    refreshed = golden.recompute_widget(widget)
    assert refreshed.status == "ok", refreshed.error
    assert refreshed.data.rows == [[3]]
    assert refreshed.result_id != widget.result_id
    assert golden.dispatch("run_sql", {"sql": "SELECT COUNT(*) FROM t"}).data.rows == [[2]]


def test_no_golden_case_is_skipped_except_input_validation():
    cases = yaml.safe_load((Path(__file__).parents[2] / "eval/questions.yaml").read_text("utf-8"))
    total = 0
    for item in cases:
        expect = item["expect"]
        total += len(expect["must_explain"]) + len(expect["must_not_claim"])
        assert not _case_skip_reason(item) or expect["outcome"] == "input_error"
    assert total == 203


def test_timed_out_turn_is_never_automated_passed():
    prepared = state(result([[1]], columns=["n"]), "Ответ 1")

    def invoke(*_args, **_kwargs):
        raise TurnTimeout

    graph = SimpleNamespace(
        invoke=invoke, get_state=lambda _config: SimpleNamespace(values=prepared)
    )
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation", tables=[]),
        load_catalog=None,
        dispatch=None,
    )
    report = process_case(case(), {}, None, graph, dataset)
    assert report["status"] == "failed"
    assert report["automated_status"] == "failed"
    assert report["error_type"] == "TurnTimeout"


def test_semantics_report_clarification_options_as_not_evaluated():
    """Варианты уточнения заданы списком: это не должно ронять оценку вопроса."""
    case = {
        "id": "c",
        "question": "q",
        "expect": {"clarification": {"resolves": "причина", "options": ["а", "б"]}},
    }

    semantics = _evaluate_semantics(case, "", [])

    options = [r for r in semantics["rubrics"] if r["kind"] == "clarification.options"]
    assert options[0]["status"] == "not_evaluated"


STATUS_ROWS = [["Успешно", 454882, 0], ["Ошибка", 40414, 0], ["Отменено", 4704, 0]]
STATUS_TEXT = "Успешно 454882, Ошибка 40414, Отменено 4704"


def _status_answer(x_field="status", y_field="n", rows=None, columns=None, **chart_fields):
    res = result(
        rows or STATUS_ROWS, columns=columns or ["status", "n", "unrelated"], result_id="r_chart"
    )
    spec = ChartSpec(
        chart_id="c1",
        type="bar",
        title="Статусы",
        result_id="r_chart",
        x=XAxis(field=x_field, kind="category"),
        y=[YMeasure(field=y_field, format="int")],
        **chart_fields,
    )
    answer = state(res, STATUS_TEXT)
    answer["answer_blocks"].append(Block(type="chart", chart=spec.model_dump()))
    return evaluate_case(_status_case(), answer)


def _status_case():
    golden = yaml.safe_load(
        (Path(__file__).parents[2] / "eval/questions.yaml").read_text(encoding="utf-8")
    )
    return next(item for item in golden if item["id"] == "s4_status_bar_01")


def _chart_issues(issues):
    return [issue for issue in issues if "Chart" in issue]


def test_chart_on_the_measure_column_passes_the_golden_check():
    status, issues, details = _status_answer()
    assert _chart_issues(issues) == []
    assert details["chart_checks"]["passed"] is True
    assert details["chart_checks"]["charts"][0]["data_fact_columns"] == ["status", "n"]
    assert status == "needs_review"


def test_chart_on_an_unrelated_column_fails_although_the_result_has_the_numbers():
    status, issues, details = _status_answer(y_field="unrelated")
    assert status == "failed"
    assert any("displayed values of the chart axes" in issue for issue in issues)
    assert details["chart_checks"]["passed"] is False


def test_chart_with_a_wrong_x_fails_although_the_right_column_is_next_to_it():
    rows = [[label, "другое", n] for label, n, _ in STATUS_ROWS]
    status, issues, _ = _status_answer(x_field="wrong", rows=rows, columns=["status", "wrong", "n"])
    assert status == "failed"
    assert any("displayed values of the chart axes" in issue for issue in issues)


def test_chart_with_axes_on_shuffled_pairs_fails():
    rows = [["Успешно", 40414, 0], ["Ошибка", 454882, 0], ["Отменено", 4704, 0]]
    status, issues, _ = _status_answer(rows=rows)
    assert status == "failed"
    assert any("displayed values of the chart axes" in issue for issue in issues)


def test_other_chart_checks_stay_next_to_the_axes_check():
    _, issues, _ = _status_answer(limit=2)
    assert any("truncates series" in issue for issue in issues)
    assert not any("displayed values" in issue for issue in issues)


def test_chart_without_limit_shows_the_whole_series():
    """Ряд не режется пределом, который никто не задавал (#245)."""
    rows = [[f"2026-01-{day:02d}", day] for day in range(1, 25)]
    c = case(chart={"types_any": ["line"], "min_points": 24})
    spec = ChartSpec(
        chart_id="c1",
        type="line",
        title="Chart",
        result_id="r_chart",
        x=XAxis(field="date", kind="time"),
        y=[YMeasure(field="val", format="number")],
    )
    assert spec.limit is None
    s = state(result(rows, columns=["date", "val"], result_id="r_chart"), "24")
    s["answer_blocks"].append(Block(type="chart", chart=spec.model_dump()))
    assert not [issue for issue in evaluate_case(c, s)[1] if "truncates" in issue]


@pytest.mark.parametrize("message", [MODEL_ERROR_MARKDOWN, "Changed wording"])
def test_typed_terminal_error_fails_even_with_matching_facts(message):
    s = state(result([[123]]), "123")
    s["answer_blocks"].append(Block(type="error", markdown=message))
    s["terminal_error"] = "SERVER_ERROR"
    c = case(facts=[{"key": "count", "comparison": "exact", "value": 123}])
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation"),
        load_catalog=lambda _: None,
        dispatch=lambda *a: None,
    )
    graph = SimpleNamespace(invoke=lambda *a, **k: s)
    report = process_case(c, {c["id"]: c}, None, graph, dataset)
    assert report["status"] == "failed"
    assert report["fact_checks"] == []
    assert report["terminal_error"] == "SERVER_ERROR"
    assert report["diagnostics"][-1]["status"] == "error"
    assert report["diagnostics"][-1]["error_type"] == "SERVER_ERROR"


def test_recovered_model_error_does_not_fail_successful_turn():
    def invoke(input_state, config):
        config["configurable"]["diagnostics"].last_error = "SERVER_ERROR"
        return state(result([[123]]), "123")

    c = case()
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation"),
        load_catalog=lambda _: None,
        dispatch=lambda *a: None,
    )
    report = process_case(c, {c["id"]: c}, None, SimpleNamespace(invoke=invoke), dataset)
    assert report["status"] == "passed"
    assert report["diagnostics"][-1]["status"] == "success"
    assert report["diagnostics"][-1]["error_type"] is None


@pytest.mark.parametrize("blocked", [False, True])
def test_total_includes_every_outcome_in_partial_and_completed_reports(
    tmp_path, monkeypatch, blocked
):
    skipped = case(outcome="input_error")
    skipped["id"] = "skipped"
    questions, manifest = suite_files(tmp_path, [case(), skipped])
    output = tmp_path / "report.json"
    outcomes = ("passed", "failed", "needs_review", "skipped", "blocked", "pending")

    def assert_counts(report):
        summary = report["summary"]
        assert summary["total"] == len(report["cases"]) == 2
        assert summary["total"] == sum(summary[key] for key in outcomes)

    def invoke(*args, **kwargs):
        partial = json.loads(output.read_text("utf-8"))
        assert_counts(partial)
        assert partial["summary"]["pending"] == 1
        assert partial["summary"]["skipped"] == 1
        return state(result([[123]]), "123")

    def client():
        if blocked:
            raise ValueError("Unavailable")
        return None

    monkeypatch.setattr("scripts.eval_runner.get_agent_client", client)
    monkeypatch.setattr(
        "scripts.eval_runner.build_agent_graph", lambda: SimpleNamespace(invoke=invoke)
    )
    dataset = SimpleNamespace(
        catalog=SimpleNamespace(dataset_id="ds_validation"),
        load_catalog=lambda _: None,
        dispatch=lambda *a: None,
    )
    report = run_eval(questions, output, manifest_path=manifest, dataset=dataset)
    assert_counts(report)
    assert report["summary"]["pending"] == 0
    assert report["summary"]["blocked" if blocked else "passed"] == 1
