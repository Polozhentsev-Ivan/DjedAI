import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from djedai.contracts.chart import ChartSpec, XAxis, YMeasure  # noqa: E402
from djedai.contracts.dashboard import (  # noqa: E402
    DashboardSnapshot,
    WidgetLayout,
    WidgetSnapshot,
    WidgetToolCall,
)
from djedai.contracts.tool_result import Fact, ToolError, ToolResult, ToolResultData  # noqa: E402
from scripts.eval_runner import (  # noqa: E402
    check_fact,
    evaluate_case,
    open_dataset,
    run_eval,
)
from scripts.verify_golden import load_suite  # noqa: E402


class Block:
    def __init__(self, type: str = "text", markdown: str = "", **fields):
        self.type = type
        self.markdown = markdown
        for name, value in fields.items():
            setattr(self, name, value)


def tool_result(
    *,
    rows: list[list] | None = None,
    facts: list[Fact] | None = None,
    tool: str = "run_sql",
    status: str = "ok",
    error: ToolError | None = None,
    result_id: str | None = None,
) -> ToolResult:
    return ToolResult(
        result_id=result_id or f"r_{tool}_{status}",
        status=status,
        tool=tool,
        data=ToolResultData(columns=[], rows=rows or []),
        facts=facts or [],
        error=error,
    )


class StubDataset:
    """The graph only needs a catalog and a dispatcher; tests never hit DuckDB."""

    def __init__(self, dataset_id: str = "hk_background_jobs_v1"):
        self.catalog = SimpleNamespace(dataset_id=dataset_id)
        self.closed = False

    def load_catalog(self, dataset_id: str):
        if dataset_id != self.catalog.dataset_id:
            raise ValueError(f"Agent requested unknown dataset {dataset_id!r}")
        return self.catalog

    def dispatch(self, tool: str, params: dict):
        raise AssertionError("Tool dispatch must not run in this unit test")

    def close(self):
        self.closed = True


def answer_case(case_id: str, **expect) -> dict:
    return {
        "id": case_id,
        "dataset": "hk_background_jobs_v1",
        "question": f"question for {case_id}",
        "expect": {
            "outcome": "answer",
            "tools_any": ["run_sql"],
            "facts": [],
            **expect,
        },
    }


def test_check_fact_requires_tool_results_not_answer_text():
    fact = {"key": "dataset.row_count", "comparison": "exact", "value": 500000}

    wrong = tool_result(rows=[[1500000]])
    assert check_fact(fact, [wrong]) is False

    right = tool_result(rows=[[500000]])
    assert check_fact(fact, [right]) is True
    # A number that only appears in prose is not evidence about the data.
    assert check_fact(fact, []) is False


def test_check_fact_approx_applies_tolerance():
    fact = {
        "key": "processed.mean",
        "comparison": "approx",
        "value": 66782.03742,
        "abs_tolerance": 0.005,
    }

    assert check_fact(fact, [tool_result(rows=[[66782.04]])]) is True
    assert check_fact(fact, [tool_result(rows=[[66782.05]])]) is False
    assert check_fact(fact, []) is False
    assert check_fact(fact, [tool_result(rows=[[70000]])]) is False


def test_check_fact_exact_unordered_keeps_category_value_pairs():
    fact = {
        "key": "status.counts",
        "comparison": "exact_unordered",
        "value": [["Успешно", 454882], ["Ошибка", 40414], ["Отменено", 4704]],
    }

    correct = tool_result(rows=[["Успешно", 454882], ["Ошибка", 40414], ["Отменено", 4704]])
    assert check_fact(fact, [correct]) is True

    reordered = tool_result(rows=[["Отменено", 4704], ["Успешно", 454882], ["Ошибка", 40414]])
    assert check_fact(fact, [reordered]) is True

    swapped = tool_result(rows=[["Успешно", 40414], ["Ошибка", 454882], ["Отменено", 4704]])
    assert check_fact(fact, [swapped]) is False
    assert check_fact(fact, []) is False


