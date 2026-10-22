from collections.abc import Callable
from typing import Any, Literal, TypedDict

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from djedai.analytics.tools import (
    compare_segments,
    describe_column,
    describe_dataset,
    detect_anomalies,
    find_duplicates,
    run_sql,
    seasonality,
    segment_profile,
    time_aggregate,
    value_counts,
)
from djedai.contracts.tool_result import ToolResult


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


class SeasonalityParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "table": "events",
                    "time_col": "ts",
                    "measure": "amount",
                    "freq": "month",
                    "period": 12,
                    "agg": "sum",
                },
                {"table": "events", "time_col": "ts", "agg": "count", "freq": "day", "period": 7},
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
        default="sum",
        description="Функция агрегации",
    )
    freq: Literal["minute", "hour", "day", "week", "month", "quarter", "year"] = Field(
        default="month",
        description="Частота агрегации",
    )
    period: int | None = Field(
        default=None,
        description=(
            "Период сезонности (>= 2). По умолчанию: 60/min, 24/hour, "
            "7/day, 52/week, 12/month, 4/quarter, 1/year."
        ),
    )
    filter: str | None = Field(default=None, description="SQL WHERE")


class SegmentProfileParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"table": "events", "group_by": ["city"], "measures": ["amount"]},
                {"table": "test", "group_by": ["city"]},
            ]
        }
    )

    table: str = Field(description="Имя таблицы")
    group_by: list[str] = Field(
        description="Колонки сегментации",
        min_length=1,
    )
    measures: list[str] | None = Field(
        default=None,
        description="Числовые колонки для сводки (без них — только размер)",
    )
    filter: str | None = Field(default=None, description="SQL WHERE")
    top_n: int = Field(default=100, description="Максимум сегментов", ge=1)


class CompareSegmentsParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "table": "events",
                    "segment_col": "city",
                    "a": "Москва",
                    "b": "СПб",
                    "measures": ["amount"],
                },
                {
                    "table": "test",
                    "segment_col": "city",
                    "a": "Москва",
                    "b": "СПб",
                    "measures": ["age"],
                },
            ]
        }
    )

    table: str = Field(description="Имя таблицы")
    segment_col: str = Field(description="Колонка-сегмент")
    a: str = Field(description="Первое значение сегмента")
    b: str = Field(description="Второе значение сегмента")
    measures: list[str] = Field(
        description="Колонки для сравнения: числовые → t-test + Cohen's d; "
        "категориальные → χ² + Cramér's V",
        min_length=1,
    )
    filter: str | None = Field(default=None, description="SQL WHERE")
    alpha: float = Field(
        default=0.05,
        description="Порог значимости",
        gt=0,
        lt=1,
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
    ToolDefinition(
        name="seasonality",
        description=(
            "Ищет повторяющиеся во времени закономерности (STL-декомпозиция). "
            "Возвращает силу сезонности и тренда, пиковую и минимальную фазы, "
            "амплитуду. Требует минимум 2*period наблюдений."
        ),
        parameters_model=SeasonalityParams,
    ),
    ToolDefinition(
        name="segment_profile",
        description=(
            "Сводка по группам: размер сегмента и mean/median/min/max/std "
            "по выбранным числовым колонкам."
        ),
        parameters_model=SegmentProfileParams,
    ),
    ToolDefinition(
        name="compare_segments",
        description=(
            "Сравнение двух значений сегментной колонки: для числовых "
            "колонок — t-test (Welch) и Cohen's d, для категориальных — "
            "χ² и Cramér's V. Возвращает p-value и признак значимости."
        ),
        parameters_model=CompareSegmentsParams,
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
    "seasonality": seasonality,
    "segment_profile": segment_profile,
    "compare_segments": compare_segments,
}


def execute_tool(
    tool_name: str,
    params: dict[str, Any],
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
) -> ToolResult:
    """Валидирует параметры и выполняет инструмент из реестр."""
    definition = next((tool for tool in TOOLS_REGISTRY if tool.name == tool_name), None)
    if definition is None:
        raise ValueError(f"Инструмент '{tool_name}' не зарегистрирован")

    validated = definition.parameters_model.model_validate(params)
    arguments = validated.model_dump(exclude_none=True)
    # source_result_id относится к будущей работе поверх сохранённого результата,
    # а run_sql принимает только SQL и лимит строк.
    arguments.pop("source_result_id", None)
    if connection is not None:
        arguments["connection"] = connection
    return TOOL_FUNCTIONS[tool_name](**arguments)
