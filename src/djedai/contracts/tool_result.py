"""Контракт ToolResult (§4.3) — универсальный результат выполнения аналитического инструмента."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolError(BaseModel):
    """Информация об ошибке выполнения инструмента."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(description="Код ошибки, например 'invalid_column' или 'timeout'")
    message: str = Field(description="Понятное пользователю сообщение об ошибке")
    suggestion: str | None = Field(default=None, description="Подсказка, как исправить")


class Fact(BaseModel):
    """Структурированный факт для передачи в LLM и верификации чисел."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(description="Текстовый факт для формирования ответа пользователю")
    kind: str = Field(description="Категория факта (например, 'metric', 'seasonal_peak')")
    values: dict[str, Any] = Field(
        default_factory=dict, description="Точные числовые значения для верификатора"
    )


class ChartHint(BaseModel):
    """Подсказка аналитической функции о подходящем типе визуализации."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(description="Рекомендуемый тип графика, например 'line', 'bar'")
    x: str | None = Field(default=None, description="Поле по оси X")
    y: list[str] = Field(default_factory=list, description="Поля по оси Y")
    color: str | None = Field(default=None, description="Поле разбивки")


class ToolResultData(BaseModel):
    """Табличные данные результата для прямого рендера фронтендом."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: list[str] = Field(default_factory=list, description="Имена колонок")
    rows: list[list[Any]] = Field(default_factory=list, description="Строки со значениями")
    truncated: bool = Field(default=False, description="Флаг усечения строк по лимиту")


class ToolResult(BaseModel):
    """Единый результат выполнения любого инструмента (B2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str = Field(description="Уникальный идентификатор вызова, например 'r_17'")
    status: Literal["ok", "error", "empty"] = Field(description="Статус выполнения инструмента")
    data: ToolResultData | None = Field(default=None, description="Табличные данные результата")
    data_ref: str | None = Field(
        default=None, description="Ссылка на сохранение в DuckDB/Parquet при больших объёмах"
    )
    facts: list[Fact] = Field(
        default_factory=list, description="Извлеченные факты для ответа и верификатора"
    )
    chart_hint: ChartHint | None = Field(default=None, description="Подсказка для графика")
    error: ToolError | None = Field(default=None, description="Ошибка, если status='error'")
    tool: str | None = Field(default=None, description="Имя вызванного инструмента")
    params: dict[str, Any] = Field(default_factory=dict, description="Параметры вызова")
    sql: str | None = Field(default=None, description="Выполненный SQL-запрос для «Как посчитано»")
    source_result_id: str | None = Field(
        default=None, description="ID исходного результата при цепочке расчётов"
    )
    duration_ms: int | None = Field(default=None, description="Время выполнения в миллисекундах")
