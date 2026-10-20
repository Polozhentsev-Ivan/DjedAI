"""Контракт Dashboard (§7.2) — сущность перевыполняемого дашборда."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from djedai.contracts.chart import ChartSpec
from djedai.contracts.tool_result import ToolError


class WidgetLayout(BaseModel):
    """Позиция и размеры виджета в сетке дашборда."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: int = Field(ge=0, description="Позиция X в сетке")
    y: int = Field(ge=0, description="Позиция Y в сетке")
    w: int = Field(ge=1, description="Ширина виджета в колонках")
    h: int = Field(ge=1, description="Высота виджета в строках")


class WidgetToolCall(BaseModel):
    """Рецепт перевычисления данных виджета."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(description="Имя инструмента, например 'time_aggregate'")
    params: dict[str, Any] = Field(default_factory=dict, description="Параметры вызова")


class WidgetRecipeStep(BaseModel):
    """Шаг пересчёта; зависимость ссылается на шаг рецепта, а не старый result_id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1)
    call: WidgetToolCall
    source_step_id: str | None = None


class WidgetRecipe(BaseModel):
    """Версионированный рецепт в порядке выполнения, без временных ID результата."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    steps: list[WidgetRecipeStep] = Field(min_length=1)
    output_step_id: str

    @model_validator(mode="after")
    def validate_dependencies(self) -> WidgetRecipe:
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError("step_id должны быть уникальны")
            if step.source_step_id is not None and step.source_step_id not in seen:
                raise ValueError("Зависимость должна ссылаться на предыдущий шаг")
            if step.call.params.get("source_result_id") is not None:
                raise ValueError("Рецепт хранит source_step_id вместо временного source_result_id")
            seen.add(step.step_id)
        if self.output_step_id not in seen:
            raise ValueError("output_step_id не найден в рецепте")
        return self


class DashboardWidget(BaseModel):
    """Виджет дашборда со схемой пересчета и визуализацией."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    widget_id: str = Field(description="Уникальный идентификатор виджета")
    tool_call: WidgetToolCall | None = Field(
        default=None, description="Независимый одиночный вызов; для цепочки используют recipe"
    )
    recipe: WidgetRecipe | None = None
    chart: dict[str, Any] | None = Field(
        default=None, description="ChartSpec без result_id — подставляется при рендере"
    )
    caption: str | None = Field(default=None, description="Краткий текстовый вывод или инсайт")
    layout: WidgetLayout = Field(description="Расположение на сетке")

    @model_validator(mode="after")
    def validate_recipe(self) -> DashboardWidget:
        if (self.tool_call is None) == (self.recipe is None):
            raise ValueError("Нужен ровно один из tool_call или recipe")
        if self.tool_call is not None and self.tool_call.params.get("source_result_id") is not None:
            raise ValueError("Зависимый вызов нужно сохранить как recipe")
        return self


class Dashboard(BaseModel):
    """Сущность дашборда, сохраняемая в БД приложения и доступная для обновления."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dashboard_id: str = Field(description="Уникальный идентификатор дашборда")
    dataset_id: str = Field(description="ID датасета, к которому привязан дашборд")
    title: str = Field(description="Название дашборда")
    description: str | None = Field(default=None, description="Описание дашборда")
    source_chat_id: str | None = Field(
        default=None, description="ID чата, в котором был создан дашборд"
    )
    widgets: list[DashboardWidget] = Field(default_factory=list, description="Список виджетов")
    created_at: AwareDatetime = Field(description="Дата создания")
    refreshed_at: AwareDatetime | None = Field(
        default=None, description="Дата последнего пересчета"
    )


class WidgetSnapshot(DashboardWidget):
    """Ответ refresh: рецепт вместе с результатом последнего запуска виджета."""

    chart: ChartSpec
    result_id: str | None
    status: Literal["ok", "error", "empty"]
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> WidgetSnapshot:
        if self.status == "error":
            if self.error is None or self.result_id is not None:
                raise ValueError("Ошибка виджета требует error и result_id=null")
        elif self.error is not None or self.result_id != self.chart.result_id:
            raise ValueError("Успешный виджет ссылается на актуальный result_id графика")
        return self


class DashboardSnapshot(Dashboard):
    """Отдельный wire-формат GET/refresh: сохранённый Dashboard и исход каждого виджета."""

    widgets: list[WidgetSnapshot]
