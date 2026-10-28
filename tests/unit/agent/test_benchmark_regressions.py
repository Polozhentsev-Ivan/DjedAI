"""Independent regressions for benchmark scoring and reasoning configuration."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

CHECKOUT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(CHECKOUT / "src"))
spec = importlib.util.spec_from_file_location(
    "review194_benchmark", CHECKOUT / "scripts/benchmark_model_profiles.py"
)
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)

from djedai.agent.client import BIModelClient, ModelResponse  # noqa: E402
from djedai.agent.diagnostics import RunDiagnostics  # noqa: E402
from djedai.contracts.agent import ComposeResponse, Understanding, VisualizeResponse  # noqa: E402


def case(name):
    return next(item for item in benchmark.CASES if item.name == name)


@pytest.mark.parametrize(
    "name,text",
    [
        ("compose_currency", "RUB: 5, USD: 70"),
        ("compose_count", "Всего 4 строки. Выручка 999 рублей."),
        ("compose_missing", ""),
    ],
)
def test_semantic_checker_rejects_wrong_currency_extra_claim_and_empty_answer(name, text):
    answer = ComposeResponse.model_validate({"answer_blocks": [{"type": "text", "markdown": text}]})
    assert benchmark.semantic_ok(answer, case(name)) is False


@pytest.mark.parametrize("amount", ["999руб.", "999USD", "999кг", "999,50руб.", "4руб."])
def test_count_rejects_numeric_claims_with_an_attached_unit(amount):
    assert not benchmark.text_ok(f"Всего 4 строки. Выручка {amount}", case("compose_count"))


@pytest.mark.parametrize(
    "name,text", [("compose_count", "Всего 4строки."), ("compose_percentage", "50процентов")]
)
def test_supported_scalar_answer_can_use_an_attached_unit(name, text):
    assert benchmark.text_ok(text, case(name))


def test_stage_chart_must_reference_existing_result_and_columns():
    answer = VisualizeResponse.model_validate(
        {
            "charts": [
                {
                    "chart_id": "bad",
                    "type": "bar",
                    "title": "Broken",
                    "result_id": "r_nonexistent",
                    "x": {"field": "missing_x"},
                    "y": [{"field": "missing_y"}],
                }
            ]
        }
    )
    assert benchmark.semantic_ok(answer, case("visualize_region")) is False


def test_chart_values_without_requested_sort_are_compared_as_labeled_rows():
    from djedai.contracts.tool_result import ToolResult, ToolResultData

    chart_case = next(item for item in benchmark.TURN_CASES if item.name == "turn_chart")
    result = ToolResult(
        result_id="r_chart",
        status="ok",
        data=ToolResultData(columns=["region", "total"], rows=[["C", 40], ["A", 30], ["B", 5]]),
    )
    assert benchmark._results_match([result], chart_case) is True


def test_requested_chart_does_not_require_repeating_every_value_in_prose():
    helper_spec = importlib.util.spec_from_file_location(
        "review194_fake_helper", CHECKOUT / "tests/unit/agent/test_model_benchmark.py"
    )
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)

    class ChartOnlyClient(helper.FakeClient):
        def _answer(self, system):
            return "График готов."

    chart_case = next(item for item in benchmark.TURN_CASES if item.name == "turn_chart")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        ChartOnlyClient(),
        chart_case,
        repeat=0,
        profile_name="review",
    )
    assert turn["schema_valid"] is True
    assert turn["sql_ok"] is True
    assert turn["charts"] == 1
    assert turn["semantic_ok"] is True


def test_chart_removed_by_verify_is_not_a_successful_published_chart():
    class StrippedChartClient(_fake_helper().FakeClient):
        def _answer(self, system):
            return "График готов."

        def _verify(self, system):
            return {"is_valid": False, "corrected_markdown": "График готов."}

    chart_case = next(item for item in benchmark.TURN_CASES if item.name == "turn_chart")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        StrippedChartClient(),
        chart_case,
        repeat=0,
        profile_name="stripped-chart",
    )
    assert turn["schema_valid"] and turn["sql_ok"] and turn["trace"]["is_verified"]
    assert len(turn["trace"]["charts"]) == 1
    assert all(block["type"] != "chart" for block in turn["trace"]["answer_blocks"])
    assert turn["charts"] == 0
    assert turn["semantic_ok"] is False


@pytest.mark.parametrize("limit,valid", [(1, False), (2, False), (3, True), (20, True)])
@pytest.mark.parametrize("sort", [None, {"field": "total", "order": "desc"}])
def test_graph_chart_requires_all_requested_categories_after_display_limit(limit, valid, sort):
    class LimitedChartClient(_fake_helper().FakeClient):
        def generate_typed_response(self, system, question, response_model, **kwargs):
            response = super().generate_typed_response(system, question, response_model, **kwargs)
            if response_model is VisualizeResponse:
                response.data = VisualizeResponse.model_validate(
                    {
                        "charts": [
                            {**chart.model_dump(), "limit": limit, "sort": sort}
                            for chart in response.data.charts
                        ]
                    }
                )
            return response

    chart_case = next(item for item in benchmark.TURN_CASES if item.name == "turn_chart")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        LimitedChartClient(),
        chart_case,
        repeat=0,
        profile_name="limited-chart",
    )
    assert turn["schema_valid"] and turn["sql_ok"] and turn["charts"] == 1
    assert turn["semantic_ok"] is valid


@pytest.mark.parametrize("kind,valid", [("category", True), ("number", False)])
def test_stage_chart_rejects_numeric_x_axis_with_text_categories(kind, valid):
    response = VisualizeResponse.model_validate(
        {
            "charts": [
                {
                    "chart_id": "region-chart",
                    "type": "bar",
                    "title": "Регионы",
                    "result_id": "r_benchmark",
                    "x": {"field": "region", "kind": kind},
                    "y": [{"field": "total"}],
                }
            ]
        }
    )
    assert benchmark.semantic_ok(response, case("visualize_region")) is valid


@pytest.mark.parametrize(
    "bad_chart",
    [
        {"result_id": "missing-result"},
        {"x": {"field": "missing-column"}},
        {"x": {"field": "region", "kind": "number"}},
        {"limit": 1},
        {"type": "unknown-chart-type"},
    ],
)
def test_valid_intermediate_chart_cannot_hide_an_invalid_published_chart(bad_chart):
    from djedai.contracts.agent import Block

    class InvalidPublishedChartClient(_fake_helper().FakeClient):
        def generate_typed_response(self, system, question, response_model, **kwargs):
            response = super().generate_typed_response(system, question, response_model, **kwargs)
            if response_model is ComposeResponse:
                # The saved visualize result stays valid; only this extra final
                # answer block is invalid. A final answer must validate both.
                chart = {
                    "chart_id": "untrusted-inline",
                    "type": "bar",
                    "title": "Регионы",
                    "result_id": self.result_id,
                    "x": {"field": "region"},
                    "y": [{"field": "total"}],
                    **bad_chart,
                }
                response.data.answer_blocks.append(Block(type="chart", chart=chart))
            elif response_model is VisualizeResponse:
                self.result_id = response.data.charts[0].result_id
            return response

    chart_case = next(item for item in benchmark.TURN_CASES if item.name == "turn_chart")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        InvalidPublishedChartClient(),
        chart_case,
        repeat=0,
        profile_name="invalid-published-chart",
    )
    assert turn["schema_valid"] and turn["sql_ok"] and turn["trace"]["is_verified"]
    assert len(turn["trace"]["charts"]) == 1
    assert sum(block["type"] == "chart" for block in turn["trace"]["answer_blocks"]) == 2
    assert turn["semantic_ok"] is False


def test_numeric_chart_axis_accepts_real_numeric_values():
    from djedai.contracts.chart import ChartSpec
    from djedai.contracts.tool_result import ToolResult

    source = ToolResult(
        result_id="numeric-source",
        status="ok",
        data={
            "columns": ["x", "total"],
            "rows": [[1, 30], [2, 40]],
        },
    )
    chart = ChartSpec.model_validate(
        {
            "chart_id": "numeric-axis",
            "type": "bar",
            "title": "Числовая ось",
            "result_id": source.result_id,
            "x": {"field": "x", "kind": "number"},
            "y": [{"field": "total"}],
        }
    )
    assert benchmark.charts_ok([chart], [source], ((1, 30), (2, 40)))


def test_candidate_must_not_hide_case_degradation_by_improving_another_case():
    def report(profile, states):
        rows = [
            {"case": name, "semantic_ok": ok, "schema_valid": True}
            for name, outcomes in states.items()
            for ok in outcomes
        ]
        return {
            "model": "offline",
            "profile": profile,
            "calls": rows,
            "turns": [],
            "semantic_ok": sum(row["semantic_ok"] for row in rows),
            "turn_latency_seconds": {"median": None},
        }

    baseline = report("baseline", {"count": [True, True, False], "chart": [False] * 3})
    candidate = report("proposed", {"count": [False] * 3, "chart": [True] * 3})
    comparison = benchmark.compare_profiles([baseline, candidate])["comparisons"][0]
    assert comparison["safe_to_enable"] is False


def test_stage_output_limit_keeps_known_usage_in_benchmark_record():
    client = BIModelClient(
        api_key="offline-only", base_url="http://127.0.0.1:1/v1", model_name="offline"
    )
    usage = SimpleNamespace(
        prompt_tokens=321, completion_tokens=2048, completion_tokens_details=None
    )
    response = SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="{}"))],
    )
    client._request = Mock(return_value=response)
    events = []
    diag = RunDiagnostics(
        events.append, owner_id="o", chat_id="c", dataset_id="d", user_message_id="u"
    )
    try:
        with diag.stage("understand"):
            report = benchmark.run_benchmark(
                client, repeats=1, context_lines=0, cases=(case("understand_count"),)
            )
        finished = next(e for e in events if e["event"] == "model_call_finished")
        assert finished["output_tokens"] == 2048
        row = report["calls"][0]
        assert row["error_type"] == "OUTPUT_LIMIT"
        assert (row["prompt_tokens"], row["completion_tokens"]) == (321, 2048)
    finally:
        client.client.close()


def test_understanding_count_must_resolve_the_existing_table():
    answer = Understanding.model_validate(
        {
            "intent": "data_question",
            "resolved_question": "Count rows",
            "entities": {"tables": ["nonexistent"]},
        }
    )
    assert benchmark.semantic_ok(answer, case("understand_count")) is False


def test_backend_reports_the_actual_reasoning_configuration_error():
    import os

    from fastapi import HTTPException

    from djedai.backend.chat_service import build_agent_runtime
    from djedai.config import LimitSettings

    limits = LimitSettings(
        _env_file=None,
        MODEL_REASONING_UNDERSTAND="none",
        MODEL_REASONING_SUPPORTED=False,
    )
    with (
        patch.dict(
            os.environ,
            {
                "MODEL_BASE_URL": "http://127.0.0.1:1/v1",
                "MODEL_NAME": "offline",
                "MODEL_API_KEY": "offline-only",
            },
        ),
        patch("djedai.agent.config.load_dotenv"),
        patch("djedai.agent.config.get_limits", return_value=limits),
        pytest.raises(HTTPException) as failure,
    ):
        build_agent_runtime()
    assert failure.value.status_code == 503
    assert "MODEL_REASONING" in str(failure.value.detail)


@pytest.mark.parametrize("text", ["RUB: 70, USD: 5", "70 RUB; 5 USD", "USD: 5.00\nRUB: 70.0"])
def test_labeled_currency_expectations_accept_valid_presentations(text):
    assert benchmark.text_ok(text, case("compose_currency")) is True


@pytest.mark.parametrize(
    "text", ["RUB: 70, USD: 5, итог 70", "RUB: 70, USD: 5, USD: 70", "RUB: 70, USD: −5"]
)
def test_labeled_currency_expectations_reject_unbound_or_conflicting_claims(text):
    assert benchmark.text_ok(text, case("compose_currency")) is False


def test_unordered_rows_keep_multiplicity_and_ordered_cases_stay_strict():
    expected = (("A", 30), ("B", 5), ("C", 40))
    assert benchmark.rows_match([["C", 40], ["A", 30], ["B", 5]], expected, ordered=False)
    assert not benchmark.rows_match([["A", 30], ["A", 30], ["B", 5]], expected, ordered=False)
    assert not benchmark.rows_match([["C", 40], ["A", 30], ["B", 5]], expected, ordered=True)


def test_missing_usage_is_explicitly_a_partial_total():
    rows = [{"in": 10, "out": 4}, {"in": None, "out": None}]
    total = benchmark.usage_totals(rows, "in", "out")
    assert total["input_tokens"] is None and total["output_tokens"] is None
    assert total["known_input_tokens"] == 10 and total["known_output_tokens"] == 4
    assert total["complete"] is False


def test_another_claim_cannot_hide_behind_the_same_scalar_value():
    assert not benchmark.text_ok("Всего 4 строки. Выручка 4.", case("compose_count"))


def test_two_totally_failed_profiles_are_not_evidence_to_enable_a_candidate():
    reports = [
        {
            "model": "offline",
            "profile": profile,
            "calls": [{"case": "count", "semantic_ok": False, "schema_valid": False}],
            "turns": [],
            "semantic_ok": 0,
            "turn_latency_seconds": {"median": None},
        }
        for profile in ["baseline", "proposed"]
    ]
    comparison = benchmark.compare_profiles(reports)["comparisons"][0]
    assert comparison["safe_to_enable"] is False
    assert comparison["insufficient_evidence"] is True
    assert comparison["recommendation"] == "keep_baseline"


@pytest.mark.parametrize(
    "options,valid",
    [
        (["Валовые продажи", "Чистые продажи"], True),
        (["gross_sales", "net_sales"], True),
        (["gross_sales / net_sales", "Другое"], False),
        (["gross_sales", "gross_sales"], False),
    ],
)
def test_ambiguous_metric_choices_are_distinct_and_resolve_catalog_definitions(options, valid):
    answer = Understanding.model_validate(
        {
            "intent": "ambiguous",
            "clarification": {"question": "Какие продажи?", "options": options},
        }
    )
    assert benchmark.semantic_ok(answer, case("understand_ambiguous")) is valid


def _fake_helper():
    helper_spec = importlib.util.spec_from_file_location(
        "trace_fake", CHECKOUT / "tests/unit/agent/test_model_benchmark.py"
    )
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    return helper


def test_complete_turn_trace_contains_inputs_to_the_quality_verdict():
    count = next(item for item in benchmark.TURN_CASES if item.name == "turn_count")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        _fake_helper().FakeClient(),
        count,
        repeat=0,
        profile_name="trace",
    )
    assert turn["semantic_ok"]
    trace = turn["trace"]
    assert trace["understanding"]["entities"]["tables"] == ["orders"]
    assert trace["plan"][0]["args"]["sql"].startswith("SELECT COUNT")
    assert trace["results"][-1]["data"]["rows"] == [[4]]
    assert trace["answer_blocks"][0]["markdown"] == "4"


def test_ambiguous_turn_with_metric_candidates_uses_a_real_catalog():
    class CandidateClient(_fake_helper().FakeClient):
        def generate_typed_response(self, system, question, response_model, **kwargs):
            if response_model is Understanding:
                candidate = {
                    "metric_id": "gross_sales",
                    "title": "Валовые продажи",
                    "kind": "sum",
                    "grain": {"table": "sales", "key": "region"},
                    "measure": {"table": "sales", "column": "gross_sales"},
                    "additivity": "additive",
                    "unit": "number",
                }
                invalid = {
                    **candidate,
                    "metric_id": "net_sales",
                    "grain": {"table": "missing", "key": "region"},
                    "measure": {"table": "missing", "column": "net_sales"},
                }
                return ModelResponse(
                    status="success",
                    data=Understanding.model_validate(
                        {
                            "intent": "ambiguous",
                            "clarification": {
                                "question": "Какие продажи?",
                                "options": ["gross_sales", "net_sales"],
                                "metric_candidates": [candidate, invalid],
                            },
                        }
                    ),
                )
            return super().generate_typed_response(system, question, response_model, **kwargs)

    ambiguous = next(item for item in benchmark.TURN_CASES if item.name == "turn_ambiguous")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        CandidateClient(),
        ambiguous,
        repeat=0,
        profile_name="candidate-dto",
    )
    assert turn["error_type"] is None and turn["semantic_ok"] is True
    candidates = turn["trace"]["answer_blocks"][0]["metric_candidates"]
    assert candidates[0]["status"] == "proposed"
    assert candidates[1] is None
    with benchmark.make_connection() as connection:
        catalog = benchmark.make_catalog(connection)
    assert catalog.to_prompt() == benchmark.CATALOG
    assert catalog.table("sales").column("region").is_unique


def test_turn_deadline_applies_across_stages_and_preserves_the_last_checkpoint():
    from djedai.contracts.agent import PlanResponse
    from djedai.execution import current_budget

    class SlowClient(_fake_helper().FakeClient):
        def generate_typed_response(self, system, question, response_model, **kwargs):
            if response_model is PlanResponse:
                budget = current_budget()
                assert budget is not None
                budget.wait(budget.remaining + 0.1)
            return super().generate_typed_response(system, question, response_model, **kwargs)

    count = next(item for item in benchmark.TURN_CASES if item.name == "turn_count")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        SlowClient(),
        count,
        repeat=0,
        profile_name="deadline",
        turn_timeout_seconds=0.3,
    )
    assert turn["error_type"] == "TIMEOUT"
    assert not turn["schema_valid"] and not turn["semantic_ok"]
    assert turn["trace"]["understanding"]["intent"] == "data_question"


def test_a_chart_in_an_interrupted_checkpoint_is_not_a_published_success():
    from djedai.contracts.agent import VerifyResponse
    from djedai.execution import current_budget

    class SlowVerify(_fake_helper().FakeClient):
        def generate_typed_response(self, system, question, response_model, **kwargs):
            if response_model is VerifyResponse:
                budget = current_budget()
                budget.wait(budget.remaining + 0.1)
            return super().generate_typed_response(system, question, response_model, **kwargs)

    chart = next(item for item in benchmark.TURN_CASES if item.name == "turn_chart")
    turn = benchmark.run_turn(
        benchmark.build_agent_graph(),
        SlowVerify(),
        chart,
        repeat=0,
        profile_name="late-deadline",
        turn_timeout_seconds=0.5,
    )
    assert turn["error_type"] == "TIMEOUT"
    assert turn["charts"] == 1
    assert turn["semantic_ok"] is False


@pytest.mark.parametrize(
    "finish,content,error",
    [
        ("length", "{", "OUTPUT_LIMIT"),
        ("stop", "broken-json", "INVALID_JSON"),
        ("stop", '{"intent":"not-an-intent"}', "SCHEMA_MISMATCH"),
    ],
)
def test_received_but_invalid_responses_keep_usage_and_finish_reason(finish, content, error):
    client = BIModelClient(
        api_key="offline", base_url="http://127.0.0.1:1/v1", model_name="offline"
    )
    client._request = Mock(
        return_value=SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=15, completion_tokens=30),
            choices=[
                SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))
            ],
        )
    )
    try:
        response = client.generate_typed_response(
            "synthetic", "question", Understanding, stage="understand"
        )
        assert response.error_type == error
        assert (response.prompt_tokens, response.completion_tokens, response.finish_reason) == (
            15,
            30,
            finish,
        )
        assert response.raw_response is not None
    finally:
        client.client.close()


def test_provider_rejection_keeps_safe_http_status_without_a_default_retry():
    import httpx
    from openai import BadRequestError

    client = BIModelClient(
        api_key="offline", base_url="http://127.0.0.1:1/v1", model_name="offline"
    )
    client._request = Mock(
        side_effect=BadRequestError(
            "unsupported",
            response=httpx.Response(400, request=httpx.Request("POST", "http://127.0.0.1:1")),
            body={},
        )
    )
    try:
        response = client.generate_typed_response(
            "synthetic", "question", Understanding, stage="understand"
        )
        assert response.error_type == "SERVER_ERROR"
        assert response.http_status == 400
        assert client._request.call_count == 1
    finally:
        client.client.close()


@pytest.mark.parametrize("interrupt,expected", [(False, "completed"), (True, "interrupted")])
def test_cli_checkpoints_results_and_fingerprints_without_recording_credentials(
    tmp_path, monkeypatch, interrupt, expected
):
    import json

    class Client(_fake_helper().FakeClient):
        def __init__(self, **kwargs):
            super().__init__()
            self.reasoning = kwargs["reasoning"]
            self.client = SimpleNamespace(close=lambda: None)

        def generate_typed_response(self, *args, **kwargs):
            if interrupt and self.calls == 1:
                raise KeyboardInterrupt
            return super().generate_typed_response(*args, **kwargs)

    monkeypatch.setattr(benchmark, "BIModelClient", Client)
    monkeypatch.setenv("MODEL_API_KEY", "private-only-regression")
    output = tmp_path / "report.json"
    result = benchmark.main(
        [
            "--model",
            "offline",
            "--env-file",
            str(tmp_path / "none.env"),
            "--repeats",
            "1",
            "--skip-turns",
            "--context-lines",
            "0",
            "--output",
            str(output),
        ]
    )
    assert result == (130 if interrupt else 0)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == expected
    assert len(report["source"]["fixture_sha256"]) == 64
    assert report["source"]["file_sha256"]["prompts/understand.md"]
    assert report["source"]["application_sha"]
    assert "private-only-regression" not in output.read_text(encoding="utf-8")
    assert not output.with_name(output.name + ".tmp").exists()
    assert len(report["models"][0]["calls"]) == (1 if interrupt else 15)
    if interrupt:
        assert report["comparison"] == {}
