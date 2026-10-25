import importlib.util
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from djedai.agent.client import ModelResponse
from djedai.contracts.agent import (
    ComposeResponse,
    PlanResponse,
    Understanding,
    VerifyResponse,
    VisualizeResponse,
)
from djedai.contracts.chart import ChartSpec, XAxis, YMeasure
from djedai.contracts.tool_result import ToolResult, ToolResultData

path = Path(__file__).resolve().parents[3] / "scripts/benchmark_model_profiles.py"
spec = importlib.util.spec_from_file_location("benchmark_model_profiles", path)
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


class FakeClient:
    model = "offline-test-model"

    def __init__(self, *, fail_first=False, wrong_count=False, bad_plan=None, wrong_verify=False):
        self.calls = 0
        self.fail_first = fail_first
        self.wrong_count = wrong_count
        self.bad_plan = bad_plan
        self.wrong_verify = wrong_verify
        self.reasoning = {stage: "provider_default" for stage in benchmark.REASONING_STAGES}
        self.stages = []

    def reasoning_for(self, stage):
        return self.reasoning.get(stage, "provider_default")

    def generate_typed_response(self, system, question, response_model, *, stage=None, **kwargs):
        self.calls += 1
        self.stages.append(stage)
        if self.fail_first and self.calls == 1:
            return ModelResponse(status="error", error_type="INVALID_JSON")
        if response_model is Understanding:
            if "продажи" in question:
                data = {
                    "intent": "ambiguous",
                    "clarification": {
                        "question": "Какая метрика?",
                        "options": ["gross_sales", "net_sales"],
                    },
                }
            else:
                data = {"intent": "data_question", "entities": {"tables": ["orders"]}}
        elif response_model is PlanResponse:
            data = {"plan": [{"tool": "run_sql", "args": {"sql": self._sql(question)}}]}
            if self.bad_plan is not None:
                data = {"plan": [self.bad_plan]}
        elif response_model is ComposeResponse:
            data = {"answer_blocks": [{"type": "text", "markdown": self._answer(system)}]}
        elif response_model is VerifyResponse:
            data = self._verify(system)
        else:
            assert response_model is VisualizeResponse
            result_id = re.search(r'"result_id": "([^"]+)"', system)
            data = {
                "charts": [
                    ChartSpec(
                        chart_id="ch_benchmark",
                        type="bar",
                        title="Сумма по region",
                        result_id=result_id.group(1) if result_id else "r_benchmark",
                        x=XAxis(field="region"),
                        y=[YMeasure(field="total")],
                    ).model_dump(mode="json")
                ]
            }
        return ModelResponse(
            status="success",
            data=response_model.model_validate(data),
            prompt_tokens=123,
            completion_tokens=15,
            finish_reason="stop",
        )

    def _sql(self, question):
        lowered = question.lower()
        if "count(*)" in lowered or "сколько" in lowered:
            return "SELECT COUNT(*) AS n FROM orders"
        if "refunds" in question:
            return "SELECT SUM(amount) AS total FROM refunds"
        if "currency" in question:
            return (
                "SELECT currency, SUM(amount) AS total FROM orders "
                "GROUP BY currency ORDER BY currency"
            )
        if "discount_pct" in question:
            return (
                "SELECT 100.0 * COUNT(*) FILTER (WHERE discount_pct > 0) / COUNT(*) AS pct "
                "FROM orders"
            )
        if "segment" in question:
            return (
                "SELECT c.segment, SUM(o.amount) AS total FROM orders o "
                "JOIN customers c ON c.customer_id = o.customer_id "
                "WHERE o.amount > 5 GROUP BY c.segment ORDER BY c.segment"
            )
        return "SELECT region, SUM(amount) AS total FROM orders GROUP BY region ORDER BY region"

    def _answer(self, system):
        facts = system.split("Facts and Data:", 1)[-1]
        if "[[None]]" in facts or "Нет успешных результатов" in facts:
            return "Данных нет"
        if "RUB" in facts:
            return "RUB 70, USD 5"
        if "'pct'" in facts:
            return "50%"
        if "retail" in facts:
            return "retail 50, wholesale 20"
        if "'region'" in facts:
            return "A 30, B 5, C 40"
        return "5" if self.wrong_count else "4"

    def _verify(self, system):
        if "Всего 5 строк" in system:
            if self.wrong_verify:
                return {"is_valid": True, "corrected_markdown": ""}
            return {"is_valid": False, "corrected_markdown": "Всего 4 строки."}
        return {"is_valid": True, "corrected_markdown": ""}


def test_benchmark_covers_required_stage_scenarios():
    report = benchmark.run_benchmark(FakeClient(), repeats=1, context_lines=0, turn_repeats=0)
    assert report["requests"] == len(benchmark.CASES) == 15
    assert report["schema_valid"] == report["semantic_ok"] == 15
    assert report["max_prompt_tokens"] == 123
    assert report["stage_latency_seconds"]["plan"]["median"] >= 0
    assert all(call["reasoning_setting"] == "provider_default" for call in report["calls"])
    assert all(call["reasoning_effort"] is None for call in report["calls"])
    assert {call["stage"] for call in report["calls"]} == {
        "understand",
        "plan",
        "compose",
        "verify",
        "visualize",
    }


