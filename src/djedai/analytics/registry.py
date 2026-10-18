from typing import Any

from pydantic import BaseModel, Field


class RunSqlParams(BaseModel):
    """Модель параметров для функции run_sql."""
    sql: str = Field(description="SQL-запрос (только SELECT) для получения данных.")
    source_result_id: str | None = Field(
        default=None,
        description="ID предыдущего результата, если нужно сделать запрос поверх уже посчитанных данных.",
    )


class DescribeDatasetParams(BaseModel):
    """Модель параметров для describe_dataset. У этой функции нет параметров."""
    pass


class ToolDefinition(BaseModel):
    """Описание инструмента для нейросети (MLE)."""
    name: str
    description: str
    # Теперь мы храним саму модель (класс), а не просто словарь схемы
    parameters_model: type[BaseModel]

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """Автоматически генерирует JSON-схему из модели."""
        return self.parameters_model.model_json_schema()


TOOLS_REGISTRY = [
    ToolDefinition(
        name="run_sql",
        description="Выполняет безопасный SQL-запрос (только SELECT) для получения данных.",
        parameters_model=RunSqlParams,
    ),
    ToolDefinition(
        name="describe_dataset",
        description="Показывает общую информацию о загруженных данных (список таблиц, колонок).",
        parameters_model=DescribeDatasetParams,
    ),
]


def get_tools_for_llm():
    """Возвращает список инструментов для передачи в нейросеть (MLE)."""
    return TOOLS_REGISTRY