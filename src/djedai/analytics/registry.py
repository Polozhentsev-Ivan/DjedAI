from collections.abc import Callable, Collection
from typing import Any, Literal, TypedDict

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from djedai.analytics.tools import (
    describe_column,
    describe_dataset,
    detect_anomalies,
    find_duplicates,
    run_sql,
    time_aggregate,
    value_counts,
)
from djedai.contracts.tool_result import ToolError, ToolResult


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


class TimeAggregateParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "table": "events",
                    "time_col": "ts",
                    "freq": "day",
                    "agg": "sum",
                    "measure": "amount",
                },
                {
                    "table": "events",
                    "time_col": "ts",
                    "freq": "month",
                    "agg": "count",
                    "group_by": ["city"],
                },
            ]
        }
    )

    table: str = Field(description="Имя таблицы")
    time_col: str = Field(description="Колонка DATE или TIMESTAMP")
    measure: str | None = Field(
        default=None,
        description="Колонка метрики (обязательна при agg != 'count')",
    )
    agg: Literal["count", "sum", "avg", "min", "max", "median", "stddev"] = Field(
        default="count",
        description="Функция агрегации",
    )
    freq: Literal["minute", "hour", "day", "week", "month", "quarter", "year"] = Field(
        default="day",
        description="Размер временного бакета",
    )
    group_by: list[str] | None = Field(
        default=None,
        description="Дополнительные колонки группировки",
    )
    filter: str | None = Field(
        default=None,
        description="SQL WHERE без префиксов таблиц, напр. \"city = 'Москва'\"",
    )
    top_n: int | None = Field(
        default=None,
        description="Максимум бакетов в результате",
    )


class FindDuplicatesParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                # полные дубли строк (columns не указан)
                {"table": "events"},
                # частичные дубли по конкретным полям
                {"table": "test", "columns": ["city", "age"]},
                # частичные + прочие колонки
                {"table": "events", "columns": ["ts", "city"], "include_all_columns": True},
            ]
        }
    )

    table: str = Field(description="Имя таблицы")
    columns: list[str] | None = Field(
        default=None,
        description="Колонки дубля. Не указано — ищем полные дубли строк (все колонки).",
    )
    filter: str | None = Field(default=None, description="SQL WHERE")
    include_all_columns: bool = Field(
        default=False,
        description="Показать прочие колонки первой строки группы (только если columns задан)",
    )
    limit: int = Field(default=100, description="Максимум групп дублей", ge=1)


class DetectAnomaliesParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                # точечный выброс
                {"table": "events", "measure": "amount", "method": "iqr"},
                # жёстче порог
                {"table": "events", "measure": "amount", "method": "zscore", "threshold": 2.5},
                # по группам
                {"table": "events", "measure": "amount", "method": "mad", "group_by": ["city"]},
                # аномалии во временном ряду
                {
                    "table": "events",
                    "measure": "amount",
                    "method": "stl-residual",
                    "time_col": "ts",
                    "freq": "month",
                },
            ]
        }
    )

    table: str = Field(description="Имя таблицы")
    measure: str = Field(description="Числовая колонка для проверки на выбросы")
    method: Literal["iqr", "zscore", "mad", "stl-residual"] = Field(
        default="iqr",
        description=(
            "iqr=1.5·IQR, zscore=3σ, mad=3.5·1.4826·MAD "
            "(точечные); stl-residual — остатки STL-декомпозиции "
            "временного ряда, нужен time_col."
        ),
    )
    time_col: str | None = Field(
        default=None,
        description="Колонка DATE/TIMESTAMP. Обязательна для method='stl-residual'.",
    )
    freq: Literal["minute", "hour", "day", "week", "month", "quarter", "year"] = Field(
        default="day",
        description="Частота агрегации для stl-residual (остальные методы игнорируют).",
    )
    threshold: float | None = Field(
        default=None,
        description="Порог. По умолчанию: 1.5 / 3.0 / 3.5 / 3.0 для iqr"
        " / zscore / mad / stl-residual.",
    )
    group_by: list[str] | None = Field(
        default=None,
        description="Колонки группировки",
    )
    filter: str | None = Field(default=None, description="SQL WHERE")
    id_column: str | None = Field(
        default=None,
        description="Колонка-идентификатор строки",
    )
    limit: int = Field(default=100, description="Максимум аномалий", ge=1)


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
    ToolDefinition(
        name="time_aggregate",
        description=(
            "Агрегирует числовую метрику по временным бакетам "
            "(минута/час/день/неделя/месяц/квартал/год), опционально — "
            "с группировкой. Аналог GROUP BY date_trunc(...)."
        ),
        parameters_model=TimeAggregateParams,
    ),
    ToolDefinition(
        name="find_duplicates",
        description=(
            "Находит группы дублей по выбранным полям. "
            "Возвращает комбинацию значений, количество повторов и "
            "опционально прочие поля первой строки группы."
        ),
        parameters_model=FindDuplicatesParams,
    ),
    ToolDefinition(
        name="detect_anomalies",
        description=(
            "Ищет выбросы в числовой колонке. Точечные методы: "
            "iqr (Тьюки, 1.5·IQR), zscore (3σ), mad (3.5·1.4826·MAD). "
            "Для временных рядов — stl-residual (остатки STL-декомпозиции, "
            "нужен time_col и freq). Поддерживает группировку и id_column."
        ),
        parameters_model=DetectAnomaliesParams,
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


TOOL_FUNCTIONS: dict[str, Callable[..., ToolResult]] = {
    "run_sql": run_sql,
    "describe_dataset": describe_dataset,
    "describe_column": describe_column,
    "value_counts": value_counts,
    "time_aggregate": time_aggregate,
    "find_duplicates": find_duplicates,
    "detect_anomalies": detect_anomalies,
}


def execute_tool(
    tool_name: str,
    params: dict[str, Any],
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
) -> ToolResult:
    """Валидирует параметры и выполняет инструмент из реестр."""
    definition = next((tool for tool in TOOLS_REGISTRY if tool.name == tool_name), None)
    if definition is None:
        raise ValueError(f"Инструмент '{tool_name}' не зарегистрирован")
    if params.get("source_result_id") is not None:
        raise ValueError("Вычисления поверх source_result_id будут доступны позднее")

    validated = definition.parameters_model.model_validate(params)
    arguments = validated.model_dump(exclude_none=True)
    # source_result_id относится к будущей работе поверх сохранённого результата,
    # а run_sql принимает только SQL и лимит строк.
    arguments.pop("source_result_id", None)
    if allowed_tables is not None:
        table = arguments.get("table")
        if table is not None and table not in allowed_tables:
            raise ValueError("Таблица не относится к выбранному датасету")
        arguments["allowed_tables"] = allowed_tables
    if connection is not None:
        arguments["connection"] = connection
    result = TOOL_FUNCTIONS[tool_name](**arguments)
    if (
        allowed_tables is not None
        and result.error
        and result.error.code in {"database_error", "db_error", "sql_parse_error"}
    ):
        # Driver errors may include attached server names, paths or credentials.
        result = result.model_copy(
            update={
                "error": ToolError(
                    code=result.error.code,
                    message="Не удалось выполнить запрос к выбранному датасету.",
                    suggestion="Проверьте поля и запрос по каталогу датасета.",
                )
            }
        )
    return result
