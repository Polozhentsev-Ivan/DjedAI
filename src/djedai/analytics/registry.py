from collections.abc import Callable, Collection
from typing import Any, Literal, TypedDict

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from djedai.analytics.metrics import TIME_GRAINS, MetricContext, compute_metric
from djedai.analytics.tools import (
    compare_segments,
    correlation,
    describe_column,
    describe_dataset,
    detect_anomalies,
    find_duplicates,
    forecast,
    lagged_correlation,
    run_sql,
    seasonality,
    segment_profile,
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


class MetricFilterParam(BaseModel):
    column: str = Field(description="Колонка вида таблица.колонка")
    op: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "not_null"]
    value: Any = Field(default=None, description="Значение; для in/not_in — список")


class ComputeMetricParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "metric_id": "revenue",
                    "group_by": ["customers.segment"],
                    "period_start": "2023-01-01",
                    "period_end": "2024-01-01",
                    "time_grain": "month",
                }
            ]
        }
    )

    metric_id: str = Field(description="ID подтверждённой метрики из раздела «Метрики»")
    group_by: list[str] = Field(
        default_factory=list, description="Колонки разбивки вида таблица.колонка"
    )
    period_start: str | None = Field(default=None, description="Начало периода ISO, включительно")
    period_end: str | None = Field(default=None, description="Конец периода ISO, не включительно")
    time_grain: Literal[TIME_GRAINS] | None = Field(  # type: ignore[valid-type]
        default=None, description="Разбивка по календарю метрики"
    )
    currency: str | None = Field(
        default=None, description="Валюта, если метрика хранит валюту в колонке"
    )
    filters: list[MetricFilterParam] = Field(
        default_factory=list, description="Дополнительные условия вопроса"
    )


class CorrelationParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"table": "events", "columns": ["amount", "id"], "method": "pearson"},
                {"table": "test", "columns": ["age", "id"], "method": "spearman", "min_n": 3},
            ]
        }
    )
    table: str = Field(description="Имя таблицы")
    columns: list[str] = Field(
        description="Числовые колонки (минимум две)",
        min_length=2,
    )
    method: Literal["pearson", "spearman", "kendall"] = Field(
        default="pearson",
        description="Метод корреляции",
    )
    filter: str | None = Field(default=None, description="SQL WHERE")
    min_n: int = Field(
        default=10,
        description="Минимальное число наблюдений для оценки",
        ge=3,
    )


class LaggedCorrelationParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "table": "events",
                    "time_col": "ts",
                    "a": "amount",
                    "b": "id",
                    "freq": "day",
                    "max_lag": 7,
                },
                {
                    "table": "events",
                    "time_col": "ts",
                    "a": "amount",
                    "b": "id",
                    "freq": "day",
                    "agg": "sum",
                    "max_lag": 14,
                    "include_granger": True,
                },
            ]
        }
    )
    table: str = Field(description="Имя таблицы")
    time_col: str = Field(description="Колонка DATE или TIMESTAMP")
    a: str = Field(description="Числовая колонка A")
    b: str = Field(description="Числовая колонка B")
    freq: Literal["minute", "hour", "day", "week", "month", "quarter", "year"] = Field(
        default="day",
        description="Частота агрегации",
    )
    agg: Literal["count", "sum", "avg", "min", "max", "median", "stddev"] = Field(
        default="sum",
        description="Агрегация",
    )
    max_lag: int = Field(
        default=14,
        description="Максимальный сдвиг в периодах",
        ge=0,
        le=180,
    )
    min_n: int = Field(
        default=10,
        description="Минимальное число наблюдений на лаг",
        ge=3,
    )
    method: Literal["pearson", "spearman"] = Field(
        default="pearson",
        description="Метод корреляции",
    )
    filter: str | None = Field(default=None, description="SQL WHERE")
    include_granger: bool = Field(
        default=False,
        description="Добавить тест Грейнджера (потенциальная предсказуемость, не причинность)",
    )


