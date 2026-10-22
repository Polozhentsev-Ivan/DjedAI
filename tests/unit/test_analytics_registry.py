import json

import pytest
from pydantic import ValidationError

from djedai.analytics.registry import (
    TOOLS_REGISTRY,
    DescribeColumnParams,
    DescribeDatasetParams,
    RunSqlParams,
    ValueCountsParams,
    execute_tool,
    get_tools_for_llm,
)

EXPECTED_TOOLS = [
    {
        "name": "run_sql",
        "description": "Выполняет безопасный SQL-запрос (только SELECT) для получения данных.",
        "parameters_schema": {
            "description": "Модель параметров для функции run_sql.",
            "properties": {
                "sql": {
                    "description": "SQL-запрос (только SELECT) для получения данных.",
                    "title": "Sql",
                    "type": "string",
                },
                "source_result_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "ID предыдущего результата, если "
                    "нужно сделать запрос "
                    "поверх уже посчитанных данных.",
                    "title": "Source Result Id",
                },
            },
            "required": ["sql"],
            "title": "RunSqlParams",
            "type": "object",
        },
    },
    {
        "name": "describe_dataset",
        "description": "Показывает общую информацию о загруженных данных (список таблиц, колонок).",
        "parameters_schema": {
            "description": "Модель параметров для describe_dataset. У этой функции нет параметров.",
            "examples": [{}],
            "properties": {},
            "title": "DescribeDatasetParams",
            "type": "object",
        },
    },
    {
        "name": "describe_column",
        "description": "Показывает статистику по конкретной колонке (пропуски, "
        "уникальные значения).",
        "parameters_schema": {
            "examples": [{"table": "test", "column": "age"}],
            "properties": {
                "table": {
                    "description": "Имя таблицы",
                    "title": "Table",
                    "type": "string",
                },
                "column": {
                    "description": "Имя колонки",
                    "title": "Column",
                    "type": "string",
                },
            },
            "required": ["table", "column"],
            "title": "DescribeColumnParams",
            "type": "object",
        },
    },
    {
        "name": "value_counts",
        "description": "Показывает самые частые значения в колонке.",
        "parameters_schema": {
            "examples": [{"table": "test", "column": "city", "top_n": 3, "filter": "age > 25"}],
            "properties": {
                "table": {
                    "description": "Имя таблицы",
                    "title": "Table",
                    "type": "string",
                },
                "column": {
                    "description": "Имя колонки",
                    "title": "Column",
                    "type": "string",
                },
                "top_n": {
                    "default": 10,
                    "description": "Количество топ-значений",
                    "title": "Top N",
                    "type": "integer",
                },
                "filter": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Опциональный фильтр (SQL WHERE). Например: 'age > 25'",
                    "title": "Filter",
                },
            },
            "required": ["table", "column"],
            "title": "ValueCountsParams",
            "type": "object",
        },
    },
    {
        "name": "time_aggregate",
        "description": (
            "Агрегирует числовую метрику по временным бакетам "
            "(минута/час/день/неделя/месяц/квартал/год), опционально — "
            "с группировкой. Аналог GROUP BY date_trunc(...)."
        ),
        "parameters_schema": {
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
            ],
            "properties": {
                "table": {"description": "Имя таблицы", "title": "Table", "type": "string"},
                "time_col": {
                    "description": "Колонка DATE или TIMESTAMP",
                    "title": "Time Col",
                    "type": "string",
                },
                "measure": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Колонка метрики (обязательна при agg != 'count')",
                    "title": "Measure",
                },
                "agg": {
                    "default": "count",
                    "description": "Функция агрегации",
                    "enum": ["count", "sum", "avg", "min", "max", "median", "stddev"],
                    "title": "Agg",
                    "type": "string",
                },
                "freq": {
                    "default": "day",
                    "description": "Размер временного бакета",
                    "enum": ["minute", "hour", "day", "week", "month", "quarter", "year"],
                    "title": "Freq",
                    "type": "string",
                },
                "group_by": {
                    "anyOf": [
                        {"items": {"type": "string"}, "type": "array"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Дополнительные колонки группировки",
                    "title": "Group By",
                },
                "filter": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "SQL WHERE без префиксов таблиц, напр. \"city = 'Москва'\"",
                    "title": "Filter",
                },
                "top_n": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Максимум бакетов в результате",
                    "title": "Top N",
                },
            },
            "required": ["table", "time_col"],
            "title": "TimeAggregateParams",
            "type": "object",
        },
    },
    {
        "name": "find_duplicates",
        "description": (
            "Находит группы дублей по выбранным полям. "
            "Возвращает комбинацию значений, количество повторов и "
            "опционально прочие поля первой строки группы."
        ),
        "parameters_schema": {
            "examples": [
                {"table": "events"},
                {"table": "test", "columns": ["city", "age"]},
                {"table": "events", "columns": ["ts", "city"], "include_all_columns": True},
            ],
            "properties": {
                "table": {"description": "Имя таблицы", "title": "Table", "type": "string"},
                "columns": {
                    "anyOf": [
                        {"items": {"type": "string"}, "type": "array"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Колонки дубля. Не указано — ищем полные "
                    "дубли строк (все колонки).",
                    "title": "Columns",
                },
                "filter": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "SQL WHERE",
                    "title": "Filter",
                },
                "include_all_columns": {
                    "default": False,
                    "description": "Показать прочие колонки первой "
                    "строки группы (только если columns задан)",
                    "title": "Include All Columns",
                    "type": "boolean",
                },
                "limit": {
                    "default": 100,
                    "description": "Максимум групп дублей",
                    "minimum": 1,
                    "title": "Limit",
                    "type": "integer",
                },
            },
            "required": ["table"],
            "title": "FindDuplicatesParams",
            "type": "object",
        },
    },
    {
        "name": "detect_anomalies",
        "description": (
            "Ищет выбросы в числовой колонке. Точечные методы: "
            "iqr (Тьюки, 1.5·IQR), zscore (3σ), mad (3.5·1.4826·MAD). "
            "Для временных рядов — stl-residual (остатки STL-декомпозиции, "
            "нужен time_col и freq). Поддерживает группировку и id_column."
        ),
        "parameters_schema": {
            "examples": [
                {"table": "events", "measure": "amount", "method": "iqr"},
                {"table": "events", "measure": "amount", "method": "zscore", "threshold": 2.5},
                {"table": "events", "measure": "amount", "method": "mad", "group_by": ["city"]},
                {
                    "table": "events",
                    "measure": "amount",
                    "method": "stl-residual",
                    "time_col": "ts",
                    "freq": "month",
                },
            ],
            "properties": {
                "table": {"description": "Имя таблицы", "title": "Table", "type": "string"},
                "measure": {
                    "description": "Числовая колонка для проверки на выбросы",
                    "title": "Measure",
                    "type": "string",
                },
                "method": {
                    "default": "iqr",
                    "description": (
                        "iqr=1.5·IQR, zscore=3σ, mad=3.5·1.4826·MAD "
                        "(точечные); stl-residual — остатки STL-декомпозиции "
                        "временного ряда, нужен time_col."
                    ),
                    "enum": ["iqr", "zscore", "mad", "stl-residual"],
                    "title": "Method",
                    "type": "string",
                },
                "time_col": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Колонка DATE/TIMESTAMP. Обязательна для method='stl-residual'.",
                    "title": "Time Col",
                },
                "freq": {
                    "default": "day",
                    "description": "Частота агрегации для stl-residual (остальные "
                    "методы игнорируют).",
                    "enum": ["minute", "hour", "day", "week", "month", "quarter", "year"],
                    "title": "Freq",
                    "type": "string",
                },
                "threshold": {
                    "anyOf": [{"type": "number"}, {"type": "null"}],
                    "default": None,
                    "description": "Порог. По умолчанию: 1.5 / 3.0 / 3.5 /"
                    " 3.0 для iqr / zscore / mad / stl-residual.",
                    "title": "Threshold",
                },
                "group_by": {
                    "anyOf": [
                        {"items": {"type": "string"}, "type": "array"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Колонки группировки",
                    "title": "Group By",
                },
                "filter": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "SQL WHERE",
                    "title": "Filter",
                },
                "id_column": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Колонка-идентификатор строки",
                    "title": "Id Column",
                },
                "limit": {
                    "default": 100,
                    "description": "Максимум аномалий",
                    "minimum": 1,
                    "title": "Limit",
                    "type": "integer",
                },
            },
            "required": ["table", "measure"],
            "title": "DetectAnomaliesParams",
            "type": "object",
        },
    },
]


