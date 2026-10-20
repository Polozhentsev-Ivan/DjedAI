import json
from copy import deepcopy
from pathlib import Path

import pytest

from djedai.contracts import (
    AssistantMessage,
    Dashboard,
    DashboardSnapshot,
    DatasetCatalog,
    ErrorResponse,
    ToolResult,
    WidgetRecipe,
)


@pytest.fixture
def example():
    path = (
        Path(__file__).resolve().parents[3]
        / "src/djedai/contracts/examples/component_exchange.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_exchange_examples_share_ids_and_schema_across_components(example):
    catalog = DatasetCatalog.model_validate(example["catalog"])
    result = ToolResult.model_validate(example["success"])
    message = AssistantMessage.model_validate(example["message"])
    dashboard = Dashboard.model_validate(example["dashboard"])
    assert result.data.rows == [[catalog.tables[0].row_count]]
    assert message.result_ids == [result.result_id]
    assert message.blocks[1].chart.result_id == result.result_id
    assert dashboard.dataset_id == catalog.dataset_id
    recipe = dashboard.widgets[0].recipe
    assert recipe.steps[-1].source_step_id == recipe.steps[0].step_id
    assert "source_result_id" not in json.dumps(recipe.model_dump())
    assert Dashboard.model_validate_json(dashboard.model_dump_json()) == dashboard
    assert ToolResult.model_validate(example["tool_error"]).status == "error"
    assert ErrorResponse.model_validate(example["http_error"]).error.code == "timeout"
    snapshot = DashboardSnapshot.model_validate(example["partial_failure"])
    assert snapshot.widgets[0].result_id is None
    assert snapshot.widgets[0].error is not None
    assert snapshot.widgets[1].status == "ok"


@pytest.mark.parametrize("invalid", ["duplicate", "forward", "self", "missing_output", "old_id"])
def test_recipe_rejects_unreplayable_dependencies(example, invalid):
    recipe = deepcopy(example["dashboard"]["widgets"][0]["recipe"])
    if invalid == "duplicate":
        recipe["steps"][1]["step_id"] = "rows"
    elif invalid == "forward":
        recipe["steps"][0]["source_step_id"] = "count"
    elif invalid == "self":
        recipe["steps"][0]["source_step_id"] = "rows"
    elif invalid == "missing_output":
        recipe["output_step_id"] = "does_not_exist"
    else:
        recipe["steps"][1]["call"]["params"]["source_result_id"] = "r_from_old_chat"
    with pytest.raises(ValueError):
        WidgetRecipe.model_validate(recipe)


@pytest.mark.parametrize("invalid", ["both", "neither", "legacy_dependency"])
def test_widget_requires_one_reproducible_recipe(example, invalid):
    dashboard = deepcopy(example["dashboard"])
    widget = dashboard["widgets"][0]
    if invalid == "both":
        widget["tool_call"] = {"tool": "run_sql", "params": {"sql": "SELECT 1"}}
    elif invalid == "neither":
        widget["recipe"] = None
    else:
        widget["recipe"] = None
        widget["tool_call"] = {"tool": "run_sql", "params": {"source_result_id": "r_old"}}
    with pytest.raises(ValueError):
        Dashboard.model_validate(dashboard)


@pytest.mark.parametrize("invalid", ["missing_error", "stale_result", "mismatched_chart"])
def test_snapshot_does_not_present_failed_or_stale_data_as_fresh(example, invalid):
    snapshot = deepcopy(example["partial_failure"])
    if invalid == "missing_error":
        snapshot["widgets"][0]["error"] = None
    elif invalid == "stale_result":
        snapshot["widgets"][0]["result_id"] = "r_count_1"
    else:
        snapshot["widgets"][1]["result_id"] = "r_different"
    with pytest.raises(ValueError):
        DashboardSnapshot.model_validate(snapshot)