def test_check_fact_exact_unordered_compares_numeric_cells_by_value():
    fact = {
        "key": "status.counts",
        "comparison": "exact_unordered",
        "value": [["Успешно", 454882], ["Ошибка", 40414], ["Отменено", 4704]],
    }
    as_double = tool_result(rows=[["Отменено", 4704.0], ["Успешно", 454882.0], ["Ошибка", 40414.0]])
    assert check_fact(fact, [as_double]) is True

    duplicates = tool_result(rows=[["Успешно", 454882.0], ["Ошибка", 40414.0]])
    assert check_fact(fact, [duplicates]) is False

    ordered = {
        "key": "july.counts",
        "comparison": "exact",
        "value": [["2025-07", 21377], ["2026-07", 21375]],
    }
    as_double = tool_result(rows=[["2025-07", 21377.0], ["2026-07", 21375.0]])
    assert check_fact(ordered, [as_double]) is True
    reordered = tool_result(rows=[["2026-07", 21375.0], ["2025-07", 21377.0]])
    assert check_fact(ordered, [reordered]) is False


def test_check_fact_exact_unordered_accepts_column_list_from_fact_values():
    columns = ["Код запуска", "Код ошибки", "Статус"]
    fact = {"key": "dataset.columns", "comparison": "exact_unordered", "value": columns}
    result = tool_result(facts=[Fact(text="columns", values={"columns": list(reversed(columns))})])
    assert check_fact(fact, [result]) is True


def test_check_fact_compares_timestamps_by_instant():
    fact = {
        "key": "start.min",
        "comparison": "exact",
        "value": "2024-10-01T00:00:57+03:00",
    }
    same = tool_result(rows=[["2024-10-01 00:00:57"]])
    other = tool_result(rows=[["2024-10-01 00:00:58"]])
    assert check_fact(fact, [same]) is True
    assert check_fact(fact, [other]) is False


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-30T21:59+03:00",
        "2026-09-30T18:59Z",
        "2026-09-30 21:59",
        "2026-09-30T21:59:00.000+0300",
    ],
)
def test_check_fact_accepts_equivalent_timestamp_precisions(timestamp):
    fact = {
        "key": "start.max",
        "comparison": "exact",
        "value": "2026-09-30T21:59:00+03:00",
    }
    assert check_fact(fact, [tool_result(rows=[[timestamp]])]) is True
    assert check_fact(fact, [tool_result(rows=[["2026-09-30T21:58+03:00"]])]) is False


