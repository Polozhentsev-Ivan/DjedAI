import importlib.util
from pathlib import Path

import pytest

from djedai.agent.client import ModelResponse
from djedai.contracts.agent import ComposeResponse, PlanResponse, Understanding

path = Path(__file__).resolve().parents[3] / "scripts/benchmark_model_profiles.py"
spec = importlib.util.spec_from_file_location("benchmark_model_profiles", path)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class FakeClient:
    model = "offline-test-model"

    def __init__(self, *, fail_first=False, wrong_count=False, bad_plan=None):
        self.calls = 0
        self.fail_first = fail_first
        self.wrong_count = wrong_count
        self.bad_plan = bad_plan

    def generate_typed_response(self, system, question, response_model):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            return ModelResponse(status="error", error_type="INVALID_JSON")
        if response_model is Understanding:
            data = {"intent": "data_question"}
            if "gross_sales" in system:
                data = {
                    "intent": "ambiguous",
                    "clarification": {
                        "question": "Какая метрика?",
                        "options": ["gross_sales", "net_sales"],
                    },
                }
        elif response_model is PlanResponse:
            sql = (
                "SELECT region, SUM(amount) FROM orders GROUP BY region ORDER BY region"
                if "Сумма" in question
                else "SELECT COUNT(*) FROM orders"
            )
            data = {"plan": [{"tool": "run_sql", "args": {"sql": sql}}]}
            if self.bad_plan is not None:
                data = {"plan": [self.bad_plan]}
        else:
            assert response_model is ComposeResponse
            text = "Данных нет" if "успешных" in system else "4" if self.wrong_count else "3"
            data = {"answer_blocks": [{"type": "text", "markdown": text}]}
        return ModelResponse(
            status="success",
            data=response_model.model_validate(data),
            prompt_tokens=123,
            completion_tokens=15,
            finish_reason="stop",
        )


@pytest.mark.parametrize(
    "fail_first,wrong_count,valid,correct",
    [
        (False, False, 6, 6),
        (True, False, 5, 5),
        (False, True, 6, 5),
    ],
)
def test_benchmark_distinguishes_schema_validity_from_correct_answers(
    fail_first, wrong_count, valid, correct
):
    report = benchmark.run_benchmark(
        FakeClient(fail_first=fail_first, wrong_count=wrong_count), repeats=1, context_lines=0
    )
    assert report["requests"] == 6
    assert report["schema_valid"] == valid
    assert report["semantic_ok"] == correct
    assert report["max_prompt_tokens"] == 123


@pytest.mark.parametrize("plan", [{"tool": "sql", "args": {}}, {"tool": "run_sql", "args": {}}])
def test_benchmark_continues_after_schema_valid_but_unexecutable_plan(plan):
    report = benchmark.run_benchmark(FakeClient(bad_plan=plan), repeats=1, context_lines=0)
    assert report["requests"] == report["schema_valid"] == 6
    assert report["semantic_ok"] == 4
