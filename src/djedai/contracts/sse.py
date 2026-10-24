"""Контракты Server-Sent Events (SSE) для стриминга агента и фоновых задач (§7.3)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from djedai.contracts.message import MessageBlock


class SSEStatusData(BaseModel):
    """Данные события изменения статуса узла графа агента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str = Field(description="Узел графа агента (understand, plan, execute, compose, verify)")
    text: str = Field(description="Человекочитаемый статус текущего действия на русском языке")
    message_id: str | None = Field(default=None, description="ID сохранённого вопроса пользователя")
    run_id: str | None = Field(default=None, description="ID хода для диагностики")
    phase: Literal["stage", "retry"] = "stage"


class SSEToolCallData(BaseModel):
    """Данные события вызова инструмента агентом."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(description="Название вызванного инструмента")
    params: dict[str, Any] = Field(default_factory=dict, description="Параметры вызова инструмента")


class SSEToolResultData(BaseModel):
    """Данные события завершения выполнения инструмента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str = Field(description="Идентификатор сохраненного результата")
    ok: bool = Field(description="Признак успешного завершения инструмента")
    error: str | None = Field(
        default=None, description="Сообщение об ошибке, если вызов завершился сбоем"
    )


class SSEDoneData(BaseModel):
    """Данные события успешного завершения генерации ответа."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(description="Идентификатор сформированного сообщения ассистента")


class SSEErrorData(BaseModel):
    """Данные события ошибки при обработке сообщения."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = Field(description="Понятное описание возникшей ошибки на русском языке")
    detail: str | None = Field(default=None, description="Технические детали ошибки")
    code: str | None = Field(default=None, description="Код ошибки, в том числе unauthorized")


class SSEStatusEvent(BaseModel):
    """Событие смены статуса агента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["status"] = "status"
    data: SSEStatusData


class SSEToolCallEvent(BaseModel):
    """Событие вызова инструмента агентом."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["tool_call"] = "tool_call"
    data: SSEToolCallData


class SSEToolResultEvent(BaseModel):
    """Событие результата вызова инструмента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["tool_result"] = "tool_result"
    data: SSEToolResultData


class SSEBlockEvent(BaseModel):
    """Событие передачи готового блока ответа ассистента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["block"] = "block"
    data: MessageBlock


class SSEDoneEvent(BaseModel):
    """Событие завершения стриминга ответа."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["done"] = "done"
    data: SSEDoneData


class SSEErrorEvent(BaseModel):
    """Событие ошибки обработки."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["error"] = "error"
    data: SSEErrorData


AgentStreamEvent = Annotated[
    SSEStatusEvent
    | SSEToolCallEvent
    | SSEToolResultEvent
    | SSEBlockEvent
    | SSEDoneEvent
    | SSEErrorEvent,
    Field(discriminator="event"),
]


# События профилирования датасета (/api/datasets/{id}/events)


class DatasetProgressData(BaseModel):
    """Данные о прогрессе фонового профилирования датасета."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str = Field(description="Текущая стадия обработки датасета")
    progress: float = Field(ge=0.0, le=1.0, description="Доля завершения от 0.0 до 1.0")
    message: str | None = Field(default=None, description="Сообщение о выполняемом действии")


class DatasetProgressEvent(BaseModel):
    """Событие прогресса профилирования датасета."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["progress"] = "progress"
    data: DatasetProgressData


class DatasetDoneData(BaseModel):
    """Данные об успешном завершении профилирования датасета."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(description="Идентификатор профилированного датасета")


class DatasetDoneEvent(BaseModel):
    """Событие успешного завершения профилирования датасета."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["done"] = "done"
    data: DatasetDoneData


class DatasetErrorEvent(BaseModel):
    """Событие ошибки фонового профилирования датасета."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["error"] = "error"
    data: SSEErrorData


DatasetStreamEvent = Annotated[
    DatasetProgressEvent | DatasetDoneEvent | DatasetErrorEvent,
    Field(discriminator="event"),
]
