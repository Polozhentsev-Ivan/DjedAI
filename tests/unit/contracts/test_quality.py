from __future__ import annotations

import pytest
from pydantic import ValidationError

from djedai.contracts import ColumnQuality, QualityIssue, QualityReport, ToolResult

AMOUNT = {
    "table": "orders",
    "column": "amount",
    "raw_table": "raw_orders",
    "original_name": "Сумма",
    "transformation": "parse_money",
    "target_type": "money",
    "format": "decimal_comma, RUB",
    "usage": ["measure"],
    "scope": "population",
    "rows": 103,
    "source_empty": 3,
    "non_empty": 100,
    "parse_failed": 4,
    "failed_ratio": 0.04,
    "examples": ["n/a", "—"],
    "issues": [
        {
            "code": "parse_losses",
            "severity": "warning",
            "message": "4 из 100 непустых значений не распознаны",
        }
    ],
}


def test_report_round_trips_inside_tool_result():
    report = QualityReport(lineage="complete", checked=["orders.amount"], columns=[AMOUNT])
    result = ToolResult(result_id="r_1", status="ok", quality=report)
    restored = ToolResult.model_validate_json(result.model_dump_json())
    assert restored.quality == report
    assert restored.quality.columns[0].severity == "warning"
    assert restored.quality.has("warning")
    assert not restored.quality.has("blocking")


def test_source_empty_and_parse_failures_are_separate_counts():
    with pytest.raises(ValidationError, match="пустые"):
        ColumnQuality.model_validate({**AMOUNT, "source_empty": 0})
    with pytest.raises(ValidationError, match="ошибок разбора"):
        ColumnQuality.model_validate({**AMOUNT, "rows": 3, "non_empty": 0})


def test_table_profile_requires_boundary():
    with pytest.raises(ValidationError, match="boundary"):
        ColumnQuality.model_validate({**AMOUNT, "scope": "table"})
    with pytest.raises(ValidationError, match="boundary"):
        ColumnQuality.model_validate({**AMOUNT, "boundary": "по всей таблице"})
    profile = ColumnQuality.model_validate(
        {**AMOUNT, "scope": "table", "boundary": "по всей таблице до фильтра"}
    )
    assert profile.scope == "table"


def test_partial_lineage_names_what_was_not_resolved():
    with pytest.raises(ValidationError, match="unresolved"):
        QualityReport(lineage="partial")
    with pytest.raises(ValidationError, match="unresolved"):
        QualityReport(lineage="complete", unresolved=["read_csv(...)"])
    assert QualityReport(lineage="partial", unresolved=["x"]).lineage == "partial"


def test_issue_requires_known_code():
    with pytest.raises(ValidationError):
        QualityIssue(code="unknown", severity="info", message="x")
