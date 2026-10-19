from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ToolError(BaseModel):
    """Модель ошибки, если инструмент не смог выполниться."""

    code: str = Field(description="Код ошибки (например, 'invalid_column')")
    message: str = Field(description="Текст ошибки для пользователя")
    suggestion: str | None = Field(default=None, description="Подсказка, как исправить")


class ToolResult(BaseModel):
    """Универсальный контракт ответа любого аналитического инструмента."""

    result_id: str = Field(description="Уникальный ID результата, например 'r_17'")
    status: Literal["ok", "error", "empty"] = Field(description="Статус: 'ok', 'error' или 'empty'")

    data: dict[str, Any] | None = Field(default=None, description="Данные (колонки и строки)")
    facts: list[dict[str, Any]] = Field(
        default_factory=list, description="Факты для нейросети (текст + значения)"
    )
    chart_hint: dict[str, Any] | None = Field(default=None, description="Подсказка для графика")

    error: ToolError | None = Field(default=None, description="Объект ошибки, если status='error'")

    tool: str | None = Field(default=None, description="Имя инструмента")
    params: dict[str, Any] | None = Field(
        default=None, description="Параметры, с которыми вызвали инструмент"
    )
    sql: str | None = Field(default=None, description="SQL-запрос, если он выполнялся")

    source_result_id: str | None = Field(
        default=None, description="ID результата, на основе которого сделан этот"
    )

    @model_validator(mode="after")
    def check_status_error_consistency(self) -> "ToolResult":
        if self.status == "error" and self.error is None:
            raise ValueError("Если status='error', поле error обязательно должно быть заполнено.")
        if self.status != "error" and self.error is not None:
            raise ValueError("Поле error допускается только при status='error'.")
        return self
