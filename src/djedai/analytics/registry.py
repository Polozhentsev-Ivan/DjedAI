from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class RunSqlParams(BaseModel):
    """Модель параметров для функции run_sql."""

    sql: str = Field(description="SQL-запрос (только SELECT) для получения данных.")
    source_result_id: str | None = Field(
        default=None,
        description="ID предыдущего результата, если нужно сделать запрос "
        "поверх уже посчитанных данных.",
    )


class DescribeDatasetParams(BaseModel):
    """Модель параметров для describe_dataset. У этой функции нет параметров."""

    model_config = ConfigDict(json_schema_extra={"examples": [{}]})


class DescribeColumnParams(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"table": "test", "column": "age"}]})

    table: str = Field(description="Имя таблицы")
    column: str = Field(description="Имя колонки")


class ValueCountsParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"table": "test", "column": "city", "top_n": 3, "filter": "age > 25"}]
        }
    )

    table: str = Field(description="Имя таблицы")
    column: str = Field(description="Имя колонки")
    top_n: int = Field(default=10, description="Количество топ-значений")
    filter: str | None = Field(
        default=None, description="Опциональный фильтр (SQL WHERE). Например: 'age > 25'"
    )


class ToolDefinition(BaseModel):
    """Внутреннее описание инструмента с моделью для валидации параметров."""

    name: str
    description: str
    parameters_model: type[BaseModel]

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self.parameters_model.model_json_schema()


class LLMToolDefinition(TypedDict):
    """JSON-совместимое описание инструмента для нейросети (MLE)."""

    name: str
    description: str
    parameters_schema: dict[str, Any]


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
    ToolDefinition(
        name="describe_column",
        description="Показывает статистику по конкретной колонке (пропуски, уникальные значения).",
        parameters_model=DescribeColumnParams,
    ),
    ToolDefinition(
        name="value_counts",
        description="Показывает самые частые значения в колонке.",
        parameters_model=ValueCountsParams,
    ),
]


def get_tools_for_llm() -> list[LLMToolDefinition]:
    """Возвращает список инструментов для передачи в нейросеть (MLE)."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters_schema": tool.parameters_schema,
        }
        for tool in TOOLS_REGISTRY
    ]
