"""Тесты моделей общих контрактов djedai.contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from djedai.contracts import (
    AssistantMessage,
    ChartSpec,
    ClarificationBlock,
    Dashboard,
    DashboardWidget,
    Fact,
    TextBlock,
    ToolError,
    ToolResult,
    UserCreate,
    UserRead,
    WidgetLayout,
    WidgetToolCall,
)


def test_tool_result_serialization() -> None:
    res = ToolResult(
        result_id="r_42",
        status="ok",
        facts=[Fact(text="Выручка выросла на 15%", kind="metric", values={"growth": 0.15})],
        sql="SELECT sum(amount) FROM orders",
    )
    dumped = res.model_dump(mode="json")
    assert dumped["result_id"] == "r_42"
    assert dumped["status"] == "ok"
    assert dumped["facts"][0]["values"]["growth"] == 0.15

    # Валидация ошибки при status=error
    err_res = ToolResult(
        result_id="r_err",
        status="error",
        error=ToolError(code="column_not_found", message="Колонка не найдена"),
    )
    assert err_res.error.code == "column_not_found"


def test_chart_spec_serialization() -> None:
    chart = ChartSpec(
        chart_id="ch_1",
        result_id="r_42",
        type="line",
        title="Динамика выручки",
        limit=10,
    )
    dumped = chart.model_dump(mode="json")
    assert dumped["chart_id"] == "ch_1"
    assert dumped["type"] == "line"

    # Forbid extra fields
    with pytest.raises(ValidationError):
        ChartSpec(chart_id="ch_2", result_id="r_1", type="line", title="T", extra_field=123)


def test_assistant_message_with_blocks() -> None:
    now = datetime.now(UTC)
    msg = AssistantMessage(
        message_id="m_1",
        chat_id="c_1",
        created_at=now,
        blocks=[
            TextBlock(markdown="Инсайт по данным"),
            ClarificationBlock(
                question="Какую метрику показать?", options=["Выручка", "Количество"]
            ),
        ],
        result_ids=["r_42"],
    )
    assert len(msg.blocks) == 2
    assert msg.blocks[0].type == "text"
    assert msg.blocks[1].type == "clarification"


def test_dashboard_model() -> None:
    now = datetime.now(UTC)
    dash = Dashboard(
        dashboard_id="d_1",
        dataset_id="ds_1",
        title="Главный дашборд",
        created_at=now,
        widgets=[
            DashboardWidget(
                widget_id="w_1",
                tool_call=WidgetToolCall(tool="time_aggregate", params={"freq": "month"}),
                caption="Рост продаж",
                layout=WidgetLayout(x=0, y=0, w=6, h=4),
            )
        ],
    )
    assert dash.widgets[0].widget_id == "w_1"
    assert dash.widgets[0].tool_call.tool == "time_aggregate"


def test_auth_models() -> None:
    now = datetime.now(UTC)
    create = UserCreate(username="analyst_1", password="secret_password_123")
    assert create.username == "analyst_1"

    read = UserRead(user_id="u_1", username=create.username, created_at=now)
    assert read.user_id == "u_1"
    assert read.is_active is True
