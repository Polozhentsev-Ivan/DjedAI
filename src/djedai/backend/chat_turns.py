"""Persist question outcomes without exposing diagnostic contents or replaying work."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from djedai.backend.db.models import MessageModel
from djedai.backend.db.session import budgeted_write
from djedai.contracts.chat import ChatTurnState
from djedai.execution import ExecutionBudget

logger = logging.getLogger(__name__)

STATUS_TEXT = {
    "load_ctx": "Загружаю контекст чата…",
    "understand": "Разбираю вопрос…",
    "plan": "Строю план анализа…",
    "execute": "Выполняю расчёты…",
    "visualize": "Подбираю визуализацию…",
    "compose": "Формулирую ответ…",
    "verify": "Проверяю числа в ответе…",
    "clarify": "Уточняю вопрос…",
}
ERRORS = {
    "model_timeout": ("timeout", "Модель не ответила за отведённое время"),
    "turn_timeout": ("timeout", "Время ожидания ответа истекло"),
    "model_unavailable": ("error", "Сервис модели временно недоступен"),
    "model_configuration": (
        "error",
        "Недопустимый профиль reasoning. Проверьте MODEL_REASONING_* и "
        "MODEL_REASONING_SUPPORTED для выбранной модели и endpoint; "
        "provider_default сохраняет режим провайдера.",
    ),
    "model_response_invalid": ("error", "Не удалось проверить ответ модели"),
    "agent_error": ("error", "Не удалось получить ответ агента"),
    "catalog_stale": (
        "error",
        "Схема источника изменилась после профилирования. Перепрофилируйте датасет и "
        "повторите вопрос",
    ),
    "cancelled": ("cancelled", "Получение ответа остановлено"),
    "unauthorized": ("error", "Сессия истекла. Войдите снова"),
    "stream_interrupted": ("interrupted", "Соединение прервано до конца ответа"),
}


def read_turn(db: Session, message_id: str) -> ChatTurnState | None:
    value = db.scalar(select(MessageModel.turn).where(MessageModel.message_id == message_id))
    return ChatTurnState.model_validate(value) if value else None


def apply_turn(db: Session, message_id: str, state: ChatTurnState) -> bool:
    """Compare-and-set inside the caller's transaction; terminal states never change."""
    result = db.execute(
        update(MessageModel)
        .where(
            MessageModel.message_id == message_id,
            MessageModel.role == "user",
            MessageModel.turn["run_id"].as_string() == state.run_id,
            MessageModel.turn["status"].as_string() == "running",
        )
        .values(turn=state.model_dump(mode="json"))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def write_turn(
    db: Session, message_id: str, state: ChatTurnState, budget: ExecutionBudget | None = None
) -> bool:
    # This small independent budget also permits saving an outcome after the
    # execution deadline, without waiting indefinitely behind another DB writer.
    budget = budget or ExecutionBudget(0.2)

    def write(writer: Session) -> bool:
        changed = apply_turn(writer, message_id, state)
        budget.check()
        writer.commit()
        return changed

    return budgeted_write(db, budget, write)


def terminal_error(state: ChatTurnState, code: str) -> ChatTurnState:
    status, message = ERRORS[code]
    return state.model_copy(
        update={
            "status": status,
            "code": code,
            "message": message,
            "suggestion": "Войдите снова, чтобы продолжить."
            if code == "unauthorized"
            else "Запрос не повторяется автоматически. Отправьте его снова, если нужно.",
            "finished_at": datetime.now(UTC),
            "phase": "stage",
        }
    )


def finish_error(db: Session, message_id: str, run_id: str, code: str) -> None:
    try:
        state = read_turn(db, message_id)
        if state is not None and state.run_id == run_id and state.status == "running":
            write_turn(db, message_id, terminal_error(state, code))
    except (SQLAlchemyError, TimeoutError):
        db.rollback()
        logger.error("Chat outcome persistence unavailable (run_id=%s)", run_id)


def recover_expired_turns(db: Session, chat_id: str) -> None:
    """An orphaned turn can expire after a crash; GET never resumes the graph."""
    rows = db.execute(
        select(MessageModel.message_id, MessageModel.turn).where(
            MessageModel.chat_id == chat_id,
            MessageModel.turn["status"].as_string() == "running",
        )
    ).all()
    for message_id, raw in rows:
        state = ChatTurnState.model_validate(raw)
        if state.deadline_at <= datetime.now(UTC):
            finish_error(db, message_id, state.run_id, "turn_timeout")