@pytest.mark.parametrize(
    "calendar_text",
    [
        "2024 г.",
        "2024г.",
        "2024 г. Итоги",
        "2024 г.\nИтоги",
        "2024 год",
        "2026-09-30T21:59+03:00",
        "2026-09-30T21:59-03:00",
        "2026-09-30T21:59+0300",
        "2026-09-30T21:59Z",
        "2026-09-30 21:59+03:00",
        "2026-09-30T21:59:37+03:00",
        "2026-09-30T21:59:37.123456Z",
    ],
)
def test_answer_calendar_notations_are_not_measured_values(calendar_text):
    case = answer_case(
        "calendar_answer",
        facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
        answer_contains_value={"from_result": True},
    )
    result = {
        "results": [tool_result(rows=[[500000]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("text", f"Всего 500000 строк.\n{calendar_text}")],
    }

    status, issues, details = evaluate_case(case, result)

    assert status == "passed", issues
    assert details["fact_checks"][0]["passed"] is True


@pytest.mark.parametrize("wrong_count", [2024, 59, 500])
def test_calendar_exemptions_do_not_hide_wrong_measured_values(wrong_count):
    case = answer_case(
        "wrong_calendar_answer",
        facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
        answer_contains_value={"from_result": True},
    )
    result = {
        "results": [tool_result(rows=[[500000]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block("text", f"2024 г. На 2026-09-30T21:59+03:00 всего {wrong_count} строк.")
        ],
    }

    status, issues, details = evaluate_case(case, result)

    assert status == "failed"
    assert details["fact_checks"][0]["passed"] is True
    assert f"Number {wrong_count} in the answer is not backed by tool results" in issues


def test_correct_tool_result_does_not_hide_wrong_answer_number():
    case = answer_case(
        "s1_overview_01",
        facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
        answer_contains_value={"from_result": True},
    )
    result = {
        "results": [tool_result(rows=[[500000]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("text", "Всего 1500000 строк")],
    }

    status, issues, details = evaluate_case(case, result)

    assert status == "failed"
    # The reference itself is correct, but the hallucinated answer number is rejected.
    assert details["fact_checks"][0]["result"] is True
    assert any("1500000" in issue and "not backed" in issue for issue in issues)


def test_answer_number_is_not_enough_when_tool_result_differs():
    case = answer_case(
        "s1_overview_01",
        facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
        answer_contains_value={"from_result": True},
    )
    result = {
        "results": [tool_result(rows=[[2]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("text", "Всего 500000 строк")],
    }

    status, issues, details = evaluate_case(case, result)

    assert status == "failed"
    assert details["fact_checks"][0]["result"] is False
    assert any("not confirmed by tool results" in issue for issue in issues)


def test_answer_number_must_be_backed_by_tool_results():
    case = answer_case(
        "s1_overview_01",
        facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
        answer_contains_value={"from_result": True},
    )
    result = {
        "results": [tool_result(rows=[[500000]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("text", "Всего 500 000 строк, средняя обработка 12345")],
    }

    status, issues, details = evaluate_case(case, result)

    assert status == "failed"
    assert details["fact_checks"][0]["passed"] is True
    assert any("12345" in issue and "not backed" in issue for issue in issues)


def test_answer_rounding_and_user_constants_are_allowed():
    case = {
        "id": "s3_duration_extremes_01",
        "question": "Порог задан мной: считай попытки строго дольше 3600 секунд.",
        "expect": {
            "outcome": "answer",
            "tools_any": ["run_sql"],
            "answer_contains_value": {"from_result": True},
            "facts": [
                {
                    "key": "duration.max_seconds",
                    "comparison": "approx",
                    "value": 66782.03742,
                    "abs_tolerance": 0.005,
                }
            ],
        },
    }
    result = {
        "results": [tool_result(rows=[[66782.03742]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("text", "Порог 3600 секунд; максимум 66 782,04 секунды.")],
    }

    status, issues, _ = evaluate_case(case, result)

    assert status == "passed", issues


def test_answer_may_derive_share_from_tool_counts():
    case = answer_case("s2_status_01", answer_contains_value={"from_result": True})
    result = {
        "results": [
            tool_result(rows=[["Успешно", 454882], ["Ошибка", 40414]]),
            tool_result(rows=[[500000]]),
        ],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("text", "Успешно 454882 из 500000, это 90,98 %")],
    }

    status, issues, _ = evaluate_case(case, result)

    assert status == "passed", issues


def test_terminal_api_error_is_failed_before_facts():
    case = answer_case(
        "s1_overview_01",
        facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
    )
    result = {
        "results": [tool_result(rows=[[500000]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [
            Block("text", "Всего 500000 строк"),
            Block("error", "API Error: upstream 500"),
        ],
    }

    status, issues, details = evaluate_case(case, result)

    assert status == "failed"
    assert any("API Error" in issue for issue in issues)
    assert details["fact_checks"] == []


def test_failed_tool_run_is_failed_even_with_matching_number():
    case = answer_case(
        "edge_ambiguous_measure_01",
        facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
    )
    failed = tool_result(
        status="error",
        error=ToolError(code="not_implemented", message="tool is not connected"),
    )
    result = {
        "results": [failed],
        "plan": [SimpleNamespace(tool="value_counts")],
        "answer_blocks": [Block("text", "Всего 500000 строк")],
    }

    status, issues, _ = evaluate_case(case, result)

    assert status == "failed"
    assert any("not connected" in issue for issue in issues)


def test_fallback_intent_for_rejection_is_failed():
    case = {
        "id": "edge_off_topic_01",
        "question": "Какая погода?",
        "expect": {"outcome": "reject", "intent": "off_topic"},
    }
    result = {
        "understanding": SimpleNamespace(intent="ambiguous"),
        "answer_blocks": [Block("error", "Уточните запрос.")],
        "results": [],
        "plan": [],
    }
    status, issues, _ = evaluate_case(case, result)
    assert status == "failed"
    assert any("off_topic" in issue for issue in issues)


@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.yaml.load")
def test_run_eval_skips_unsupported(mock_yaml_load, mock_build_graph, tmp_path):
    mock_yaml_load.return_value = [
        {
            "id": "unsupported_case",
            "dataset": "ds_1",
            "expect": {"outcome": "answer", "requirements": ["unsupported_custom_feature"]},
        }
    ]
    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")
    output_file = tmp_path / "report.json"

    report = run_eval(dummy_yaml, output_file)

    mock_build_graph.assert_not_called()
    assert report["summary"]["skipped"] == 1
    assert report["summary"]["total"] == 0
    assert report["cases"][0]["status"] == "skipped"


def test_supported_requirements_include_charts_dashboards_correlation_forecast():
    from scripts.eval_runner import SUPPORTED_REQUIREMENTS

    assert {"charts", "dashboards", "correlation", "forecast"}.issubset(SUPPORTED_REQUIREMENTS)


@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.get_agent_client")
@patch("scripts.eval_runner.yaml.load")
def test_run_eval_skips_input_validation(
    mock_yaml_load, mock_get_client, mock_build_graph, tmp_path
):
    mock_yaml_load.return_value = [
        {
            "id": "edge_empty_input_01",
            "dataset": "ds_1",
            "question": "",
            "expect": {"outcome": "input_error", "requirements": []},
        }
    ]
    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")

    report = run_eval(dummy_yaml, tmp_path / "report.json")

    mock_build_graph.assert_not_called()
    mock_get_client.assert_not_called()
    assert report["summary"]["skipped"] == 1
    assert report["summary"]["total"] == 0


@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.get_agent_client")
@patch("scripts.eval_runner.yaml.load")
def test_run_eval_success_scenario(mock_yaml_load, mock_get_client, mock_build_graph, tmp_path):
    mock_yaml_load.return_value = [
        answer_case(
            "success_case",
            facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 42}],
        )
    ]
    mock_graph = MagicMock()
    mock_graph.invoke.return_value = {
        "results": [tool_result(rows=[[42]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("text", "The answer is 42.")],
    }
    mock_build_graph.return_value = mock_graph

    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")
    output_file = tmp_path / "report.json"
    report = run_eval(dummy_yaml, output_file, dataset=StubDataset())

    assert report["summary"]["passed"] == 1
    assert report["summary"]["failed"] == 0
    assert report["summary"]["total"] == 1
    assert report["cases"][0]["status"] == "passed"
    assert report["cases"][0]["used_tools"] == ["run_sql"]
    assert report["cases"][0]["fact_checks"][0]["passed"] is True
    assert report["cases"][0]["thread_id"] == "eval_success_case"
    config = mock_graph.invoke.call_args.kwargs["config"]
    assert "catalog_loader" in config["configurable"]
    assert "tool_dispatcher" in config["configurable"]


@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.get_agent_client")
@patch("scripts.eval_runner.yaml.load")
def test_run_eval_marks_api_error_as_failed(
    mock_yaml_load, mock_get_client, mock_build_graph, tmp_path
):
    mock_yaml_load.return_value = [
        answer_case(
            "s1_overview_01",
            facts=[{"key": "dataset.row_count", "comparison": "exact", "value": 500000}],
        )
    ]
    mock_graph = MagicMock()
    mock_graph.invoke.return_value = {
        "results": [tool_result(rows=[[500000]])],
        "plan": [SimpleNamespace(tool="run_sql")],
        "answer_blocks": [Block("error", "API Error: model is unavailable")],
    }
    mock_build_graph.return_value = mock_graph

    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")
    report = run_eval(dummy_yaml, tmp_path / "report.json", dataset=StubDataset())

    assert report["summary"]["failed"] == 1
    assert report["summary"]["passed"] == 0
    assert report["cases"][0]["status"] == "failed"
    assert "API Error" in report["cases"][0]["issues"][0]


@patch("scripts.eval_runner.get_agent_client", side_effect=ValueError("no API key"))
@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.yaml.load")
def test_missing_model_client_blocks_instead_of_passing(
    mock_yaml_load, mock_build_graph, mock_get_client, tmp_path
):
    mock_yaml_load.return_value = [answer_case("s1_overview_01")]
    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")

    report = run_eval(dummy_yaml, tmp_path / "report.json")

    mock_build_graph.assert_not_called()
    assert report["summary"]["blocked"] == 1
    assert report["summary"]["total"] == 1
    assert report["summary"]["passed"] == 0
    assert report["cases"][0]["status"] == "blocked"
    assert "Model client unavailable" in report["cases"][0]["reason"]


@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.get_agent_client")
@patch("scripts.eval_runner.yaml.load")
def test_context_questions_run_in_one_chat_with_history(
    mock_yaml_load, mock_get_client, mock_build_graph, tmp_path
):
    dependency = answer_case("s2_status_01")
    case = answer_case("s6_filter_production_01")
    case["context"] = {"previous_cases": ["s2_status_01"], "same_chat": True}
    mock_yaml_load.return_value = [dependency, case]

    calls = []

    def invoke(state, config=None):
        calls.append((state, config))
        question = state["messages"][-1].content
        return {
            "results": [tool_result(rows=[[1]])],
            "plan": [SimpleNamespace(tool="run_sql")],
            "answer_blocks": [Block("text", f"answer for {question}")],
        }

    mock_graph = MagicMock()
    mock_graph.invoke.side_effect = invoke
    mock_build_graph.return_value = mock_graph

    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")
    report = run_eval(dummy_yaml, tmp_path / "report.json", dataset=StubDataset())
    by_id = {item["id"]: item for item in report["cases"]}

    assert by_id["s2_status_01"]["status"] == "passed"
    assert by_id["s6_filter_production_01"]["status"] == "passed"
    assert by_id["s6_filter_production_01"]["context_cases"] == ["s2_status_01"]

    target_calls = [
        item
        for item in calls
        if item[1]["configurable"]["thread_id"] == "eval_s6_filter_production_01"
    ]
    assert len(target_calls) == 2
    assert [message.role for message in target_calls[-1][0]["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert [message.content for message in target_calls[-1][0]["messages"]] == [
        "question for s2_status_01",
        "answer for question for s2_status_01",
        "question for s6_filter_production_01",
    ]


@patch("scripts.eval_runner.build_agent_graph")
@patch("scripts.eval_runner.get_agent_client")
@patch("scripts.eval_runner.yaml.load")
def test_failed_prerequisite_blocks_context_case(
    mock_yaml_load, mock_get_client, mock_build_graph, tmp_path
):
    dependency = answer_case("s2_status_01")
    case = answer_case("s6_filter_production_01")
    case["context"] = {"previous_cases": ["s2_status_01"], "same_chat": True}
    mock_yaml_load.return_value = [dependency, case]

    mock_graph = MagicMock()
    mock_graph.invoke.return_value = {
        "results": [],
        "plan": [],
        "answer_blocks": [Block("error", "API Error: model is unavailable")],
    }
    mock_build_graph.return_value = mock_graph

    dummy_yaml = tmp_path / "dummy.yaml"
    dummy_yaml.write_text("dummy")
    report = run_eval(dummy_yaml, tmp_path / "report.json", dataset=StubDataset(), max_workers=1)
    by_id = {item["id"]: item for item in report["cases"]}

    assert by_id["s2_status_01"]["status"] == "failed"
    assert by_id["s6_filter_production_01"]["status"] == "blocked"
    assert "Prerequisite s2_status_01 failed" in by_id["s6_filter_production_01"]["reason"]
    executed = [
        call
        for call in mock_graph.invoke.call_args_list
        if call.kwargs["config"]["configurable"]["thread_id"] == "eval_s6_filter_production_01"
    ]
    assert len(executed) == 1


TOP_SERVICES = [
    ["Уведомления", 71655],
    ["Каталог товаров", 71593],
    ["Платежи", 67068],
    ["Складской учёт", 63375],
    ["Хранилище данных", 59316],
]


def _memory_case(**overrides):
    expect = {
        "facts": [{"key": "service.top5", "comparison": "exact", "value": TOP_SERVICES}],
        "reuse_result_from": "s3_top_services_01",
        "answer_contains_value": {"from_result": True},
    }
    expect.update(overrides)
    return answer_case("s6_second_ranked_service_01", **expect)


def _memory_context():
    return [
        {
            "id": "s3_top_services_01",
            "tool_results": [tool_result(rows=TOP_SERVICES)],
        }
    ]


def test_memory_answer_is_confirmed_by_declared_previous_result_without_new_tool_call():
    case = _memory_case()
    result = {
        "results": [],
        "plan": [],
        "answer_blocks": [Block("text", "Второй — «Каталог товаров», 71593 попытки.")],
    }

    status, issues, details = evaluate_case(case, result, context_results=_memory_context())

    assert status == "passed", issues
    assert details["fact_checks"][0]["source"] == "context:s3_top_services_01"
    assert details["reuse"] == {
        "from": "s3_top_services_01",
        "result_ids": ["r_run_sql_ok"],
        "tools": ["run_sql"],
        "accepted": True,
    }


def test_memory_answer_with_wrong_number_is_still_failed():
    case = _memory_case()
    result = {
        "results": [],
        "plan": [],
        "answer_blocks": [Block("text", "Второй — «Каталог товаров», 71594 попытки.")],
    }

    status, issues, details = evaluate_case(case, result, context_results=_memory_context())

    assert status == "failed"
    assert details["fact_checks"][0]["passed"] is True
    assert details["fact_checks"][0]["source"] == "context:s3_top_services_01"
    assert any("71594" in issue and "not backed" in issue for issue in issues)


def test_memory_answer_requires_an_explicit_reuse_declaration():
    case = _memory_case(reuse_result_from=None)
    result = {
        "results": [],
        "plan": [],
        "answer_blocks": [Block("text", "Второй — «Каталог товаров», 71593 попытки.")],
    }

    status, issues, _ = evaluate_case(case, result, context_results=_memory_context())

    assert status == "failed"
    assert any("Did not use any expected tools" in issue for issue in issues)
    assert any("not confirmed by tool results" in issue for issue in issues)


def test_memory_answer_requires_the_declared_previous_result_to_exist():
    case = _memory_case()
    result = {
        "results": [],
        "plan": [],
        "answer_blocks": [Block("text", "Второй — «Каталог товаров», 71593 попытки.")],
    }

    status, issues, details = evaluate_case(case, result, context_results=[])

    assert status == "failed"
    assert any("was not found to verify reuse" in issue for issue in issues)
    assert details["fact_checks"][0]["passed"] is False


def _dashboard_widget(widget_id: str, chart_type: str, result_id: str) -> WidgetSnapshot:
    return WidgetSnapshot(
        widget_id=widget_id,
        chart=ChartSpec(
            chart_id=f"ch_{widget_id}",
            type=chart_type,
            title=f"Widget {widget_id}",
            result_id=result_id,
            x=XAxis(field="category") if chart_type != "kpi" else None,
            y=[YMeasure(field="count")],
        ),
        layout=WidgetLayout(x=0, y=0, w=4, h=2),
        tool_call=WidgetToolCall(tool="run_sql", params={"sql": "SELECT 1"}),
        result_id=result_id,
        status="ok",
    )


def test_refreshed_dashboard_must_reuse_the_previous_saved_dashboard():
    case = answer_case(
        "s6_refresh_previous_dashboard_01",
        blocks_include=["dashboard_link"],
        reuse_dashboard_from="s5_overview_dashboard_01",
        dashboard={
            "persisted": True,
            "refresh": "recompute_recipes",
            "widgets": [
                {"id": "a", "chart_types_any": ["kpi"]},
                {"id": "b", "chart_types_any": ["bar"]},
                {"id": "c", "chart_types_any": ["pie"]},
            ],
        },
    )
    widgets = [
        _dashboard_widget("w1", "kpi", "r1"),
        _dashboard_widget("w2", "bar", "r2"),
        _dashboard_widget("w3", "pie", "r3"),
    ]
    snapshot = DashboardSnapshot(
        dashboard_id="d_prior",
        dataset_id="hk_background_jobs_v1",
        title="Обзор фоновых заданий",
        widgets=widgets,
        created_at=datetime.now(UTC),
        refreshed_at=datetime.now(UTC),
    )
    results = [tool_result(rows=[[1]], result_id=f"r{index}") for index in range(1, 4)]
    context = [
        {
            "id": "s5_overview_dashboard_01",
            "dashboard_checks": {
                "dashboard_id": "d_prior",
                "widget_ids": ["w1", "w2", "w3"],
            },
        }
    ]

    def score(dashboard_id: str):
        return evaluate_case(
            case,
            {
                "results": results,
                "plan": [SimpleNamespace(tool="run_sql")],
                "answer_blocks": [
                    Block("text", "Дашборд обновлён."),
                    Block("dashboard_link", dashboard_id=dashboard_id),
                ],
                "dashboard_snapshot": snapshot,
            },
            context_results=context,
        )

    status, issues, _ = score("d_prior")
    assert status == "passed", issues

    status, issues, _ = score("d_other")
    assert status == "failed"
    assert any("does not reuse prerequisite dashboard" in issue for issue in issues)


def test_refreshed_dashboard_facts_and_tools_come_from_replayed_recipes():
    case = answer_case(
        "s6_refresh_previous_dashboard_01",
        blocks_include=["dashboard_link"],
        facts=[{"key": "total", "comparison": "exact", "value": 42}],
        reuse_dashboard_from="s5_overview_dashboard_01",
        dashboard={
            "persisted": True,
            "refresh": "recompute_recipes",
            "widgets": [
                {"id": "a", "facts": ["total"], "chart_types_any": ["kpi"]},
                {"id": "b", "chart_types_any": ["bar"]},
                {"id": "c", "chart_types_any": ["pie"]},
            ],
        },
    )
    widgets = [
        _dashboard_widget("w1", "kpi", "r1"),
        _dashboard_widget("w2", "bar", "r2"),
        _dashboard_widget("w3", "pie", "r3"),
    ]
    snapshot = DashboardSnapshot(
        dashboard_id="d_prior",
        dataset_id="hk_background_jobs_v1",
        title="Обзор фоновых заданий",
        widgets=widgets,
        created_at=datetime.now(UTC),
        refreshed_at=datetime.now(UTC),
    )

    class RecomputeDataset:
        """The graph created new results; the runner replays the widget recipes."""

        def recompute_widget(self, widget):
            return tool_result(rows=[[42]], result_id=f"new_{widget.widget_id}")

    context = [
        {
            "id": "s5_overview_dashboard_01",
            "dashboard_checks": {
                "dashboard_id": "d_prior",
                "widget_ids": ["w1", "w2", "w3"],
            },
        }
    ]
    status, issues, details = evaluate_case(
        case,
        {
            "results": [],
            "plan": [],
            "answer_blocks": [
                Block("text", "Дашборд обновлён: всего 42 попытки."),
                Block("dashboard_link", dashboard_id="d_prior"),
            ],
            "dashboard_snapshot": snapshot,
        },
        dataset=RecomputeDataset(),
        context_results=context,
    )

    assert status == "passed", issues
    assert details["fact_checks"][0]["source"] == "dashboard:d_prior"
    assert details["dashboard_checks"]["reuse_accepted"] is True
    assert details["dashboard_checks"]["recomputed_tools"] == ["run_sql"]


@pytest.mark.parametrize("dashboard_id", ["d_prior", "d_unrelated"])
def test_real_golden_dashboard_refresh_contract(dashboard_id):
    cases, _ = load_suite()
    case = next(case for case in cases if case["id"] == "s6_refresh_previous_dashboard_01")
    expected = case["expect"]
    facts = {fact["key"]: fact for fact in expected["facts"]}
    widgets = [
        _dashboard_widget(widget["id"], widget["chart_types_any"][0], f"r_{index}")
        for index, widget in enumerate(expected["dashboard"]["widgets"])
    ]
    widget_facts = {
        widget["id"]: facts[widget["facts"][0]]["value"]
        for widget in expected["dashboard"]["widgets"]
    }

    class RecomputeDataset:
        def recompute_widget(self, widget):
            value = widget_facts[widget.widget_id]
            return tool_result(
                rows=value if isinstance(value, list) else [[value]],
                result_id=f"new_{widget.widget_id}",
            )

    snapshot = DashboardSnapshot(
        dashboard_id=dashboard_id,
        dataset_id=case["dataset"],
        title="Обзор фоновых заданий",
        widgets=widgets,
        created_at=datetime.now(UTC),
        refreshed_at=datetime.now(UTC),
    )
    status, issues, details = evaluate_case(
        case,
        {
            "results": [],
            "plan": [],
            "answer_blocks": [
                Block("text", "Панель обновлена."),
                Block("dashboard_link", dashboard_id=dashboard_id),
            ],
            "dashboard_snapshot": snapshot,
        },
        dataset=RecomputeDataset(),
        context_results=[
            {
                "id": expected["reuse_dashboard_from"],
                "dashboard_checks": {
                    "dashboard_id": "d_prior",
                    "widget_ids": [widget.widget_id for widget in widgets],
                },
            }
        ],
    )
    assert status == ("needs_review" if dashboard_id == "d_prior" else "failed"), issues
    assert details["automated_status"] == ("passed" if dashboard_id == "d_prior" else "failed")
    assert details["dashboard_checks"]["reuse_accepted"] is True
    if dashboard_id != "d_prior":
        assert any("does not reuse prerequisite" in issue for issue in issues)


def test_open_dataset_uses_ingested_table_not_mock_database(tmp_path):
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("name;value\nalpha;1\nbeta;2\n", encoding="utf-8")
    dataset = open_dataset(
        csv_path, {"dataset_id": "ds_eval", "source": {"file_name": "sample.csv"}}
    )
    try:
        table = dataset.catalog.tables[0]
        assert table.row_count == 2
        result = dataset.dispatch(
            "run_sql", {"sql": f'SELECT count(*) AS n FROM "{table.table_id}"'}
        )
        assert result.status == "ok"
        assert result.data.rows == [[2]]
        assert dataset.load_catalog("ds_eval") is dataset.catalog
        with pytest.raises(ValueError, match="unknown dataset"):
            dataset.load_catalog("other")
    finally:
        dataset.close()