def test_llm_export_is_json_serializable():
    tools = get_tools_for_llm()

    assert json.loads(json.dumps(tools, ensure_ascii=False)) == tools


def test_llm_export_contains_exact_tool_schemas():
    assert get_tools_for_llm() == EXPECTED_TOOLS


@pytest.mark.parametrize("tool, expected", list(zip(TOOLS_REGISTRY, EXPECTED_TOOLS, strict=True)))
def test_internal_registry_retains_schema_and_validation_model(tool, expected):
    assert tool.parameters_schema == expected["parameters_schema"]
    assert tool.parameters_model.model_json_schema() == expected["parameters_schema"]

    if tool.name == "run_sql":
        assert tool.parameters_model is RunSqlParams
        params = tool.parameters_model.model_validate({"sql": "SELECT 1"})
        assert params.sql == "SELECT 1"
        assert params.source_result_id is None
        params = tool.parameters_model.model_validate(
            {"sql": "SELECT * FROM previous", "source_result_id": "r_1"}
        )
        assert params.source_result_id == "r_1"
        with pytest.raises(ValidationError):
            tool.parameters_model.model_validate({})
        with pytest.raises(ValidationError):
            tool.parameters_model.model_validate({"sql": "SELECT 1", "source_result_id": 1})
    elif tool.name == "describe_dataset":
        assert tool.parameters_model is DescribeDatasetParams
        assert tool.parameters_model.model_validate({}).model_dump() == {}
        with pytest.raises(ValidationError):
            tool.parameters_model.model_validate("invalid parameters")

    elif tool.name == "describe_column":
        assert tool.parameters_model is DescribeColumnParams
        params = tool.parameters_model.model_validate({"table": "test", "column": "age"})
        assert params.table == "test"
        with pytest.raises(ValidationError):
            tool.parameters_model.model_validate({"table": "test"})

    elif tool.name == "value_counts":
        assert tool.parameters_model is ValueCountsParams
        params = tool.parameters_model.model_validate(
            {"table": "test", "column": "city", "filter": "age > 20"}
        )
        assert params.filter == "age > 20"
        with pytest.raises(ValidationError):
            tool.parameters_model.model_validate({"column": "city"})


