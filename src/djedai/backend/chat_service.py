"""Оркестрация хода чата: запуск агента, сохранение и SSE-события."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import duckdb
from fastapi import HTTPException, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from djedai.agent.config import get_agent_client
from djedai.agent.graph import build_agent_graph
from djedai.analytics.registry import execute_tool
from djedai.backend.dataset_service import DatasetContext
from djedai.backend.db.models import ChatModel, MessageModel
from djedai.backend.db.repositories import (
    get_chat as get_chat_record,
)
from djedai.backend.db.repositories import (
    get_chat_messages,
    save_assistant_message,
    save_message,
    save_tool_result,
)
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
    return replace(
        build_agent_runtime(),
        dataset_context=request.app.state.dataset_service.get_context,
        results_storage_dir=request.app.state.settings.results_data_dir,
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


def _history_message(message: MessageModel) -> Message:
    content = message.content or ""
    if not content and message.blocks:
        content = "\n".join(
            str(block.get("markdown", ""))
            for block in message.blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
    role = "user" if message.role == "user" else "assistant"
    return Message(role=role, content=content)


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


def stream_chat_turn(
    db: Session,
    chat: ChatModel | str,
    text: str,
    runtime: AgentRuntime,
    is_cancelled: Callable[[], bool] | None = None,
) -> Iterator[AgentStreamEvent]:
    """Сохраняет вопрос, выполняет граф агента и отдаёт события по мере готовности."""
    connection: duckdb.DuckDBPyConnection | None = None
    try:
        chat_model = (
            get_chat_record(db, chat)
            if isinstance(chat, str)
            else (get_chat_record(db, chat.chat_id) or chat)
        )
        if chat_model is None:
            yield SSEErrorEvent(
                data=SSEErrorData(
                    message="Не удалось получить ответ агента",
                    detail="Чат не найден",
                )
            )
            return

        if runtime.dataset_context is not None:
            context = runtime.dataset_context(chat_model.owner_id, chat_model.dataset_id)
            connection = duckdb.connect(str(context.duckdb_path), read_only=True)
            connection.execute("SET enable_external_access = false")

            def dispatch(tool: str, params: dict[str, Any]) -> ToolResult:
                if params.get("source_result_id") is not None:
                    raise ValueError("Вычисления поверх source_result_id будут доступны позднее")
                return execute_tool(tool, params, connection=connection)

            def catalog(dataset_id: str) -> DatasetCatalog:
                if dataset_id != context.catalog.dataset_id:
                    raise ValueError("Каталог не относится к датасету чата")
                return context.catalog

            runtime = replace(runtime, tool_dispatcher=dispatch, catalog_loader=catalog)

        if is_cancelled and is_cancelled():
            return
        user_message_id = _new_id("m")
        save_message(
            db,
            message_id=user_message_id,
            chat_id=chat_model.chat_id,
            role="user",
            content=text,
        )
        history = [
            _history_message(message) for message in get_chat_messages(db, chat_model.chat_id)[-10:]
        ]
        state: dict[str, Any] = {
            "chat_id": chat_model.chat_id,
            "dataset_id": chat_model.dataset_id,
            "messages": history,
        }

        configurable: dict[str, Any] = {"thread_id": chat_model.chat_id}
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
        started = perf_counter()
        previous = started

        yield _status_event("understand")

        for update in runtime.graph.stream(
            state,
            config={"configurable": configurable},
            stream_mode="updates",
        ):
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
                        if result.result_id in seen_results:
                            continue
                        seen_results.add(result.result_id)
                        results.append(result)
                        save_tool_result(
                            db,
                            result,
                            chat_id=chat_model.chat_id,
                            owner_id=chat_model.owner_id,
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

                if node in ("compose", "clarify", "verify"):
                    compose_update = data if isinstance(data, dict) else {}
                    if "answer_blocks" in compose_update:
                        answer_blocks = compose_update["answer_blocks"] or []

        error_detail = _agent_error(answer_blocks)
        if error_detail is not None:
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
        save_assistant_message(db, assistant)

        for block in message_blocks:
            yield SSEBlockEvent(data=block)
        yield SSEDoneEvent(data=SSEDoneData(message_id=assistant.message_id))
    except Exception:  # The stream must end with an event, not a broken connection.
        logger.exception("Chat turn failed")
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
        if connection is not None:
            connection.close()
