from typing import Any

from pydantic import BaseModel, Field


class ToolError(BaseModel):
    """
    Error class for tool results.
    """

    code: str = Field(description="Код ошибки (например, 'invalid_column')")
    message: str = Field(description="Текст ошибки для пользователя")
    suggestion: str | None = Field(default=None, description="Подсказка, как исправить")


class ToolResult(BaseModel):
    """
    Universal contract for the response of any analytical tool.
    """

    result_id: str = Field(description="Уникальный ID результата, например 'r_17'")
    status: str = Field(description="Статус: 'ok', 'error' или 'empty'")

    # Данные и факты
    data: dict[str, Any] | None = Field(default=None, description="Данные (колонки и строки)")
    facts: list[dict[str, Any]] = Field(
        default_factory=list, description="Факты для нейросети (текст + значения)"
    )
    chart_hint: dict[str, Any] | None = Field(default=None, description="Подсказка для графика")

    # Ошибка
    error: ToolError | None = Field(default=None, description="Объект ошибки, если status='error'")

    # Метаданные
    tool: str | None = Field(default=None, description="Имя инструмента")
    params: dict[str, Any] | None = Field(
        default=None, description="Параметры, с которыми вызвали инструмент"
    )
    sql: str | None = Field(default=None, description="SQL-запрос, если он выполнялся")

    # Связь с предыдущими результатами (для задачи B2-3 и далее)
    source_result_id: str | None = Field(
        default=None, description="ID результата, на основе которого сделан этот"
    )
