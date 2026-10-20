"""Контракты API чатов."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from djedai.contracts.message import MessageBlock


class ChatCreate(BaseModel):
    """Запрос на создание нового чата по датасету."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(min_length=1, description="ID датасета, к которому относится чат")


class ChatCreated(BaseModel):
    """Ответ на создание чата."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chat_id: str = Field(description="ID созданного чата")


class ChatSummary(BaseModel):
    """Краткое описание чата для списка."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chat_id: str = Field(description="ID чата")
    dataset_id: str = Field(description="ID датасета")
    title: str = Field(description="Название чата")
    created_at: AwareDatetime = Field(description="Дата создания чата")


class ChatMessage(BaseModel):
    """Сообщение в истории чата: вопрос пользователя или ответ ассистента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(description="ID сообщения")
    role: Literal["user", "assistant"] = Field(description="Роль автора сообщения")
    created_at: AwareDatetime = Field(description="Дата создания сообщения")
    text: str | None = Field(default=None, description="Текст вопроса пользователя")
    blocks: list[MessageBlock] = Field(
        default_factory=list, description="Структурированные блоки ответа ассистента"
    )
    result_ids: list[str] = Field(
        default_factory=list, description="ID результатов вычислений, на которые опирается ответ"
    )


class ChatDetail(ChatSummary):
    """Чат с полной историей сообщений."""

    messages: list[ChatMessage] = Field(description="Сообщения чата в хронологическом порядке")


class SendMessageRequest(BaseModel):
    """Запрос на отправку вопроса в чат."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, description="Текст вопроса к данным")

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Текст вопроса не должен быть пустым")
        return cleaned
