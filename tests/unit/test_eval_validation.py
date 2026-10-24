"""Failures observed in the real golden run, plus controls against false passes."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from djedai.contracts.agent import Block
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


def result(rows=(), *, columns=(), tool="run_sql", status="ok", facts=(), params=None):
    return ToolResult(
        result_id="r_validation",
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
        ("Изменение: -0.00935585%", "passed"),
        ("Изменение: 2%", "failed"),
        ("Сравнение выполнено", "failed"),
    ],
)
def test_july_percentage_uses_verified_counts_and_checks_answer(answer, expected):
    actual = result(
        [["2025-07", 21377], ["2026-07", 21375]],
        facts=[Fact(text="SQL summary", kind="summary", values={"row_count": 2})],
    )
    status, issues, details = evaluate_case(case(july_facts()), state(actual, answer))
    assert status == expected, issues
    assert details["fact_checks"][1]["derivation"]["sources"] == ["july.counts"]


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
