"""Память чата: summary старых сообщений, фокус результатов и контекст промпта."""

import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from djedai.agent.data_policy import DataTransferPolicy
from djedai.agent.memory import (
    HISTORY_WINDOW,
    HistoryEntry,
    conversation_context,
    next_focus_result_ids,
    summarize_history,
)
from djedai.contracts.agent import Block, Message
from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultData,
)


def _entry(role: str, text: str, *result_ids: str) -> HistoryEntry:
    return HistoryEntry(role=role, text=text, result_ids=result_ids)


def test_summary_describes_only_messages_outside_the_recent_window() -> None:
    entries = [_entry("user", f"Вопрос {index}") for index in range(HISTORY_WINDOW + 2)]
    for index in range(HISTORY_WINDOW + 2):
        entries[index] = _entry("user", f"Вопрос {index}", f"r_{index}")

    summary = summarize_history(entries)

    assert summary is not None
    assert "Вопрос 0" in summary
    assert "Вопрос 1" in summary
    assert "Вопрос 2" not in summary
    assert "(результаты: r_0)" in summary
    assert "(результаты: r_1)" in summary


def test_summary_is_none_until_the_history_exceeds_the_window() -> None:
    entries = [_entry("user", f"Вопрос {index}") for index in range(HISTORY_WINDOW)]

    assert summarize_history(entries) is None
    assert summarize_history([]) is None


def test_long_summary_keeps_the_most_recent_old_messages() -> None:
    entries = [
        _entry("user", f"Очень длинный старый вопрос номер {index} " + "x" * 50)
        for index in range(HISTORY_WINDOW + 5)
    ]

    summary = summarize_history(entries, max_chars=200)

    assert summary is not None
    assert summary.startswith("…")
    assert len(summary) <= 201
    assert "вопрос номер 0" not in summary
    assert "вопрос номер 4" in summary


def test_summary_uses_answer_blocks_when_text_is_empty() -> None:
    entry = HistoryEntry(
        role="assistant",
        text="",
        result_ids=("r_table",),
        blocks=(
            {"type": "text", "markdown": "Выручка выросла"},
            {"type": "table", "result_id": "r_table"},
            {"type": "chart", "chart": {"title": "Динамика"}},
        ),
    )
    entries = [
        _entry("user", "старый вопрос"),
        entry,
        *[_entry("user", f"новый {index}") for index in range(HISTORY_WINDOW)],
    ]

    summary = summarize_history(entries)

    assert summary is not None
    assert "Выручка выросла" in summary
    assert "старый вопрос" in summary


def test_focus_prefers_new_successful_results_then_references_then_previous() -> None:
    failed = ToolResult(
        result_id="r_bad",
        status="error",
        error=ToolError(code="fixture", message="fixture"),
    )
    ok = ToolResult(result_id="r_new", status="ok")
    known = {"r_old", "r_second", "r_new"}

    assert next_focus_result_ids(
        ["r_old"], plan_results=[failed, ok], references=["r_old"], available_ids=known
    ) == ["r_new"]
    assert next_focus_result_ids(
        ["r_old"], references=["r_second", "r_ghost"], available_ids=known
    ) == ["r_second"]
    assert next_focus_result_ids(["r_old"], references=["r_ghost"], available_ids=known) == [
        "r_old"
    ]
    assert next_focus_result_ids(["r_old", "r_old"], available_ids=known) == ["r_old"]


def test_private_key_is_redacted_before_message_truncation() -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    body = pem.splitlines()[1]
    text = "P" * 140 + " " + pem
    assert len(text) > 250

    context = conversation_context(
        summary=None,
        recent_messages=[Message(role="user", content=text)],
        focus_result_ids=[],
        results=[],
    )

    assert body[:40] not in context
    assert "PRIVATE KEY" not in context
    assert "[REDACTED]" in context


def test_summary_sanitizes_private_key_before_truncation() -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    body = pem.splitlines()[1]
    entries = [
        _entry("user", "P" * 140 + " " + pem),
        *[_entry("user", f"новый вопрос {index}") for index in range(HISTORY_WINDOW)],
    ]

    summary = summarize_history(entries)

    assert summary is not None
    assert body[:40] not in summary
    assert "PRIVATE KEY" not in summary
    assert "[REDACTED]" in summary


def test_json_escaped_quote_is_redacted_before_history_truncation() -> None:
    text = json.dumps({"password": 'prefix"ESCAPED_PASSWORD_TAIL_173'})

    context = conversation_context(
        summary=None,
        recent_messages=[Message(role="user", content=text)],
        focus_result_ids=[],
        results=[],
    )

    assert "ESCAPED_PASSWORD_TAIL_173" not in context
    assert json.loads(context.split("Пользователь: ", 1)[1]) == {"password": "[REDACTED]"}


def test_conversation_context_contains_summary_messages_focus_and_blocks() -> None:
    result = ToolResult(
        result_id="r_second",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["region", "revenue"], rows=[["Север", 1200]]),
        facts=[Fact(text="Выручка 1200", kind="summary")],
        chart_hint=ChartHint(type="bar", x="region", y=["revenue"]),
    )

    context = conversation_context(
        summary="Ранее считали выручку за январь.",
        recent_messages=[
            Message(role="user", content="Посчитай выручку за январь"),
            Message(role="assistant", content="Выручка составила 1200"),
        ],
        focus_result_ids=["r_second"],
        results=[result],
        previous_blocks=[
            Block(type="text", markdown="Первый пункт"),
            Block(type="table", result_id="r_second"),
            Block(
                type="chart",
                chart={
                    "result_id": "r_second",
                    "type": "bar",
                    "title": "Выручка по регионам",
                },
            ),
            Block(type="dashboard_link", dashboard_id="d_1", title="Обзор"),
            Block(type="clarification", markdown="Уточните"),
            Block(type="suggestions", items=["Ещё вопрос"]),
        ],
    )

    assert "Ранее считали выручку за январь." in context
    assert "Пользователь: Посчитай выручку за январь" in context
    assert "1. result_id=r_second; tool=run_sql; columns=['region', 'revenue']" in context
    assert "facts=['Выручка 1200']" in context
    assert "2. table: result_id=r_second" in context
    assert "3. chart: type=bar, title=Выручка по регионам, result_id=r_second" in context
    assert "4. dashboard: Обзор" in context
    assert "5. clarification: Уточните" in context
    assert "6. suggestions: 1" in context


def test_focus_redacts_normalized_sensitive_column_from_catalog() -> None:
    marker = "ordinaryCredential9!"
    result = ToolResult(
        result_id="r_password",
        status="ok",
        tool="describe_column",
        params={"table": "orders", "column": "dbpasswordtext"},
        data=ToolResultData(columns=["dbpasswordtext", "count"], rows=[[marker, 2]]),
        facts=[
            Fact(
                text=f"Значение '{marker}' встречается 2 раз.",
                values={"value": marker, "count": 2},
            )
        ],
    )

    context = conversation_context(
        summary=None,
        recent_messages=[],
        focus_result_ids=["r_password"],
        results=[result],
        policy=DataTransferPolicy.sharing(),
        sensitive_columns=("dbpasswordtext", "dbPasswordText"),
    )

    assert marker not in context
    assert "[REDACTED]" in context


def test_conversation_context_is_explicit_when_there_is_no_history() -> None:
    context = conversation_context(
        summary=None,
        recent_messages=[],
        focus_result_ids=[],
        results=[],
    )

    assert context == "Истории предыдущих сообщений нет."
