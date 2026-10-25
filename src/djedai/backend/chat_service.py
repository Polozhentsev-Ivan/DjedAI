"""Оркестрация хода чата: запуск агента, сохранение и SSE-события."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from langgraph.config import get_stream_writer
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from djedai.agent.config import get_agent_client
from djedai.agent.diagnostics import RunDiagnostics, Tariff
from djedai.agent.graph import build_agent_graph
from djedai.analytics.metrics import MetricContext
from djedai.analytics.quality import assess
from djedai.analytics.registry import execute_tool
from djedai.backend.chat_turns import ERRORS, STATUS_TEXT, finish_error, write_turn
from djedai.backend.dataset_service import DatasetContext
from djedai.backend.db.models import ChatModel, DashboardModel, DatasetModel, MessageModel
from djedai.backend.db.repositories import (
    get_chat as get_chat_record,
)
from djedai.backend.db.repositories import (
    get_chat_messages,
    get_owned_tool_result,
    save_assistant_message,
    save_message,
    save_tool_result,
)
from djedai.backend.db.session import budgeted_write
from djedai.backend.diagnostics import DiagnosticJournal
from djedai.backend.metric_service import MetricService
from djedai.config import get_limits
from djedai.contracts.agent import Block, Message
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.chart import ChartSpec
from djedai.contracts.chat import ChatMessage, ChatSummary, ChatTurnState
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
from djedai.contracts.metrics import MetricDefinition
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
    metric_service: MetricService | None = None
    results_storage_dir: Path | str | None = None
    turn_timeout_seconds: float | None = None
    diagnostics: DiagnosticJournal | None = None
    tariff: Tariff | None = None
    unavailable: bool = False

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
    try:
        runtime = build_agent_runtime()
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        runtime = AgentRuntime(graph=None, unavailable=True)
    return replace(
        runtime,
        dataset_context=request.app.state.dataset_service.get_context,
        metric_service=getattr(request.app.state, "metric_service", None),
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
        turn=ChatTurnState.model_validate(message.turn) if message.turn else None,
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


def _metric_proposals(blocks: list[Block]) -> list[tuple[str, MetricDefinition]]:
    """Пары «текст кнопки — кандидат» из уточнений, которые чат покажет кнопками."""
    pairs = []
    for block in blocks:
        if block.type != "clarification" or not isinstance(
            _block_to_message_block(block), ClarificationBlock
        ):
            continue
        for option, candidate in zip(block.options, block.metric_candidates, strict=False):
            if candidate is not None and option.strip():
                pairs.append((option.strip(), candidate))
    return pairs


def _with_chosen_metric(history: list[Message], chosen: MetricDefinition) -> list[Message]:
    """Ответ кнопкой — не вопрос: агенту нужен исходный вопрос и выбранное определение."""
    if not history:
        return history
    question = next(
        (m.content for m in reversed(history[:-1]) if m.role == "user" and m.content), ""
    )
    answer = history[-1].content
    content = (
        f"{question}\nУточнение пользователя: {answer}. Определение подтверждено: "
        f"метрика {chosen.metric_id} v{chosen.version}, считать через compute_metric."
    )
    return [*history[:-1], Message(role="user", content=content.strip())]


def _agent_error(blocks: list[Block]) -> str | None:
    for block in blocks:
        if block.type == "error":
            return block.markdown or "Агент не смог обработать запрос"
    return None


def _status_event(state: ChatTurnState, message_id: str) -> SSEStatusEvent:
    return SSEStatusEvent(
        data=SSEStatusData(
            node=state.node,
            text=state.message,
            phase=state.phase,
            message_id=message_id,
            run_id=state.run_id,
        )
    )


def turn_timeout_event() -> SSEErrorEvent:
    return _error_event("turn_timeout")


def _error_event(code: str) -> SSEErrorEvent:
    return SSEErrorEvent(
        data=SSEErrorData(
            code=code,
            message=ERRORS[code][1],
            detail="Запрос не повторяется автоматически; повторите явно, если нужно.",
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
    question_id: str | None = None,
    run_id: str | None = None,
    cancellation_code: Callable[[], str] | None = None,
) -> Iterator[AgentStreamEvent]:
    """Сохраняет вопрос, выполняет граф агента и отдаёт события по мере готовности."""
    resources = ExitStack()
    budget = budget or runtime.budget(is_cancelled)
    diagnostics: RunDiagnostics | None = None
    user_message_id = question_id or _new_id("m")
    run_id = run_id or f"run_{uuid4().hex}"
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

        def observe(event: dict[str, Any]) -> None:
            if runtime.diagnostics is not None:
                runtime.diagnostics(event)
            kind = event["event"]
            if kind in {"stage_started", "model_retry_wait", "model_retry_started"}:
                try:
                    get_stream_writer()(
                        {
                            "node": event["node"],
                            "phase": "retry" if kind == "model_retry_wait" else "stage",
                        }
                    )
                except RuntimeError:
                    # Direct graph adapters used in tests may have no LangGraph writer.
                    pass

        diagnostics = RunDiagnostics(
            observe,
            owner_id=owner_id,
            chat_id=chat_model.chat_id,
            dataset_id=chat_model.dataset_id,
            user_message_id=user_message_id,
            tariff=runtime.tariff,
            run_id=run_id,
        )
        started_at = datetime.now(UTC)
        turn = ChatTurnState(
            run_id=run_id,
            status="running",
            node="load_ctx",
            message=STATUS_TEXT["load_ctx"],
            started_at=started_at,
            deadline_at=started_at + timedelta(seconds=budget.remaining),
        )

        def accept(writer: Session) -> MessageModel:
            return save_message(
                writer,
                message_id=user_message_id,
                chat_id=chat_model.chat_id,
                role="user",
                content=text,
                turn=turn.model_dump(mode="json"),
                before_commit=budget.check,
            )

        budgeted_write(db, budget, accept)
        yield _status_event(turn, user_message_id)
        if runtime.unavailable:
            finish_error(db, user_message_id, run_id, "model_unavailable")
            diagnostics.finish("error", "CONFIGURATION_ERROR")
            yield _error_event("model_unavailable")
            return

        # Выбор варианта уточнения подтверждает приложенное к нему определение (B2-9).
        chosen: MetricDefinition | None = None
        metrics: dict[str, MetricDefinition] = {}
        if runtime.metric_service is not None:
            budget.check()
            chosen = runtime.metric_service.confirm_choice(
                owner_id, chat_model.dataset_id, chat_model.chat_id, text, user_message_id
            )
            metrics = runtime.metric_service.confirmed(owner_id, chat_model.dataset_id)

        if runtime.dataset_context is not None:
            budget.check()
            if is_cancelled and is_cancelled():
                return
            context = runtime.dataset_context(owner_id, chat_model.dataset_id)
            if context.catalog.dataset_id != chat_model.dataset_id:
                raise ValueError("Каталог не относится к датасету чата")
            connection = resources.enter_context(context.open_connection())
            allowed_tables = frozenset(table.table_id for table in context.catalog.tables)
            semantics = MetricContext(context.catalog, metrics)

            def dispatch(tool: str, params: dict[str, Any]) -> ToolResult:
                if params.get("source_result_id") is not None:
                    raise ValueError("Вычисления поверх source_result_id будут доступны позднее")
                result = execute_tool(
                    tool,
                    params,
                    connection=connection,
                    allowed_tables=allowed_tables,
                    semantics=semantics,
                )
                # Потери типизации в колонках именно этого расчёта (B2-10).
                return assess(result, connection, context.catalog)

            def catalog(dataset_id: str) -> DatasetCatalog:
                if dataset_id != context.catalog.dataset_id:
                    raise ValueError("Каталог не относится к датасету чата")
                return context.catalog

            runtime = replace(runtime, tool_dispatcher=dispatch, catalog_loader=catalog)

        if is_cancelled and is_cancelled():
            return
        budget.check()
        history = [
            _history_message(message) for message in get_chat_messages(db, chat_model.chat_id)[-10:]
        ]
        if chosen is not None:
            history = _with_chosen_metric(history, chosen)
        state: dict[str, Any] = {
            "chat_id": chat_model.chat_id,
            "dataset_id": chat_model.dataset_id,
            "messages": history,
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
        if runtime.metric_service is not None:
            configurable["metrics_loader"] = lambda _dataset_id: metrics

        trace: list[TraceStep] = []
        results: list[ToolResult] = []
        seen_results: set[str] = set()
        answer_blocks: list[Block] = []
        started = perf_counter()
        previous = started

        for item in runtime.graph.stream(
            state,
            config={"configurable": configurable},
            stream_mode=["custom", "updates"],
        ):
            budget.check()
            if is_cancelled and is_cancelled():
                return
            mode, update = item if isinstance(item, tuple) else ("updates", item)
            if mode == "custom":
                node = update.get("node")
                if node not in STATUS_TEXT:
                    continue
                phase = update.get("phase", "stage")
                turn = turn.model_copy(
                    update={
                        "node": node,
                        "phase": phase,
                        "message": "Ожидаю повторного запроса к модели…"
                        if phase == "retry"
                        else STATUS_TEXT[node],
                    }
                )
                if not write_turn(db, user_message_id, turn, budget):
                    raise TurnCancelled
                yield _status_event(turn, user_message_id)
                continue
            now = perf_counter()
            for node, data in update.items():
                trace.append(TraceStep(node=node, ms=int((now - previous) * 1000)))
                previous = now
                if node in ("understand", "load_ctx"):
                    continue

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

                if node in ("compose", "clarify", "verify", "cite_metrics"):
                    compose_update = data if isinstance(data, dict) else {}
                    if "answer_blocks" in compose_update:
                        answer_blocks = compose_update["answer_blocks"] or []

        budget.check()
        error_detail = _agent_error(answer_blocks)
        if error_detail is not None:
            error_type = diagnostics.last_error or "AGENT_ERROR"
            code = (
                "model_timeout"
                if error_type == "TIMEOUT"
                else "model_unavailable"
                if error_type == "SERVER_ERROR"
                else "model_response_invalid"
                if error_type in {"INVALID_JSON", "SCHEMA_MISMATCH", "OUTPUT_LIMIT"}
                else "agent_error"
            )
            finish_error(db, user_message_id, run_id, code)
            diagnostics.finish("timeout" if code == "model_timeout" else "error", error_type)
            yield _error_event(code)
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
        completed_turn = turn.model_copy(
            update={
                "status": "success",
                "message": "Ответ готов",
                "finished_at": datetime.now(UTC),
                "assistant_message_id": assistant.message_id,
                "phase": "stage",
            }
        )
        proposals = _metric_proposals(answer_blocks) if runtime.metric_service else []
        save_assistant_message(
            db,
            assistant,
            budget=budget,
            question_id=user_message_id,
            completed_turn=completed_turn,
            also=(
                lambda writer: runtime.metric_service.propose(
                    writer, owner_id, chat_model.dataset_id, assistant.message_id, proposals
                )
            )
            if proposals
            else None,
        )
        if diagnostics is not None:
            diagnostics.finish("success")

        for block in message_blocks:
            budget.check()
            yield SSEBlockEvent(data=block)
        budget.check()
        yield SSEDoneEvent(data=SSEDoneData(message_id=assistant.message_id))
    except TurnTimeout:
        finish_error(db, user_message_id, run_id, "turn_timeout")
        if diagnostics is not None:
            diagnostics.finish("timeout", "TIMEOUT")
        yield turn_timeout_event()
    except TurnCancelled:
        finish_error(
            db, user_message_id, run_id, cancellation_code() if cancellation_code else "cancelled"
        )
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
        finish_error(db, user_message_id, run_id, "agent_error")
        yield _error_event("agent_error")
    finally:
        finish_error(
            db,
            user_message_id,
            run_id,
            cancellation_code() if cancellation_code else "stream_interrupted",
        )
        if diagnostics is not None and not diagnostics.finished:
            diagnostics.finish("cancelled", "CANCELLED")
        resources.close()
