"""Агент и семантика метрик (B2-9): уточнение без определения, расчёт и цитирование версии.

Модель подменена, инструменты настоящие: execute_tool на фикстуре orders/items/payments.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph, route_after_execute
from djedai.analytics.metrics import MetricContext
from djedai.analytics.registry import execute_tool
from djedai.contracts.agent import (
    Block,
    Clarification,
    ComposeResponse,
    Message,
    PlannedCall,
    PlanResponse,
    Understanding,
    VerifyResponse,
)
from djedai.contracts.metrics import MetricDefinition, MetricSpec
from djedai.contracts.tool_result import ToolError, ToolResult

FIXTURES = Path(__file__).parents[2] / "fixtures" / "metrics"
ORDER_TOTAL = {
    "metric_id": "order_total",
    "title": "Сумма заказов",
    "kind": "sum",
    "grain": {"table": "orders", "key": "zakaz_id"},
    "measure": {"table": "orders", "column": "summa_itogo"},
    "additivity": "additive",
    "filters": [
        {"column": {"table": "orders", "column": "status"}, "op": "ne", "value": "cancelled"}
    ],
    "unit": "money",
    "currency": "RUB",
    "time_column": {"table": "orders", "column": "data_zakaza"},
}
CONFIRMED = MetricDefinition.model_validate(
    {
        **ORDER_TOTAL,
        "version": 3,
        "status": "confirmed",
        "confirmation": {"source": "api", "by_user_id": "u", "at": "2026-10-25T09:00:00Z"},
    }
)
NAIVE = "SELECT SUM(o.summa_itogo) FROM orders o JOIN order_items i ON i.zakaz_id = o.zakaz_id"


@pytest.fixture
def dataset(metrics_dataset):
    return metrics_dataset


def run(dataset, responses, metrics, question="Какая выручка за 2023 год?"):
    connection, catalog = dataset
    client = MagicMock()
    client.generate_typed_response.side_effect = responses
    semantics = MetricContext(catalog, metrics)
    tables = {t.table_id for t in catalog.tables}
    config = {
        "configurable": {
            "thread_id": f"metrics-{id(responses)}",
            "client": client,
            "catalog_loader": lambda _: catalog,
            "metrics_loader": lambda _: metrics,
            "tool_dispatcher": lambda tool, args: execute_tool(
                tool, args, connection=connection, allowed_tables=tables, semantics=semantics
            ),
        }
    }
    state = {
        "chat_id": "c",
        "dataset_id": catalog.dataset_id,
        "messages": [Message(role="user", content=question)],
    }
    return build_agent_graph().invoke(state, config=config), client


def ok(data):
    return ModelResponse(status="success", data=data)


def test_undefined_metric_is_clarified_with_checked_candidates(dataset):
    understanding = Understanding(
        intent="ambiguous",
        clarification=Clarification(
            question="Что считать выручкой?",
            options=["Сумма заказов без отмен", "Сумма по колонке amount", "Другое"],
            metric_candidates=[
                MetricSpec.model_validate(ORDER_TOTAL),
                MetricSpec.model_validate(
                    {**ORDER_TOTAL, "measure": {"table": "orders", "column": "amount"}}
                ),
            ],
        ),
    )
    result, client = run(dataset, [ok(understanding)], {})
    (block,) = result["answer_blocks"]
    assert block.type == "clarification"
    assert block.options[0] == "Сумма заказов без отмен"
    proposal, invalid = block.metric_candidates
    assert (proposal.metric_id, proposal.status, proposal.confirmation) == (
        "order_total",
        "proposed",
        None,
    )
    assert invalid is None  # колонки amount в датасете нет: вариант ничего не подтвердит
    prompt = client.generate_typed_response.call_args_list[0].args[0]
    assert "подтверждённых определений нет" in prompt


def test_confirmed_metric_is_computed_and_cited(dataset):
    responses = [
        ok(Understanding(intent="data_question", resolved_question="Сумма заказов за 2023")),
        ok(
            PlanResponse(
                plan=[
                    PlannedCall(
                        tool="compute_metric",
                        args={
                            "metric_id": "order_total",
                            "period_start": "2023-01-01",
                            "period_end": "2024-01-01",
                        },
                    )
                ]
            )
        ),
        ok(ComposeResponse(answer_blocks=[Block(type="text", markdown="12 000 RUB")])),
        ok(VerifyResponse(is_valid=True, corrected_markdown="12 000 RUB")),
    ]
    result, client = run(dataset, responses, {"order_total": CONFIRMED})
    (computed,) = result["results"]
    assert computed.status == "ok"
    assert computed.data.rows == [[pytest.approx(12000.0)]]
    assert computed.metric.version == 3
    answer, citation = result["answer_blocks"]
    assert answer.markdown == "12 000 RUB"
    assert citation.markdown.startswith("**Как посчитано.**")
    assert "«Сумма заказов» v3" in citation.markdown
    assert "[2023-01-01, 2024-01-01)" in citation.markdown and "RUB" in citation.markdown
    understand_prompt = client.generate_typed_response.call_args_list[0].args[0]
    assert "order_total v3 «Сумма заказов»" in understand_prompt


def test_undefined_metric_from_plan_goes_to_clarification_without_replanning(dataset):
    responses = [
        ok(Understanding(intent="data_question")),
        ok(PlanResponse(plan=[PlannedCall(tool="compute_metric", args={"metric_id": "revenue"})])),
    ]
    result, client = run(dataset, responses, {"order_total": CONFIRMED})
    (block,) = result["answer_blocks"]
    assert block.type == "clarification"
    assert "«revenue» не определена" in block.markdown
    assert client.generate_typed_response.call_count == 2


def test_fanout_sql_is_replanned_to_compute_metric(dataset):
    responses = [
        ok(Understanding(intent="data_question")),
        ok(PlanResponse(plan=[PlannedCall(tool="run_sql", args={"sql": NAIVE})])),
        ok(
            PlanResponse(
                plan=[PlannedCall(tool="compute_metric", args={"metric_id": "order_total"})]
            )
        ),
        ok(ComposeResponse(answer_blocks=[Block(type="text", markdown="12 800 RUB")])),
        ok(VerifyResponse(is_valid=True, corrected_markdown="12 800 RUB")),
    ]
    result, client = run(dataset, responses, {"order_total": CONFIRMED})
    refused, computed = result["results"]
    assert refused.error.code == "metric_grain_violation"
    assert computed.data.rows == [[pytest.approx(12800.0)]]
    replan_prompt = client.generate_typed_response.call_args_list[2].args[0]
    assert "compute_metric" in replan_prompt
    assert result["answer_blocks"][-1].markdown.startswith("**Как посчитано.**")


def test_persistent_fanout_sql_ends_in_clarification(dataset):
    plans = [ok(PlanResponse(plan=[PlannedCall(tool="run_sql", args={"sql": NAIVE})]))] * 3
    result, _ = run(
        dataset, [ok(Understanding(intent="data_question")), *plans], {"order_total": CONFIRMED}
    )
    (block,) = result["answer_blocks"]
    assert block.type == "clarification"
    assert "compute_metric" in block.markdown


def _error(tool: str, code: str) -> ToolResult:
    return ToolResult(
        result_id=f"r_{code}", status="error", tool=tool, error=ToolError(code=code, message=code)
    )


@pytest.mark.parametrize(
    ("result", "retries", "route"),
    [
        (_error("compute_metric", "metric_mixed_currency"), 1, "clarify"),
        (_error("run_sql", "metric_rows_lost"), 1, "plan"),
        (_error("run_sql", "metric_rows_lost"), 3, "clarify"),
        (_error("run_sql", "database_error"), 3, "visualize"),
    ],
)
def test_metric_errors_route_to_user_decision(result, retries, route):
    state = {
        "plan": [PlannedCall(tool=result.tool, args={})],
        "results": [result],
        "retries": retries,
    }
    assert route_after_execute(state) == route