class ForecastParams(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "table": "events",
                    "time_col": "ts",
                    "measure": "amount",
                    "agg": "sum",
                    "freq": "month",
                    "horizon": 6,
                    "method": "ets",
                    "holdout": 3,
                },
            ]
        }
    )

    table: str = Field(description="Имя таблицы")
    time_col: str = Field(description="Колонка DATE или TIMESTAMP")
    horizon: int = Field(
        description="Горизонт прогноза в периодах",
        ge=1,
        le=180,
    )
    measure: str | None = Field(
        default=None,
        description="Колонка метрики (обязательна при agg != 'count')",
    )
    agg: Literal["count", "sum", "avg", "min", "max", "median", "stddev"] = Field(
        default="sum",
        description="Агрегация",
    )
    freq: Literal["minute", "hour", "day", "week", "month", "quarter", "year"] = Field(
        default="day",
        description="Частота агрегации",
    )
    period: int | None = Field(
        default=None,
        description=(
            "Период сезонности (>= 1). 1 — без сезонности (для naive). "
            "По умолчанию из частоты: 7/day, 52/week, 12/month, 4/quarter."
        ),
        ge=1,
    )
    method: Literal["ets", "arima", "naive"] = Field(
        default="ets",
        description="Метод прогноза",
    )
    alpha: float = Field(
        default=0.05,
        description="Уровень значимости (0, 1); 0.05 → интервал 95%",
        gt=0,
        lt=1,
    )
    holdout: int | None = Field(
        default=None,
        description="Число последних точек для валидации на отложенном периоде",
        ge=1,
    )
    filter: str | None = Field(default=None, description="SQL WHERE")


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
    ToolDefinition(
        name="compute_metric",
        description=(
            "Считает подтверждённую бизнес-метрику по её определению: на своём grain, "
            "с популяцией, валютой и календарём из определения. Для подтверждённых метрик "
            "используйте его вместо run_sql. Неопределённая метрика, размножение строк JOIN, "
            "потеря строк или смешанные валюты возвращают ошибку для уточнения."
        ),
        parameters_model=ComputeMetricParams,
    ),
    ToolDefinition(
        name="correlation",
        description=(
            "Считает попарные корреляции между числовыми колонками "
            "(Pearson/Spearman/Kendall). Возвращает r, p-value и n для каждой пары. "
            "Показывает статистическую связь, а не доказанную причинность."
        ),
        parameters_model=CorrelationParams,
    ),
    ToolDefinition(
        name="lagged_correlation",
        description=(
            "Кросс-корреляция двух числовых рядов по лагам. Находит сдвиг, при котором "
            "|r| максимален. lag=k означает: a(t) сравнивается с b(t+k); положительный "
            "лаг — a опережает b, отрицательный — b опережает a. Опционально тест "
            "Грейнджера (потенциальная предсказуемость, не причинность)."
        ),
        parameters_model=LaggedCorrelationParams,
    ),
    ToolDefinition(
        name="forecast",
        description=(
            "Строит прогноз числового показателя на горизонт с интервалами "
            "неопределённости. Методы: ets (Holt-Winters), arima (ARIMA/SARIMAX), "
            "naive (сезонный naive). Если передан holdout — считает MAE/RMSE/MAPE "
            "на отложенном периоде. Прогноз — статистическая экстраполяция, "
            "не гарантия."
        ),
        parameters_model=ForecastParams,
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
    "compute_metric": compute_metric,
    "correlation": correlation,
    "lagged_correlation": lagged_correlation,
    "forecast": forecast,
}


_SEMANTIC_TOOLS = {"compute_metric", "run_sql"}


def execute_tool(
    tool_name: str,
    params: dict[str, Any],
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
    allowed_tables: Collection[str] | None = None,
    semantics: MetricContext | None = None,
) -> ToolResult:
    """Валидирует параметры и выполняет инструмент из реестр.

    ``semantics`` — каталог и подтверждённые метрики датасета (B2-9): их получают
    ``compute_metric`` и защита ``run_sql`` от размножения строк.
    """
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
    if tool_name in _SEMANTIC_TOOLS:
        arguments["semantics"] = semantics
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
