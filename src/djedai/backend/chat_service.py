"""Оркестрация хода чата: запуск агента, сохранение и SSE-события."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from djedai.agent.config import get_agent_client
from djedai.agent.diagnostics import RunDiagnostics, Tariff
from djedai.agent.graph import build_agent_graph
from djedai.agent.memory import HISTORY_WINDOW, HistoryEntry, summarize_history
from djedai.analytics.registry import execute_tool
from djedai.backend.dataset_service import DatasetContext
from djedai.backend.db.models import ChatModel, DashboardModel, DatasetModel, MessageModel
from djedai.backend.db.repositories import (
    get_chat as get_chat_record,
)
from djedai.backend.db.repositories import (
    get_chat_messages,
    get_owned_tool_result,
    load_tool_result,
    save_assistant_message,
    save_message,
    save_tool_result,
    update_chat_memory,
)
from djedai.backend.diagnostics import DiagnosticJournal
from djedai.config import get_limits
from djedai.contracts.agent import Block, Message
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.chart import ChartSpec
from djedai.contracts.chat import ChatMessage, ChatSummary
from djedai.contracts.message import (
    AssistantMessage,
    ChartBlock,
    ClarificationBlock,
    DashboardLinkBlock,
    MessageBlock,
    SuggestionsBlock,
    TableBlock,
    TextBlock,
    TraceStep,
)
from djedai.contracts.sse import (
    AgentStreamEvent,
    SSEBlockEvent,
    SSEDoneData,
    SSEDoneEvent,
    SSEErrorData,
    SSEErrorEvent,
    SSEStatusData,
    SSEStatusEvent,
    SSEToolCallData,
    SSEToolCallEvent,
    SSEToolResultData,
    SSEToolResultEvent,
)
from djedai.contracts.tool_result import ToolResult
from djedai.execution import ExecutionBudget, TurnCancelled, TurnTimeout

_BLOCK_ADAPTER: TypeAdapter[MessageBlock] = TypeAdapter(MessageBlock)
logger = logging.getLogger(__name__)

_STATUS_TEXT = {
    "load_ctx": "Загружаю контекст чата…",
    "plan": "Строю план анализа…",
    "execute": "Выполняю расчёты…",
    "visualize": "Подбираю визуализацию…",
    "compose": "Формулирую ответ…",
    "verify": "Проверяю числа в ответе…",
    "clarify": "Нужно уточнить вопрос…",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class AgentRuntime:
    """Зависимости запуска агента, которые можно подменить в тестах."""

    graph: Any
    client: Any | None = None
    tool_dispatcher: Callable[[str, dict[str, Any]], ToolResult] | None = None
    catalog_loader: Callable[[str], DatasetCatalog] | None = None
    dataset_context: Callable[[str, str], DatasetContext] | None = None
    results_storage_dir: Path | str | None = None
    turn_timeout_seconds: float | None = None
    diagnostics: DiagnosticJournal | None = None
    tariff: Tariff | None = None

    def budget(self, is_cancelled: Callable[[], bool] | None = None) -> ExecutionBudget:
        seconds = (
            self.turn_timeout_seconds
            if self.turn_timeout_seconds is not None
            else get_limits().agent_turn_timeout_seconds
        )
        return ExecutionBudget(seconds, is_cancelled)


def build_agent_runtime() -> AgentRuntime:
    """Создаёт реальный runtime; отсутствие конфигурации не имитирует ответ модели."""
    try:
        client = get_agent_client()
    except ValueError as exc:
        raise HTTPException(
            status_code=503, detail="Настройте MODEL_BASE_URL, MODEL_NAME и MODEL_API_KEY"
        ) from exc
    return AgentRuntime(
        graph=build_agent_graph(),
        client=client,
    )


def get_agent_runtime(request: Request) -> AgentRuntime:
    """Привязывает каждый HTTP-ход к постоянному хранилищу приложения."""
    settings = request.app.state.settings
    tariff = (
        Tariff(
            settings.model_input_price_usd,
            settings.model_output_price_usd,
            settings.model_price_reference,
        )
        if settings.model_input_price_usd is not None
        else None
    )
    return replace(
        build_agent_runtime(),
        dataset_context=request.app.state.dataset_service.get_context,
        results_storage_dir=request.app.state.settings.results_data_dir,
        turn_timeout_seconds=request.app.state.settings.agent_turn_timeout_seconds,
        diagnostics=request.app.state.diagnostics,
        tariff=tariff,
    )


def chat_summary(chat: ChatModel) -> ChatSummary:
    """Преобразует запись чата в контракт ChatSummary."""
    return ChatSummary(
        chat_id=chat.chat_id,
        dataset_id=chat.dataset_id,
        title=chat.title,
        created_at=_aware(chat.created_at),
    )


def message_to_chat_message(message: MessageModel) -> ChatMessage:
    """Преобразует сохранённое сообщение в контракт ChatMessage."""
    role = "user" if message.role == "user" else "assistant"
    blocks: list[MessageBlock] = []
    if role == "assistant":
        for raw in message.blocks or []:
            try:
                blocks.append(_BLOCK_ADAPTER.validate_python(raw))
            except ValidationError:
                blocks.append(TextBlock(markdown=str(raw)))
    return ChatMessage(
        message_id=message.message_id,
        role=role,
        created_at=_aware(message.created_at),
        text=message.content if role == "user" else None,
        blocks=blocks,
        result_ids=message.result_ids or [],
    )


def _history_text(message: MessageModel) -> str:
    content = message.content or ""
    if not content and message.blocks:
        content = "\n".join(
            str(block.get("markdown", ""))
            for block in message.blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return content


def _history_message(message: MessageModel) -> Message:
    role = "user" if message.role == "user" else "assistant"
    return Message(role=role, content=_history_text(message))


def _history_entry(message: MessageModel) -> HistoryEntry:
    blocks = tuple(block for block in (message.blocks or []) if isinstance(block, dict))
    return HistoryEntry(
        role=message.role,
        text=_history_text(message),
        result_ids=tuple(message.result_ids or []),
        blocks=blocks,
    )


def _previous_response_blocks(messages: list[MessageModel]) -> list[Block]:
    """Блоки последнего ответа ассистента этого же чата."""
    for message in reversed(messages):
        if message.role != "assistant" or not message.blocks:
            continue
        blocks: list[Block] = []
        for raw in message.blocks:
            if not isinstance(raw, dict):
                continue
            try:
                blocks.append(Block.model_validate(raw))
            except ValidationError:
                continue
        if blocks:
            return blocks
    return []


def _block_to_message_block(block: Block) -> MessageBlock:
    """Преобразует блок агента в согласованный MessageBlock, не теряя будущие типы."""
    if block.type == "text":
        return TextBlock(markdown=block.markdown or "")
    if block.type == "clarification":
        options = [option.strip() for option in block.options if option.strip()]
        if len(options) >= 2 and len(set(options)) == len(options):
            return ClarificationBlock(
                question=block.markdown or "Уточните, пожалуйста, вопрос",
                options=options,
            )
        return TextBlock(markdown=block.markdown or "Уточните, пожалуйста, вопрос")
    if block.type == "table" and block.result_id:
        return TableBlock(
            result_id=block.result_id,
            columns=block.columns,
            limit=block.limit,
        )
    if block.type == "chart" and block.chart:
        try:
            return ChartBlock(chart=ChartSpec.model_validate(block.chart))
        except ValidationError:
            return TextBlock(markdown=block.markdown or "Не удалось построить график")
    if block.type == "dashboard_link" and block.dashboard_id:
        return DashboardLinkBlock(
            dashboard_id=block.dashboard_id,
            title=block.title or "Дашборд",
        )
    if block.type == "suggestions":
        return SuggestionsBlock(items=block.items)
    return TextBlock(markdown=block.markdown or f"Блок «{block.type}» пока не поддерживается")


def _agent_error(blocks: list[Block]) -> str | None:
    for block in blocks:
        if block.type == "error":
            return block.markdown or "Агент не смог обработать запрос"
    return None


def _status_event(node: str) -> SSEStatusEvent:
    return SSEStatusEvent(
        data=SSEStatusData(
            node=node,
            text=_STATUS_TEXT.get(node, "Обрабатываю запрос…"),
        )
    )


def turn_timeout_event() -> SSEErrorEvent:
    return SSEErrorEvent(
        data=SSEErrorData(
            code="turn_timeout",
            message="Время ожидания ответа истекло",
            detail="Общий лимит времени исчерпан. Повторите запрос явно или уточните вопрос.",
        )
    )


def stream_chat_turn(
    db: Session,
    chat: ChatModel | str,
    text: str,
    runtime: AgentRuntime,
    is_cancelled: Callable[[], bool] | None = None,
    *,
    owner_id: str,
    budget: ExecutionBudget | None = None,
) -> Iterator[AgentStreamEvent]:
    """Сохраняет вопрос, выполняет граф агента и отдаёт события по мере готовности."""
    resources = ExitStack()
    budget = budget or runtime.budget(is_cancelled)
    diagnostics: RunDiagnostics | None = None
    try:
        budget.check()
        if is_cancelled and is_cancelled():
            return
        chat_model = get_chat_record(db, chat if isinstance(chat, str) else chat.chat_id)
        dataset = db.get(DatasetModel, chat_model.dataset_id) if chat_model else None
        if (
            not owner_id
            or chat_model is None
            or chat_model.owner_id != owner_id
            or dataset is None
            or dataset.owner_id != owner_id
        ):
            yield SSEErrorEvent(
                data=SSEErrorData(
                    message="Не удалось получить ответ агента",
                    detail="Чат не найден",
                )
            )
            return

        user_message_id = _new_id("m")
        if runtime.diagnostics is not None:
            diagnostics = RunDiagnostics(
                runtime.diagnostics,
                owner_id=owner_id,
                chat_id=chat_model.chat_id,
                dataset_id=chat_model.dataset_id,
                user_message_id=user_message_id,
                tariff=runtime.tariff,
            )

        if runtime.dataset_context is not None:
            budget.check()
            if is_cancelled and is_cancelled():
                return
            context = runtime.dataset_context(owner_id, chat_model.dataset_id)
            if context.catalog.dataset_id != chat_model.dataset_id:
                raise ValueError("Каталог не относится к датасету чата")
            connection = resources.enter_context(context.open_connection())
            allowed_tables = frozenset(table.table_id for table in context.catalog.tables)

            def dispatch(tool: str, params: dict[str, Any]) -> ToolResult:
                if params.get("source_result_id") is not None:
                    raise ValueError("Вычисления поверх source_result_id будут доступны позднее")
                return execute_tool(
                    tool, params, connection=connection, allowed_tables=allowed_tables
                )

            def catalog(dataset_id: str) -> DatasetCatalog:
                if dataset_id != context.catalog.dataset_id:
                    raise ValueError("Каталог не относится к датасету чата")
                return context.catalog

            runtime = replace(runtime, tool_dispatcher=dispatch, catalog_loader=catalog)

        if is_cancelled and is_cancelled():
            return
        budget.check()
        save_message(
            db,
            message_id=user_message_id,
            chat_id=chat_model.chat_id,
            role="user",
            content=text,
        )
        all_messages = get_chat_messages(db, chat_model.chat_id)
        history_messages = all_messages[-HISTORY_WINDOW:]
        history = [_history_message(message) for message in history_messages]
        history_summary = summarize_history([_history_entry(item) for item in all_messages])
        history_summary = history_summary or chat_model.summary
        previous_focus = list(chat_model.focus_result_ids or [])
        previous_blocks = _previous_response_blocks(all_messages[:-1])

        history_results: list[ToolResult] = []
        seen_result_ids = set()

        for msg in all_messages:
            for rid in msg.result_ids or []:
                if rid not in seen_result_ids:
                    seen_result_ids.add(rid)
                    # Explicit provenance checks keep results from another chat/user
                    # out of this chat's context, even if an ID appears in a message.
                    res = load_tool_result(
                        db,
                        rid,
                        storage_dir=runtime.results_storage_dir,
                        owner_id=owner_id,
                        chat_id=chat_model.chat_id,
                        dataset_id=chat_model.dataset_id,
                    )
                    if res is not None:
                        history_results.append(res)

        # A stored focus may outlive a deleted result; never forward an ID that
        # this chat/owner cannot resolve.
        loaded_result_ids = {result.result_id for result in history_results}
        previous_focus = [
            result_id for result_id in previous_focus if result_id in loaded_result_ids
        ]

        state: dict[str, Any] = {
            "chat_id": chat_model.chat_id,
            "dataset_id": chat_model.dataset_id,
            "messages": history,
            "history_summary": history_summary,
            "history_results": history_results,
            "focus_result_ids": previous_focus,
            "previous_blocks": previous_blocks,
        }

        configurable: dict[str, Any] = {"thread_id": chat_model.chat_id, "budget": budget}
        if diagnostics is not None:
            configurable["diagnostics"] = diagnostics
        if runtime.client is not None:
            configurable["client"] = runtime.client
        if runtime.tool_dispatcher is not None:
            configurable["tool_dispatcher"] = runtime.tool_dispatcher
        if is_cancelled is not None:
            configurable["is_cancelled"] = is_cancelled
        if runtime.catalog_loader is not None:
            configurable["catalog_loader"] = runtime.catalog_loader

        trace: list[TraceStep] = []
        results: list[ToolResult] = []
        seen_results: set[str] = set()
        answer_blocks: list[Block] = []
        stream_focus: list[str] | None = None
        started = perf_counter()
        previous = started

        yield _status_event("understand")

        for update in runtime.graph.stream(
            state,
            config={"configurable": configurable},
            stream_mode="updates",
        ):
            budget.check()
            if is_cancelled and is_cancelled():
                return
            now = perf_counter()
            for node, data in update.items():
                trace.append(TraceStep(node=node, ms=int((now - previous) * 1000)))
                previous = now
                if node in ("understand", "load_ctx"):
                    continue
                if node in _STATUS_TEXT:
                    yield _status_event(node)

                if node == "execute":
                    execute_update = data if isinstance(data, dict) else {}
                    for result in execute_update.get("results", []):
                        budget.check()
                        if result.result_id in seen_results:
                            continue
                        seen_results.add(result.result_id)
                        results.append(result)
                        save_tool_result(
                            db,
                            result,
                            chat_id=chat_model.chat_id,
                            owner_id=owner_id,
                            storage_dir=runtime.results_storage_dir,
                        )
                        yield SSEToolCallEvent(
                            data=SSEToolCallData(
                                tool=result.tool or "unknown",
                                params=result.params or {},
                            )
                        )
                        yield SSEToolResultEvent(
                            data=SSEToolResultData(
                                result_id=result.result_id,
                                ok=result.status == "ok",
                                error=result.error.message if result.error else None,
                            )
                        )

                if node in ("visualize", "compose", "verify", "clarify"):
                    node_update = data if isinstance(data, dict) else {}
                    if node in ("visualize", "compose", "clarify") and (
                        "focus_result_ids" in node_update
                    ):
                        stream_focus = list(node_update["focus_result_ids"] or [])
                    if "answer_blocks" in node_update:
                        answer_blocks = node_update["answer_blocks"] or []

        budget.check()
        error_detail = _agent_error(answer_blocks)
        if error_detail is not None:
            if diagnostics is not None:
                error_type = diagnostics.last_error or "AGENT_ERROR"
                diagnostics.finish("timeout" if error_type == "TIMEOUT" else "error", error_type)
            yield SSEErrorEvent(
                data=SSEErrorData(
                    message="Не удалось получить ответ агента",
                    detail=error_detail,
                )
            )
            return

        if is_cancelled and is_cancelled():
            return

        message_blocks = [_block_to_message_block(block) for block in answer_blocks]
        for block in message_blocks:
            referenced_result = (
                block.result_id
                if isinstance(block, TableBlock)
                else block.chart.result_id
                if isinstance(block, ChartBlock)
                else None
            )
            if (
                referenced_result
                and get_owned_tool_result(
                    db,
                    referenced_result,
                    owner_id,
                    chat_id=chat_model.chat_id,
                    dataset_id=chat_model.dataset_id,
                )
                is None
            ):
                raise ValueError("Блок ответа ссылается на результат другого контекста")
            if isinstance(block, DashboardLinkBlock):
                dashboard = db.get(DashboardModel, block.dashboard_id)
                if (
                    dashboard is None
                    or dashboard.owner_id != owner_id
                    or dashboard.dataset_id != chat_model.dataset_id
                ):
                    raise ValueError("Блок ответа ссылается на дашборд другого контекста")
        result_ids = list(
            dict.fromkeys(result.result_id for result in results if result.status != "error")
        )
        assistant = AssistantMessage(
            message_id=_new_id("m"),
            chat_id=chat_model.chat_id,
            role="assistant",
            created_at=datetime.now(UTC),
            blocks=message_blocks,
            result_ids=result_ids,
            trace=trace,
        )
        budget.check()
        save_assistant_message(db, assistant, budget=budget)

        # Focus of the next turn: newly calculated results, explicit references to
        # previous ones, or the unchanged topic after a clarification.
        if result_ids:
            next_focus = result_ids
        elif stream_focus is not None:
            next_focus = stream_focus
        else:
            next_focus = previous_focus
        updated_messages = get_chat_messages(db, chat_model.chat_id)
        next_summary = (
            summarize_history([_history_entry(item) for item in updated_messages])
            or history_summary
        )
        budget.check()
        update_chat_memory(
            db,
            chat_model.chat_id,
            summary=next_summary,
            focus_result_ids=next_focus,
        )
        if diagnostics is not None:
            diagnostics.finish("success")

        for block in message_blocks:
            budget.check()
            yield SSEBlockEvent(data=block)
        budget.check()
        yield SSEDoneEvent(data=SSEDoneData(message_id=assistant.message_id))
    except TurnTimeout:
        if diagnostics is not None:
            diagnostics.finish("timeout", "TIMEOUT")
        yield turn_timeout_event()
    except TurnCancelled:
        if diagnostics is not None:
            diagnostics.finish("cancelled", "CANCELLED")
        return
    except Exception:  # The stream must end with an event, not a broken connection.
        if diagnostics is not None:
            diagnostics.finish("error", diagnostics.last_error or "TURN_EXCEPTION")
        logger.error("Chat turn failed (run_id=%s)", diagnostics.run_id if diagnostics else None)
        try:
            db.rollback()
        except Exception:
            pass
        yield SSEErrorEvent(
            data=SSEErrorData(
                message="Не удалось получить ответ агента",
                detail="Не удалось завершить запрос. Проверьте конфигурацию и повторите явно.",
            )
        )
    finally:
        if diagnostics is not None and not diagnostics.finished:
            diagnostics.finish("cancelled", "CANCELLED")
        resources.close()
