"""Память чата: краткое описание старых сообщений и контекст прошлых результатов.

Модуль намеренно не хранит состояние и не обращается к БД: backend передаёт
нормализованные сообщения, а агент получает готовый текстовый контекст. Summary
строится детерминированно из последних реплик, чтобы не тратить отдельный вызов
модели и не зависеть от её доступности внутри лимита хода.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from djedai.agent.data_policy import DataContext, DataTransferPolicy
from djedai.contracts.agent import Block, Message
from djedai.contracts.tool_result import ToolResult

# В промпт попадают последние 10 сообщений; более старые сжимаются в summary.
HISTORY_WINDOW = 10
SUMMARY_MAX_CHARS = 2000
SUMMARY_LINE_CHARS = 300
CONTEXT_LINE_CHARS = 250
CONTEXT_RESULT_ROWS = 3
CONTEXT_FACTS = 4


@dataclass(frozen=True)
class HistoryEntry:
    """Нормализованное сообщение чата для summary и контекста агента."""

    role: str
    text: str = ""
    result_ids: tuple[str, ...] = ()
    blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def compact_text(text: str | None, limit: int) -> str:
    """Однострочный фрагмент без лишних пробелов, обрезанный по границе слова."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"


def entry_text(entry: HistoryEntry) -> str:
    """Текст реплики; если его нет, собираем подписи блоков ответа."""
    if entry.text.strip():
        return entry.text
    parts: list[str] = []
    for block in entry.blocks:
        if not isinstance(block, dict):
            continue
        markdown = block.get("markdown")
        if markdown:
            parts.append(str(markdown))
        elif block.get("type") == "table" and block.get("result_id"):
            parts.append(f"таблица {block['result_id']}")
        elif block.get("type") == "chart":
            chart = block.get("chart") or {}
            title = chart.get("title") if isinstance(chart, dict) else None
            parts.append(f"график {title or ''}".strip())
    return " ".join(parts)


