"""Golden evaluator with the production graph, CSV ingestion and analytical tools."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.contracts.agent import Block

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval_runner import evaluate_case, open_dataset, process_case  # noqa: E402

pytestmark = pytest.mark.integration


class Model:
    """Only inference is controlled; schemas, graph and tools are production code."""

    def __init__(self, tool, args, answer, fail_verify=False):
        self.tool, self.args, self.answer = tool, args, answer
        self.fail_verify = fail_verify
        self.calls = 0

    def generate_typed_response(self, system_prompt, user_prompt, schema, **kwargs):
        self.calls += 1
        if self.fail_verify and schema.__name__ == "VerifyResponse":
            return ModelResponse(status="error", error_type="TIMEOUT", message="controlled timeout")
        data = {
            "Understanding": {"intent": "data_question"},
            "PlanResponse": {"plan": [{"tool": self.tool, "args": self.args}]},
            "ComposeResponse": {"answer_blocks": [{"type": "text", "markdown": self.answer}]},
            "VerifyResponse": {"is_valid": True, "corrected_markdown": self.answer},
        }[schema.__name__]
        return ModelResponse(status="success", data=schema.model_validate(data))


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    path = tmp_path_factory.mktemp("eval") / "08_Пример.csv"
    path.write_text(
        "Код;Начало (МСК);Статус\n"
        "r1;2024-10-01 12:00:00;Ошибка\n"
        "r2;2024-10-02 12:00:00;Успешно\n"
        "r3;2024-11-01 12:00:00;Ошибка\n"
        "r4;2025-01-01 12:00:00;Успешно\n"
        "r5;2025-01-02 12:00:00;Успешно\n",
        encoding="utf-8-sig",
    )
    value = open_dataset(path, {"dataset_id": "eval_live", "source": {"file_name": path.name}})
    try:
        yield value
    finally:
        value.close()


def run(dataset, model, facts, **expect):
    case = {
        "id": "real_tools",
        "question": "Покажи результат",
        "dataset": "eval_live",
        "expect": {
            "outcome": "answer",
            "tools_any": ["run_sql"],
            "facts": facts,
            "answer_contains_value": {"from_result": True},
            **expect,
        },
    }
    return process_case(case, {case["id"]: case}, model, build_agent_graph(), dataset)


def test_monthly_aggregation_is_accepted_and_wrong_count_is_rejected(dataset):
    table = dataset.catalog.tables[0].table_id
    fact = {
        "key": "monthly.counts",
        "comparison": "exact",
        "value": [["2024-10", 2], ["2024-11", 1], ["2025-01", 2]],
    }
    model = Model(
        "time_aggregate",
        {"table": table, "time_col": "nachalo_msk", "freq": "month"},
        "2024-10: 2; 2024-11: 1; 2025-01: 2",
    )
    report = run(dataset, model, [fact], tools_planned=["time_aggregate"])
    assert report["status"] == "passed", report["issues"]
    assert report["tool_results"][0]["tool"] == "time_aggregate"
    assert report["plan"][0]["args"]["freq"] == "month"
    assert any(event["event"] == "stage_finished" for event in report["diagnostics"])
    wrong = {**fact, "value": [["2024-10", 3], ["2024-11", 1], ["2025-01", 2]]}
    report = run(dataset, model, [wrong], tools_planned=["time_aggregate"])
    assert report["status"] == "failed"


def test_quarterly_columns_are_projected_from_real_sql(dataset):
    table = dataset.catalog.tables[0].table_id
    sql = (
        "SELECT concat(CAST(year(nachalo_msk) AS VARCHAR),'-Q',"
        " CAST(quarter(nachalo_msk) AS VARCHAR)) AS quarter, count(*) AS attempts,"
        f" count(*) FILTER(WHERE status='Ошибка') AS errors FROM \"{table}\""
        " GROUP BY quarter ORDER BY quarter"
    )
    facts = [
        {
            "key": "quarterly.counts",
            "comparison": "exact",
            "value": [["2024-Q4", 3], ["2025-Q1", 2]],
        },
        {
            "key": "quarterly.errors",
            "comparison": "exact",
            "value": [["2024-Q4", 2], ["2025-Q1", 0]],
        },
    ]
    report = run(
        dataset,
        Model(
            "run_sql", {"sql": sql}, "2024 Q4: 3 попытки, 2 ошибки; 2025 Q1: 2 попытки, 0 ошибок"
        ),
        facts,
    )
    assert report["status"] == "passed", report["issues"]
    assert report["tool_results"][0]["params"]["sql"] == sql
    assert all(fact["passed"] for fact in report["fact_checks"])


def test_column_aliases_and_single_table_relations_use_runtime_metadata(dataset):
    facts = [
        {
            "key": "dataset.columns",
            "comparison": "exact_unordered",
            "value": ["Код", "Начало (МСК)", "Статус"],
        },
        {"key": "dataset.relation_count", "comparison": "exact", "value": 0},
    ]
    report = run(
        dataset,
        Model(
            "describe_dataset", {}, "Три поля: Код, Начало (МСК), Статус. Межтабличных связей нет."
        ),
        facts,
        tools_any=["describe_dataset"],
    )
    assert report["status"] == "passed", report["issues"]


def test_real_empty_duplicate_result_confirms_zero(dataset):
    actual = dataset.dispatch("find_duplicates", {"table": dataset.catalog.tables[0].table_id})
    assert actual.status == "empty"
    case = {
        "expect": {
            "outcome": "answer",
            "tools_any": ["find_duplicates"],
            "facts": [{"key": "duplicates.full_rows", "comparison": "exact", "value": 0}],
            "answer_contains_value": {"from_result": True},
        }
    }
    status, issues, _ = evaluate_case(
        case,
        {
            "results": [actual],
            "plan": [],
            "answer_blocks": [Block(type="text", markdown="0 полных дублей")],
        },
    )
    assert status == "passed", issues


def test_failed_model_verification_is_not_hidden_by_correct_sql(dataset):
    table = dataset.catalog.tables[0].table_id
    fact = {"key": "dataset.row_count", "comparison": "exact", "value": 5}
    model = Model(
        "run_sql", {"sql": f'SELECT count(*) FROM "{table}"'}, "5 строк", fail_verify=True
    )
    report = run(dataset, model, [fact])
    assert report["status"] == "failed"
    assert any("Terminal error" in issue for issue in report["issues"])
    assert report["tool_results"][0]["data"]["rows"] == [[5]]
    assert report["terminal_error"] == "TIMEOUT"
    assert report["diagnostics"][-1]["status"] == "error"
    assert report["diagnostics"][-1]["error_type"] == "TIMEOUT"


def test_model_failure_in_real_graph_never_finishes_successfully(dataset):
    model = SimpleNamespace(
        generate_typed_response=lambda *a, **k: ModelResponse(
            status="error", error_type="SERVER_ERROR"
        )
    )
    report = run(dataset, model, [])
    assert report["status"] == "failed"
    assert report["terminal_error"] == "SERVER_ERROR"
    assert report["diagnostics"][-1]["status"] == "error"
    assert report["diagnostics"][-1]["error_type"] == "SERVER_ERROR"


def test_expired_turn_budget_prevents_any_inference(dataset, monkeypatch):
    monkeypatch.setattr(
        "scripts.eval_runner.get_limits", lambda: SimpleNamespace(agent_turn_timeout_seconds=0)
    )
    model = Model("describe_dataset", {}, "5 строк")
    report = run(dataset, model, [])
    assert report["status"] == "failed"
    assert report["error_type"] == "TurnTimeout"
    assert model.calls == 0


def test_percentage_is_derived_from_real_sql_counts_without_a_percentage_cell(tmp_path):
    source = tmp_path / "july.csv"
    source.write_text(
        "Код;Начало (МСК)\nr1;2025-07-01 12:00:00\nr2;2025-07-02 12:00:00\n"
        "r3;2026-07-01 12:00:00\n",
        encoding="utf-8-sig",
    )
    dataset = open_dataset(
        source, {"dataset_id": "eval_live", "source": {"file_name": source.name}}
    )
    try:
        table = dataset.catalog.tables[0].table_id
        sql = (
            "SELECT strftime(nachalo_msk, '%Y-%m') AS month, count(*) AS attempts "
            f'FROM "{table}" GROUP BY month ORDER BY month'
        )
        facts = [
            {
                "key": "july.counts",
                "comparison": "exact",
                "value": [["2025-07", 2], ["2026-07", 1]],
            },
            {
                "key": "july.change_percent",
                "comparison": "approx",
                "value": -50,
                "abs_tolerance": 0.00005,
            },
        ]
        for answer, expected in [
            ("Изменение: -50%", "passed"),
            ("Изменение: −50%", "passed"),
            ("Изменение: 50%", "failed"),
            ("Изменение: 2%", "failed"),
        ]:
            report = run(dataset, Model("run_sql", {"sql": sql}, answer), facts)
            assert report["status"] == expected, report["issues"]
            assert report["tool_results"][0]["data"]["rows"] == [["2025-07", 2], ["2026-07", 1]]
            assert report["fact_checks"][1]["derivation"]["value"] == "-50.0"
    finally:
        dataset.close()
