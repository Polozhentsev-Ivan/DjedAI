"""Контракт ToolResult (§4.3) — универсальный результат выполнения аналитического инструмента."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from djedai.contracts.metrics import MetricProvenance


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
    kind: str | None = Field(
        default=None, description="Категория факта (например, 'metric', 'seasonal_peak')"
    )
    values: dict[str, Any] = Field(
        default_factory=dict, description="Точные числовые значения для верификатора"
    )

    def __getitem__(self, item: str) -> Any:
        try:
            return getattr(self, item)
        except AttributeError as e:
            raise KeyError(item) from e

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and hasattr(self, item)


class ChartHint(BaseModel):
    """Подсказка аналитической функции о подходящем типе визуализации (§4.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(description="Рекомендуемый тип графика, например 'line', 'bar'")
    x: str | None = Field(default=None, description="Поле по оси X")
    y: list[str] = Field(default_factory=list, description="Поля по оси Y")
    color: str | None = Field(default=None, description="Поле разбивки")

    def __getitem__(self, item: str) -> Any:
        try:
            return getattr(self, item)
        except AttributeError as e:
            raise KeyError(item) from e

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and hasattr(self, item)


class ToolResultColumn(BaseModel):
    """Описание колонки в результате аналитического инструмента (§4.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Имя колонки")
    type: str | None = Field(
        default=None, description="Тип данных (например, 'date', 'money', 'number')"
    )
    label: str | None = Field(default=None, description="Человекочитаемая подпись колонки")

    def __getitem__(self, item: str) -> Any:
        try:
            return getattr(self, item)
        except AttributeError as e:
            raise KeyError(item) from e

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and hasattr(self, item)


class ToolResultData(BaseModel):
    """Табличные данные результата для прямого рендера фронтендом (§4.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: list[str | ToolResultColumn] = Field(
        default_factory=list, description="Имена или описания колонок ({name, type, label})"
    )
    rows: list[list[Any]] = Field(default_factory=list, description="Строки со значениями")
    row_count: int | None = Field(
        default=None, description="Полный объём результата (количество строк)"
    )
    truncated: bool = Field(default=False, description="Флаг усечения строк по лимиту")

    def __getitem__(self, item: str) -> Any:
        try:
            return getattr(self, item)
        except AttributeError as e:
            raise KeyError(item) from e

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and hasattr(self, item)


class ToolResult(BaseModel):
    """Единый результат выполнения любого инструмента (B2, §4.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str = Field(description="Уникальный идентификатор вызова, например 'r_17'")
    status: Literal["ok", "error", "empty"] = Field(description="Статус: 'ok', 'error' или 'empty'")
    data: ToolResultData | None = Field(default=None, description="Табличные данные результата")
    data_ref: str | None = Field(
        default=None, description="Ссылка на сохранение в DuckDB/Parquet при больших объёмах"
    )
    facts: list[Fact] = Field(
        default_factory=list, description="Извлеченные факты для ответа и верификатора"
    )
    chart_hint: ChartHint | None = Field(default=None, description="Подсказка для графика")
    error: ToolError | None = Field(default=None, description="Объект ошибки, если status='error'")
    tool: str | None = Field(default=None, description="Имя вызванного инструмента")
    params: dict[str, Any] | None = Field(
        default_factory=dict, description="Параметры, с которыми вызвали инструмент"
    )
    sql: str | None = Field(default=None, description="SQL-запрос, если он выполнялся")
    source_result_id: str | None = Field(
        default=None, description="ID исходного результата при цепочке расчётов"
    )
    duration_ms: int | None = Field(default=None, description="Время выполнения в миллисекундах")
    metric: MetricProvenance | None = Field(
        default=None, description="Версия определения метрики и применённые ограничения (B2-9)"
    )

    @model_validator(mode="after")
    def check_status_error_consistency(self) -> ToolResult:
        if self.status == "error" and self.error is None:
            raise ValueError("Если status='error', поле error обязательно должно быть заполнено.")
        if self.status != "error" and self.error is not None:
            raise ValueError("Поле error допускается только при status='error'.")
        return self
