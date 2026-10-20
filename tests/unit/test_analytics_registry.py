import json

import pytest
from pydantic import ValidationError

from djedai.analytics.registry import (
    TOOLS_REGISTRY,
    DescribeColumnParams,
    DescribeDatasetParams,
    RunSqlParams,
    ValueCountsParams,
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
                    "description": "ID предыдущего результата, если нужно сделать запрос "
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


def test_export_can_be_modified_without_changing_registry_or_future_exports():
    tools = get_tools_for_llm()
    tools[0]["name"] = "changed"
    tools[0]["parameters_schema"]["properties"]["sql"]["type"] = "number"
    tools.pop()

    assert get_tools_for_llm() == EXPECTED_TOOLS
    assert TOOLS_REGISTRY[0].name == "run_sql"
    assert TOOLS_REGISTRY[0].parameters_schema == EXPECTED_TOOLS[0]["parameters_schema"]