def test_execute_tool_validates_params_and_dispatches():
    result = execute_tool("run_sql", {"sql": "SELECT 1 AS value"})

    assert result.status == "ok"
    assert result.tool == "run_sql"
    assert result.data is not None
    assert result.data.rows == [[1]]

    described = execute_tool("describe_dataset", {})
    assert described.tool == "describe_dataset"
    assert described.status == "ok"


def test_execute_tool_ignores_future_source_result_id():
    result = execute_tool(
        "run_sql",
        {"sql": "SELECT 1 AS value", "source_result_id": "r_previous"},
    )

    assert result.status == "ok"
    assert result.params["sql"] == "SELECT 1 AS value"


def test_execute_tool_rejects_unknown_tool_and_invalid_params():
    with pytest.raises(ValueError, match="не зарегистрирован"):
        execute_tool("unknown_tool", {})

    with pytest.raises(ValidationError):
        execute_tool("run_sql", {"sql": 1})


def test_export_can_be_modified_without_changing_registry_or_future_exports():
    tools = get_tools_for_llm()
    tools[0]["name"] = "changed"
    tools[0]["parameters_schema"]["properties"]["sql"]["type"] = "number"
    tools.pop()

    assert get_tools_for_llm() == EXPECTED_TOOLS
    assert TOOLS_REGISTRY[0].name == "run_sql"
    assert TOOLS_REGISTRY[0].parameters_schema == EXPECTED_TOOLS[0]["parameters_schema"]
