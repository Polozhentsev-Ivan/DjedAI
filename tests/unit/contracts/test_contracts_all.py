"""Тесты моделей общих контрактов djedai.contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from djedai.contracts import (
    AssistantMessage,
    ChartSpec,
    ChartTemplate,
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
                chart=ChartTemplate(chart_id="chart_growth", type="kpi", title="Рост продаж"),
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


def test_clarification_block_validation() -> None:
    # Cannot be created without options
    with pytest.raises(ValidationError):
        ClarificationBlock(question="Какую метрику выбрать?")  # type: ignore[call-arg]

    # Cannot be created with less than 2 options
    with pytest.raises(ValidationError):
        ClarificationBlock(question="Какую метрику выбрать?", options=["Только одна"])

    # Cannot be created with empty strings even among multiple options
    with pytest.raises(ValidationError):
        ClarificationBlock(question="Какую метрику выбрать?", options=["Вариант 1", "  "])

    with pytest.raises(ValidationError):
        ClarificationBlock(
            question="Какую метрику выбрать?", options=["Вариант 1", "Вариант 2", "   "]
        )

    # Cannot be created with duplicate options (including after whitespace strip)
    with pytest.raises(ValidationError):
        ClarificationBlock(question="Какую метрику выбрать?", options=["Выручка", "Выручка"])

    with pytest.raises(ValidationError):
        ClarificationBlock(question="Какую метрику выбрать?", options=["Выручка", "   Выручка   "])

    cb = ClarificationBlock(
        question="Какую метрику выбрать?", options=["  Вариант 1  ", "  Вариант 2  "]
    )
    assert cb.options == ["Вариант 1", "Вариант 2"]


def test_tool_result_data_with_columns_and_row_count() -> None:
    from djedai.contracts import ToolResultColumn, ToolResultData

    data = ToolResultData(
        columns=[
            ToolResultColumn(name="month", type="date", label="Месяц"),
            "amount",
        ],
        rows=[["2023-01-01", 120400.5]],
        row_count=48,
        truncated=False,
    )
    assert data.row_count == 48
    assert data["row_count"] == 48
    assert len(data.columns) == 2
    assert isinstance(data.columns[0], ToolResultColumn)
    assert data.columns[0].name == "month"
    assert data.columns[0]["name"] == "month"
    assert data.columns[1] == "amount"
    assert data.get("non_existent", "default") == "default"

    # From dict payload (§4.3 specification format)
    res = ToolResult.model_validate(
        {
            "result_id": "r_17",
            "status": "ok",
            "data": {
                "columns": [
                    {"name": "month", "type": "date", "label": "Месяц"},
                    {"name": "amount", "type": "money", "label": "Выручка"},
                ],
                "rows": [["2023-01-01", 120400.5]],
                "row_count": 48,
                "truncated": False,
            },
        }
    )
    assert res.data is not None
    assert res.data.row_count == 48
    assert res.data["row_count"] == 48
    assert isinstance(res.data.columns[0], ToolResultColumn)
    assert res.data.columns[0].label == "Месяц"


def test_tool_result_status_consistency() -> None:
    # status=error requires error
    with pytest.raises(ValueError):
        ToolResult(result_id="r_err", status="error")

    # status=ok forbids error
    with pytest.raises(ValueError):
        ToolResult(
            result_id="r_ok",
            status="ok",
            error=ToolError(code="err", message="msg"),
        )


def test_sse_agent_stream_events() -> None:
    from pydantic import TypeAdapter

    from djedai.contracts.sse import (
        AgentStreamEvent,
        DatasetStreamEvent,
        SSEBlockEvent,
        SSEDoneEvent,
        SSEStatusEvent,
    )

    ta_agent = TypeAdapter(AgentStreamEvent)

    # 1. Status event
    ev_status = ta_agent.validate_python(
        {"event": "status", "data": {"node": "execute", "text": "Считаю сезонность..."}}
    )
    assert isinstance(ev_status, SSEStatusEvent)
    assert ev_status.event == "status"
    assert ev_status.data.node == "execute"

    # 2. Block event with TextBlock
    ev_block = ta_agent.validate_python(
        {"event": "block", "data": {"type": "text", "markdown": "Привет!"}}
    )
    assert isinstance(ev_block, SSEBlockEvent)
    assert ev_block.data.type == "text"

    # 3. Done event
    ev_done = ta_agent.validate_python({"event": "done", "data": {"message_id": "m_101"}})
    assert isinstance(ev_done, SSEDoneEvent)
    assert ev_done.data.message_id == "m_101"

    # 4. Dataset stream events
    ta_ds = TypeAdapter(DatasetStreamEvent)
    ev_ds = ta_ds.validate_python(
        {"event": "progress", "data": {"stage": "profiling", "progress": 0.5, "message": "Анализ"}}
    )
    assert ev_ds.event == "progress"
    assert ev_ds.data.progress == 0.5
