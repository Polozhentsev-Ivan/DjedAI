"""Контракт AssistantMessage и блоков сообщений чата (§5.6)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from djedai.contracts.chart import ChartSpec


class TextBlock(BaseModel):
    """Текстовый блок в формате Markdown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text"] = "text"
    markdown: str = Field(description="Текст ответа на понятном русском языке")


class ChartBlock(BaseModel):
    """Блок с декларативным описанием графика."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["chart"] = "chart"
    chart: ChartSpec = Field(description="Спецификация графика")


class TableBlock(BaseModel):
    """Табличный блок со ссылкой на результат вычислений."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["table"] = "table"
    result_id: str = Field(description="ID результата, из которого берутся строки")
    columns: list[str] = Field(default_factory=list, description="Отображаемые колонки")
    limit: int = Field(default=20, ge=1, description="Лимит строк")


class ClarificationBlock(BaseModel):
    """Блок уточняющего вопроса с вариантами выбора при неоднозначности (T8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["clarification"] = "clarification"
    question: str = Field(description="Уточняющий вопрос пользователю")
    options: list[str] = Field(
        min_length=2, description="Варианты ответа в виде кнопок (минимум 2)"
    )

    @field_validator("options")
    @classmethod
    def validate_options(cls, v: list[str]) -> list[str]:
        cleaned = [opt.strip() for opt in v if opt.strip()]
        if len(cleaned) < 2:
            raise ValueError("Должно быть минимум 2 непустых варианта ответа")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Варианты ответа не должны повторяться")
        return v


class DashboardLinkBlock(BaseModel):
    """Ссылка на созданный или предложенный дашборд."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["dashboard_link"] = "dashboard_link"
    dashboard_id: str = Field(description="ID дашборда")
    title: str = Field(description="Название дашборда")


class SuggestionsBlock(BaseModel):
    """Подсказки для следующего шага анализа."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["suggestions"] = "suggestions"
    items: list[str] = Field(
        default_factory=list, description="Тексты предложений следующих вопросов"
    )


MessageBlock = Annotated[
    TextBlock
    | ChartBlock
    | TableBlock
    | ClarificationBlock
    | DashboardLinkBlock
    | SuggestionsBlock,
    Field(discriminator="type"),
]


class TraceStep(BaseModel):
    """Шаг трассировки рассуждения агента (для стриминга и отладки)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str = Field(description="Имя узла графа LangGraph (understand, plan, execute...)")
    tool: str | None = Field(default=None, description="Вызванный инструмент, если был")
    ms: int | None = Field(default=None, description="Время выполнения шага")


class AssistantMessage(BaseModel):
    """Сообщение ассистента в чате со структурированными блоками и аудитом вычислений."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(description="Уникальный идентификатор сообщения")
    chat_id: str = Field(description="ID чата, к которому относится сообщение")
    role: Literal["assistant"] = "assistant"
    created_at: AwareDatetime = Field(description="Время создания сообщения")
    blocks: list[MessageBlock] = Field(
        default_factory=list, description="Структурированные блоки ответа"
    )
    result_ids: list[str] = Field(
        default_factory=list, description="ID результатов вычислений (provenance: «как посчитано»)"
    )
    trace: list[TraceStep] = Field(
        default_factory=list, description="Шаги графа агента при формировании ответа"
    )