def summarize_history(
    entries: Sequence[HistoryEntry],
    *,
    window: int = HISTORY_WINDOW,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str | None:
    """Краткое описание сообщений вне окна последних ``window`` реплик.

    Возвращает ``None``, когда старых сообщений ещё нет: вызывающая сторона
    сохраняет прежнее summary из БД. Слишком длинная история обрезается с начала,
    чтобы в контексте остались самые свежие старые реплики.
    """
    older = list(entries[:-window]) if window > 0 else list(entries)
    if not older:
        return None

    lines: list[str] = []
    for entry in older:
        text = compact_text(
            DataTransferPolicy.restricted().sanitize_text(entry_text(entry)),
            SUMMARY_LINE_CHARS,
        )
        if not text and not entry.result_ids:
            continue
        role = "Пользователь" if entry.role == "user" else "Ассистент"
        line = f"{role}: {text or '—'}"
        if entry.result_ids:
            line += f" (результаты: {', '.join(entry.result_ids)})"
        lines.append(line)
    if not lines:
        return None
    return _fit_from_end(lines, max_chars)


def next_focus_result_ids(
    previous: Iterable[str],
    *,
    plan_results: Sequence[ToolResult] = (),
    references: Sequence[str] = (),
    available_ids: Iterable[str] = (),
) -> list[str]:
    """Новый фокус разговора: результаты текущего расчёта, ссылки или прежний фокус.

    Ошибки не становятся фокусом. Если ссылки модели не указывают на известные
    результаты, сохраняется прежний фокус, а не подставляется чужой/выдуманный ID.
    """
    successful = [result.result_id for result in plan_results if result.status == "ok"]
    if successful:
        return _unique(successful)
    known = set(available_ids)
    valid_refs = [reference for reference in references if reference in known]
    if valid_refs:
        return _unique(valid_refs)
    return _unique(previous)


def conversation_context(
    *,
    summary: str | None,
    recent_messages: Sequence[Message],
    focus_result_ids: Sequence[str],
    results: Sequence[ToolResult],
    previous_blocks: Sequence[Block] = (),
    policy: DataTransferPolicy | None = None,
    sensitive_columns: Collection[str] = (),
) -> str:
    """Текст для промптов: summary, последние реплики, фокус и структура ответа.

    Политика по умолчанию — безопасная restricted: строки прошлых результатов не
    попадают в контекст, но метаданные и факты остаются. Вызывающий узел передаёт
    политику клиента, чтобы режим был единым на весь ход.
    """
    policy = policy or DataTransferPolicy.restricted()
    sections: list[str] = []
    if summary:
        sections.append(f"Краткая история старой части диалога:\n{policy.sanitize_text(summary)}")

    recent = [
        f"{'Пользователь' if message.role == 'user' else 'Ассистент'}: "
        f"{compact_text(policy.sanitize_text(message.content), CONTEXT_LINE_CHARS)}"
        for message in recent_messages[-HISTORY_WINDOW:]
        if message.content.strip()
    ]
    if recent:
        sections.append("Последние сообщения диалога:\n" + "\n".join(recent))

    focus = _focus_descriptions(focus_result_ids, results, policy, sensitive_columns)
    if focus:
        sections.append(
            "Результаты в фокусе разговора (нумерованы для ссылок «первый», "
            "«второй», «тот же период»):\n" + "\n".join(focus)
        )

    blocks = _block_descriptions(previous_blocks, policy)
    if blocks:
        sections.append("Структура предыдущего ответа:\n" + "\n".join(blocks))

    return "\n\n".join(sections) if sections else "Истории предыдущих сообщений нет."


def _focus_descriptions(
    focus_result_ids: Sequence[str],
    results: Sequence[ToolResult],
    policy: DataTransferPolicy,
    sensitive_columns: Collection[str] = (),
) -> list[str]:
    by_id = {result.result_id: result for result in results}
    lines: list[str] = []
    for index, result_id in enumerate(dict.fromkeys(focus_result_ids), start=1):
        result = by_id.get(result_id)
        if result is None:
            continue
        parts = [f"{index}. result_id={result.result_id}", f"tool={result.tool or 'unknown'}"]
        if result.data is not None:
            names = [_column_name(column) for column in result.data.columns]
            parts.append(f"columns={names}")
            if result.data.row_count is not None:
                parts.append(f"row_count={result.data.row_count}")
            data = policy.result_data(
                result.data,
                context=DataContext.HISTORY,
                max_rows=CONTEXT_RESULT_ROWS,
                sensitive_columns=policy.sensitive_result_columns(
                    result, sensitive_columns=sensitive_columns
                ),
            )
            if data.get("rows"):
                parts.append(f"sample_rows={data['rows']}")
            elif data.get("rows_omitted"):
                parts.append("sample_rows=omitted_by_data_policy")
        if result.facts:
            sensitive = policy.result_has_sensitive_source(
                result, sensitive_columns=sensitive_columns
            )
            facts = [
                policy.sanitize_fact(fact, sensitive=sensitive)["text"]
                for fact in result.facts[:CONTEXT_FACTS]
            ]
            parts.append(f"facts={facts}")
        if result.chart_hint is not None:
            parts.append(f"chart_hint={policy.sanitize_value(result.chart_hint)}")
        lines.append("; ".join(parts))
    return lines


def _block_descriptions(blocks: Sequence[Block], policy: DataTransferPolicy) -> list[str]:
    lines: list[str] = []
    for index, block in enumerate(blocks, start=1):
        if block.type == "text":
            text = compact_text(policy.sanitize_text(block.markdown), CONTEXT_LINE_CHARS)
            lines.append(f"{index}. text: {text or '—'}")
        elif block.type == "table":
            lines.append(f"{index}. table: result_id={block.result_id or '?'}")
        elif block.type == "chart":
            chart = block.chart or {}
            chart_type = chart.get("type") if isinstance(chart, dict) else None
            title = chart.get("title") if isinstance(chart, dict) else None
            result_id = chart.get("result_id") if isinstance(chart, dict) else None
            lines.append(
                f"{index}. chart: type={chart_type or '?'}, "
                f"title={policy.sanitize_text(title) or '—'}, "
                f"result_id={result_id or '?'}"
            )
        elif block.type == "dashboard_link":
            title = policy.sanitize_text(block.title or block.dashboard_id or "?")
            lines.append(f"{index}. dashboard: {title}")
        elif block.type == "clarification":
            text = compact_text(policy.sanitize_text(block.markdown), 120)
            lines.append(f"{index}. clarification: {text}")
        elif block.type == "suggestions":
            lines.append(f"{index}. suggestions: {len(block.items)}")
    return lines


def _column_name(column: Any) -> str:
    if isinstance(column, str):
        return column
    if hasattr(column, "name"):
        return str(column.name)
    if isinstance(column, dict) and "name" in column:
        return str(column["name"])
    return str(column)


def _fit_from_end(lines: Sequence[str], max_chars: int) -> str:
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    boundary = tail.find("\n")
    if boundary != -1:
        tail = tail[boundary + 1 :]
    return "…" + tail


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
