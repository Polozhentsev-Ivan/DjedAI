from typing import Any

from pydantic import BaseModel, Field


# 1. Описываем параметры для функции run_sql
class RunSqlParams(BaseModel):
    sql: str = Field(description="SQL-запрос (только SELECT) для получения данных.")
    source_result_id: str | None = Field(
        default=None,
        description="Если нужно сделать запрос поверх "
        "другого результата (например, поверх графика).",
    )


# 2. Описываем параметры для describe_dataset
class DescribeDatasetParams(BaseModel):
    pass  # У этой функции нет параметров, она просто показывает, что в базе


# 3. Класс для хранения самого инструмента
class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]


# 4. СОЗДАЕМ РЕЕСТР ИНСТРУМЕНТОВ
# Сюда мы будем добавлять все функции по мере их написания
TOOLS_REGISTRY = [
    ToolDefinition(
        name="run_sql",
        description="Выполняет безопасный SQL-запрос (только SELECT) "
        "для получения данных из таблиц.",
        parameters_schema=RunSqlParams.model_json_schema(),
    ),
    ToolDefinition(
        name="describe_dataset",
        description="Показывает общую информацию о загруженных "
        "данных (список таблиц, колонок и связи).",
        parameters_schema=DescribeDatasetParams.model_json_schema(),
    ),
]


def get_tools_for_llm():
    """Возвращает список инструментов для передачи в нейросеть (MLE)."""
    return TOOLS_REGISTRY