def test_benchmark_context_probe_is_separate_from_repeats():
    report = benchmark.run_benchmark(FakeClient(), repeats=2, context_lines=3, turn_repeats=0)
    assert report["requests"] == len(benchmark.CASES) * 2 + 1
    assert [call["case"] for call in report["calls"]].count("context_probe") == 1


@pytest.mark.parametrize(
    "kwargs,expected_schema,expected_semantic",
    [
        ({"fail_first": True}, 14, 14),
        ({"wrong_count": True}, 15, 14),
        ({"wrong_verify": True}, 15, 14),
        ({"bad_plan": {"tool": "sql", "args": {}}}, 15, 9),
    ],
)
def test_benchmark_distinguishes_schema_validity_from_correct_answers(
    kwargs, expected_schema, expected_semantic
):
    report = benchmark.run_benchmark(FakeClient(**kwargs), repeats=1, context_lines=0)
    assert report["schema_valid"] == expected_schema
    assert report["semantic_ok"] == expected_semantic


def test_benchmark_continues_after_schema_valid_but_unexecutable_plan():
    report = benchmark.run_benchmark(
        FakeClient(bad_plan={"tool": "run_sql", "args": {}}),
        repeats=1,
        context_lines=0,
    )
    assert report["requests"] == report["schema_valid"] == 15
    assert report["semantic_ok"] == 9


def test_benchmark_runs_a_full_turn_with_stage_and_total_timing():
    report = benchmark.run_benchmark(FakeClient(), repeats=1, context_lines=0, turn_repeats=1)
    assert len(report["turns"]) == len(benchmark.TURN_CASES) == 8
    assert all(turn["schema_valid"] and turn["semantic_ok"] for turn in report["turns"])
    chart = next(turn for turn in report["turns"] if turn["case"] == "turn_chart")
    assert chart["charts"] == 1
    assert {"understand", "plan", "execute", "visualize", "compose", "verify"} <= set(
        chart["stage_seconds"]
    )
    assert chart["total_seconds"] >= max(chart["stage_seconds"].values())
    assert chart["calls"][0]["reasoning_setting"] == "provider_default"
    assert chart["calls"][0]["reasoning_effort"] is None


def test_benchmark_marks_candidate_regressions_and_keeps_baseline():
    baseline = benchmark.run_benchmark(FakeClient(), repeats=1, context_lines=0)
    candidate = benchmark.run_benchmark(FakeClient(wrong_count=True), repeats=1, context_lines=0)
    comparison = benchmark.compare_profiles([baseline, candidate])["comparisons"][0]
    assert "compose_count" in comparison["regressed_cases"]
    assert comparison["safe_to_enable"] is False
    assert comparison["recommendation"] == "keep_baseline"


def test_benchmark_treats_initial_errors_separately_from_regressions():
    baseline = benchmark.run_benchmark(FakeClient(wrong_count=True), repeats=1, context_lines=0)
    candidate = benchmark.run_benchmark(FakeClient(wrong_count=True), repeats=1, context_lines=0)
    comparison = benchmark.compare_profiles([baseline, candidate])["comparisons"][0]
    assert comparison["regressed_cases"] == []
    assert "compose_count" in comparison["baseline_initial_failures"]
    assert comparison["safe_to_enable"] is True


def test_benchmark_uses_the_final_tool_result_not_an_intermediate_one():
    def result(value):
        return ToolResult(
            result_id=f"r_{value}",
            status="ok",
            data=ToolResultData(columns=["n"], rows=[[value]]),
        )

    case = benchmark.TURN_CASES[0]
    assert benchmark._results_match([result(4), result(5)], case) is False
    assert benchmark._results_match([result(5), result(4)], case) is True


def test_benchmark_compares_numeric_text_with_independent_expectations():
    assert benchmark.rows_match(
        [["RUB", Decimal("70.00")], ["USD", Decimal("5")]],
        (("RUB", Decimal("70")), ("USD", Decimal("5"))),
    )
    assert not benchmark.rows_match(
        [["RUB", Decimal("70.00")]], (("RUB", Decimal("70")), ("USD", Decimal("5")))
    )
    assert benchmark.numbers_in_text("RUB 70, USD 5 и 50%") == {
        Decimal("70"),
        Decimal("5"),
        Decimal("50"),
    }
    assert benchmark.numbers_in_text("Данных нет") == set()
    assert benchmark.numbers_in_text("Ответ: 4.") == {Decimal("4")}
    assert benchmark.numbers_in_text("Итого 50%.") == {Decimal("50")}


def test_turn_tokens_do_not_turn_missing_usage_into_zero():
    complete = [
        {
            "event": "model_call_finished",
            "input_tokens": 10,
            "output_tokens": 5,
            "reasoning_tokens": 2,
        },
        {
            "event": "model_call_finished",
            "input_tokens": 20,
            "output_tokens": 7,
            "reasoning_tokens": 3,
        },
    ]
    assert benchmark._turn_tokens(complete) == (30, 12, 5)

    incomplete = [*complete, {**complete[0], "reasoning_tokens": None}]
    assert benchmark._turn_tokens(incomplete) == (40, 17, None)


def test_benchmark_rejects_unknown_reasoning_override():
    parser = benchmark.argparse.ArgumentParser()
    with pytest.raises(SystemExit):
        benchmark.parse_reasoning_overrides(["unknown=none"], parser)
    with pytest.raises(SystemExit):
        benchmark.parse_reasoning_overrides(["understand=turbo"], parser)
