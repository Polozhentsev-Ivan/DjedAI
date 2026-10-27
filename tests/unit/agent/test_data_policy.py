"""Единая политика передачи данных (#173): категории, режимы и очистка."""

from __future__ import annotations

import json

import pytest

from djedai.agent.data_policy import (
    POLICY_RESTRICTED_MESSAGE,
    REDACTED,
    DataCategory,
    DataContext,
    DataMode,
    DataTransferPolicy,
    DataTransferRestricted,
    data_sharing_allowed,
    policy_for_client,
    sensitive_column_names,
)
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.tool_result import ChartHint, Fact, ToolError, ToolResult, ToolResultData

SECRET = "sk-live-1234567890abcdef"
DSN = "postgresql://analyst:s3cr3t@db.internal:5432/warehouse"


def _catalog_with_column(name: str, original_name: str) -> DatasetCatalog:
    return DatasetCatalog.model_validate(
        {
            "dataset_id": "ds",
            "name": "ds",
            "created_at": "2026-01-01T00:00:00Z",
            "source": {"kind": "csv_upload"},
            "tables": [
                {
                    "table_id": "s",
                    "original_name": "s.csv",
                    "row_count": 1,
                    "duplicate_rows": 0,
                    "columns": [
                        {
                            "name": name,
                            "original_name": original_name,
                            "physical_type": "VARCHAR",
                            "semantic_type": "text",
                            "role": "text",
                            "null_ratio": 0.0,
                            "distinct_count": 1,
                            "is_unique": True,
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("category", "context", "restricted", "sharing"),
    [
        (DataCategory.METADATA, DataContext.USER, True, True),
        (DataCategory.USER_TEXT, DataContext.USER, True, True),
        (DataCategory.USER_TEXT, DataContext.HISTORY, True, True),
        (DataCategory.FACT, DataContext.CURRENT_RESULT, True, True),
        (DataCategory.FACT, DataContext.HISTORY, True, True),
        (DataCategory.AGGREGATE, DataContext.CURRENT_RESULT, True, True),
        (DataCategory.CELL_VALUE, DataContext.CURRENT_RESULT, True, True),
        (DataCategory.CELL_VALUE, DataContext.HISTORY, False, True),
        (DataCategory.CELL_VALUE, DataContext.CATALOG, False, True),
        (DataCategory.CELL_VALUE, DataContext.USER, False, True),
        (DataCategory.FREE_TEXT, DataContext.CURRENT_RESULT, True, True),
        (DataCategory.FREE_TEXT, DataContext.HISTORY, False, True),
        (DataCategory.FREE_TEXT, DataContext.CATALOG, False, True),
        (DataCategory.SECRET, DataContext.CURRENT_RESULT, False, False),
        (DataCategory.SECRET, DataContext.CATALOG, False, False),
    ],
)
def test_category_rules_are_stage_and_context_aware(
    category: DataCategory,
    context: DataContext,
    restricted: bool,
    sharing: bool,
) -> None:
    assert DataTransferPolicy.restricted().allows(category, context) is restricted
    assert DataTransferPolicy.sharing().allows(category, context) is sharing


def test_secret_is_redacted_from_nested_values_and_names() -> None:
    policy = DataTransferPolicy.sharing()
    payload = {
        "password": "plain-password",
        "nested": {
            "api_key": SECRET,
            "connection": DSN,
            "note": f"Bearer {SECRET}",
            "list": [{"token": "token-value"}, SECRET],
        },
        "column name with secret=value": "kept",
        f"column {SECRET}": "visible",
    }

    sanitized = policy.sanitize_value(payload)
    encoded = str(sanitized)

    for marker in ("plain-password", SECRET, DSN, "token-value"):
        assert marker not in encoded
    assert sanitized["password"] == REDACTED
    assert sanitized["nested"]["api_key"] == REDACTED
    assert sanitized["nested"]["connection"] == REDACTED
    assert sanitized["nested"]["note"] == f"Bearer {REDACTED}"
    assert sanitized["nested"]["list"][0]["token"] == REDACTED
    assert any(key.startswith("column [REDACTED]") for key in sanitized)


def test_sanitize_value_handles_models_and_bytes() -> None:
    policy = DataTransferPolicy.restricted()
    fact = Fact(text=f"ключ {SECRET}", values={"api_key": SECRET, "ok": 1})

    sanitized = policy.sanitize_value(fact)

    assert SECRET not in str(sanitized)
    assert sanitized["values"]["api_key"] == REDACTED
    assert sanitized["values"]["ok"] == 1
    assert policy.sanitize_value(b"raw-bytes") == REDACTED


def test_result_data_keeps_current_rows_and_omits_history_rows() -> None:
    data = ToolResultData(
        columns=["name", "password", "amount"],
        rows=[["visible", "column-secret", 10], ["second", "another-secret", 20]],
        row_count=2,
    )
    policy = DataTransferPolicy.restricted()

    current = policy.result_data(data, context=DataContext.CURRENT_RESULT, max_rows=1)
    history = policy.result_data(data, context=DataContext.HISTORY)

    assert current["rows"] == [["visible", REDACTED, 10]]
    assert current["rows_truncated"] is True
    assert current["row_count"] == 2
    assert "rows" not in history
    assert history["rows_omitted"] == "data_policy"
    assert "column-secret" not in str(current)
    assert "column-secret" not in str(history)


def test_sharing_mode_sends_history_rows_and_catalog_samples() -> None:
    data = ToolResultData(columns=["category"], rows=[["history-marker"]])
    policy = DataTransferPolicy.sharing()

    payload = policy.result_data(data, context=DataContext.HISTORY)

    assert payload["rows"] == [["history-marker"]]


def test_chart_result_omits_history_sample_rows_but_keeps_facts() -> None:
    result = ToolResult(
        result_id="r_1",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["category", "value"], rows=[["hidden-row", 10]]),
        facts=[Fact(text="aggregate-marker 10", values={"total": 10})],
        chart_hint=ChartHint(type="bar", x="category", y=["value"]),
    )
    policy = DataTransferPolicy.restricted()

    payload = policy.chart_result(result, context=DataContext.HISTORY)

    assert payload["sample_rows_omitted"] == "data_policy"
    assert "hidden-row" not in str(payload)
    assert payload["facts"] == ["aggregate-marker 10"]
    assert payload["chart_hint"]["type"] == "bar"


def test_error_and_fact_sanitizers_remove_secrets() -> None:
    policy = DataTransferPolicy.restricted()
    error = ToolError(
        code="database_error",
        message=f"connection failed: {DSN}",
        suggestion=f"проверьте api_key={SECRET}",
    )
    fact = Fact(text=f"password={SECRET}", values={"token": SECRET, "count": 3})

    safe_error = policy.sanitize_error(error)
    safe_fact = policy.sanitize_fact_model(fact)

    assert safe_error.code == "database_error"
    assert DSN not in safe_error.message
    assert SECRET not in safe_error.suggestion
    assert SECRET not in safe_fact.text
    assert safe_fact.values["token"] == REDACTED
    assert safe_fact.values["count"] == 3


def test_sanitize_description_payload_redacts_examples_of_sensitive_columns() -> None:
    policy = DataTransferPolicy.sharing()
    payload = {
        "table": "users",
        "columns": [
            {
                "name": "password",
                "original_name": "Пароль",
                "sample_values": ["hunter2", "correct horse"],
                "top_values": [{"value": "hunter2", "count": 3}],
                "stats": {"min": "hunter2", "max": "zzz"},
            },
            {
                "name": "city",
                "original_name": "Город",
                "sample_values": ["Berlin"],
                "top_values": [{"value": "Berlin", "count": 2}],
                "stats": {"min": "Berlin", "max": "Munich"},
            },
        ],
    }

    safe = policy.sanitize_description_payload(payload)
    password, city = safe["columns"]

    assert password["sample_values"] == [REDACTED, REDACTED]
    assert password["top_values"] == [{"value": REDACTED, "count": 3}]
    assert password["stats"] == {"min": REDACTED, "max": REDACTED}
    assert city["sample_values"] == ["Berlin"]
    assert city["top_values"] == [{"value": "Berlin", "count": 2}]
    assert city["stats"] == {"min": "Berlin", "max": "Munich"}
    assert "hunter2" not in str(safe) and "correct horse" not in str(safe)


def test_sanitize_description_payload_checks_original_name_and_key_parts() -> None:
    policy = DataTransferPolicy.sharing()
    payload = {
        "columns": [
            {"name": "user_password", "sample_values": ["secret-value"]},
            {"name": "value", "original_name": "password", "sample_values": ["other-secret"]},
            {"name": "public", "original_name": "public", "sample_values": ["visible"]},
        ]
    }

    safe = policy.sanitize_description_payload(payload)

    assert safe["columns"][0]["sample_values"] == [REDACTED]
    assert safe["columns"][1]["sample_values"] == [REDACTED]
    assert safe["columns"][2]["sample_values"] == ["visible"]


def test_sanitize_description_payload_redacts_sensitive_dataset_periods() -> None:
    policy = DataTransferPolicy.sharing()
    payload = {
        "dataset": "users",
        "tables": [
            {
                "table": "accounts",
                "periods": [
                    {"column": "password", "min": "2020-01-01", "max": "2024-01-01"},
                    {"column": "created_at", "min": "2020-01-01", "max": "2024-01-01"},
                ],
            }
        ],
    }

    safe = policy.sanitize_description_payload(payload)
    password, created_at = safe["tables"][0]["periods"]

    assert password == {"column": "password", "min": REDACTED, "max": REDACTED}
    assert created_at == {
        "column": "created_at",
        "min": "2020-01-01",
        "max": "2024-01-01",
    }


def test_sanitize_description_payload_uses_period_original_name() -> None:
    policy = DataTransferPolicy.sharing()
    payload = {
        "tables": [
            {
                "table": "accounts",
                "periods": [
                    {
                        "column": "userpassword",
                        "original_name": "userPassword",
                        "min": "1990-01-01",
                        "max": "1991-02-02",
                    },
                    {
                        "column": "created_at",
                        "original_name": "createdAt",
                        "min": "2020-01-01",
                        "max": "2024-01-01",
                    },
                ],
            }
        ]
    }

    safe = policy.sanitize_description_payload(payload)
    sensitive, created_at = safe["tables"][0]["periods"]

    assert sensitive["min"] == REDACTED and sensitive["max"] == REDACTED
    assert created_at["min"] == "2020-01-01" and created_at["max"] == "2024-01-01"


@pytest.mark.parametrize(
    ("name", "original_name"),
    [
        ("password", "Пароль"),
        ("userpassword", "userPassword"),
        ("passwordhash", "passwordHash"),
        ("apitoken", "apiToken"),
        ("dbpassword", "dbPassword"),
    ],
    ids=["name", "original-name", "normalized-name", "camel-case-name", "db-prefix"],
)
def test_sanitize_catalog_redacts_sensitive_numeric_column_values(
    name: str, original_name: str
) -> None:
    catalog = DatasetCatalog.model_validate(
        {
            "schema_version": 1,
            "dataset_id": "ds",
            "name": "ds",
            "created_at": "2026-01-01T00:00:00Z",
            "source": {"kind": "csv_upload"},
            "tables": [
                {
                    "table_id": "s",
                    "original_name": "s.csv",
                    "row_count": 2,
                    "duplicate_rows": 0,
                    "columns": [
                        {
                            "name": name,
                            "original_name": original_name,
                            "physical_type": "BIGINT",
                            "semantic_type": "integer",
                            "role": "measure",
                            "null_ratio": 0.0,
                            "distinct_count": 2,
                            "is_unique": False,
                            "sample_values": [89462718, 89462719],
                            "top_values": [{"value": 89462718, "count": 1}],
                            "stats": {
                                "min": 89462718,
                                "max": 89462719,
                                "mean": 89462718.5,
                                "median": 89462718.5,
                                "std": 0.5,
                            },
                            "description": "Пароль 89462718",
                        }
                    ],
                }
            ],
        }
    )

    safe = DataTransferPolicy.sharing().sanitize_catalog(catalog)
    prompt = safe.to_prompt()

    assert "89462718" not in prompt and "89462719" not in prompt
    assert "[REDACTED] … [REDACTED]" in prompt
    assert f"{name}: integer, measure" in prompt
    assert safe.tables[0].columns[0].stats.min == REDACTED
    assert safe.tables[0].columns[0].stats.mean is None


def test_sanitize_fact_with_sensitive_source_redacts_text_and_values() -> None:
    policy = DataTransferPolicy.sharing()
    facts = [
        Fact(
            text="Значение '89462718' встречается 2 раз.",
            kind="freq",
            values={"value": 89462718, "count": 2},
        ),
        Fact(
            text="Примеры значений: 89462718, 89462719",
            kind="examples",
            values={"examples": [89462718, 89462719]},
        ),
        Fact(
            text="Мин: 89462718, Макс: 89462719, Среднее: 89462718.5",
            kind="numeric_profile",
            values={"min": 89462718, "max": 89462719, "mean": 89462718.5},
        ),
    ]

    for fact in facts:
        safe = policy.sanitize_fact(fact, sensitive=True)
        assert "89462718" not in str(safe) and "89462719" not in str(safe)
        assert REDACTED in str(safe)

    safe_freq = policy.sanitize_fact(facts[0], sensitive=True)
    assert safe_freq["values"]["count"] == 2

    profile = Fact(
        text="Всего строк: 2, пропусков: 0, уникальных: 2",
        kind="profile",
        values={"total": 2, "non_null": 2, "distinct": 2, "null_count": 0},
    )
    safe_profile = policy.sanitize_fact(profile, sensitive=True)
    assert "Всего строк: 2" in safe_profile["text"]
    assert safe_profile["values"]["total"] == 2


def test_result_has_sensitive_source_checks_params_and_result_columns() -> None:
    policy = DataTransferPolicy.sharing()
    described = ToolResult(
        result_id="r_described",
        status="ok",
        tool="describe_column",
        params={"table": "s", "column": "password"},
        data=ToolResultData(columns=["password", "count"], rows=[[89462718, 2]]),
    )
    normalized = ToolResult(
        result_id="r_normalized",
        status="ok",
        tool="describe_column",
        params={"table": "s", "column": "userpassword"},
        data=ToolResultData(columns=["userpassword", "count"], rows=[["1990-01-01", 2]]),
    )
    sql_result = ToolResult(
        result_id="r_sql",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["category", "amount"], rows=[["A", 1]]),
    )

    assert policy.result_has_sensitive_source(described) is True
    assert policy.result_has_sensitive_source(normalized) is True
    assert policy.result_has_sensitive_source(sql_result) is False


@pytest.mark.parametrize(
    "column",
    [
        "dbpassword",
        "dbPassword",
        "db_password",
        "dbpasswordhash",
        "dbpasswordhashvalue",
        "mytoken",
        "appsecret",
    ],
)
def test_normalized_compound_column_is_sensitive(column: str) -> None:
    policy = DataTransferPolicy.restricted()
    result = ToolResult(
        result_id="r_compound",
        status="ok",
        tool="describe_column",
        params={"table": "s", "column": column},
        data=ToolResultData(columns=[column, "count"], rows=[["ordinaryCredential9!", 2]]),
    )

    assert policy.result_has_sensitive_source(result) is True


@pytest.mark.parametrize(
    "column",
    ["tokenizer", "secretary", "passwordless", "credentialed"],
)
def test_normalized_metadata_column_is_not_sensitive(column: str) -> None:
    policy = DataTransferPolicy.restricted()
    result = ToolResult(
        result_id="r_metadata",
        status="ok",
        tool="describe_column",
        params={"table": "s", "column": column},
        data=ToolResultData(columns=[column, "count"], rows=[["ordinary value", 2]]),
    )

    assert policy.result_has_sensitive_source(result) is False


def test_sensitive_column_names_preserves_original_compound_name() -> None:
    catalog = _catalog_with_column("dbpasswordtext", "dbPasswordText")

    names = sensitive_column_names(catalog)

    assert "dbpasswordtext" in names
    assert "dbPasswordText" in names


def test_sensitive_column_names_keeps_metadata_words_non_sensitive() -> None:
    catalog = _catalog_with_column("tokenizer", "tokenizer")

    assert sensitive_column_names(catalog) == ()


def test_result_has_sensitive_source_accepts_normalized_catalog_names() -> None:
    policy = DataTransferPolicy.restricted()
    result = ToolResult(
        result_id="r_db_password_text",
        status="ok",
        tool="describe_column",
        params={"table": "s", "column": "dbpasswordtext"},
        data=ToolResultData(
            columns=["dbpasswordtext", "count"], rows=[["ordinaryCredential9!", 2]]
        ),
    )

    assert policy.result_has_sensitive_source(result) is False
    assert (
        policy.result_has_sensitive_source(
            result, sensitive_columns=("dbpasswordtext", "dbPasswordText")
        )
        is True
    )


def test_result_data_redacts_normalized_catalog_column() -> None:
    data = ToolResultData(
        columns=["dbpasswordtext", "count"],
        rows=[["ordinaryCredential9!", 2]],
    )

    payload = DataTransferPolicy.sharing().result_data(
        data,
        context=DataContext.CURRENT_RESULT,
        sensitive_columns=("dbpasswordtext", "dbPasswordText"),
    )

    assert payload["rows"] == [[REDACTED, 2]]
    assert "ordinaryCredential9!" not in str(payload)


def test_string_result_columns_and_group_by_preserve_sensitivity() -> None:
    marker = "ordinaryCredential9!"
    policy = DataTransferPolicy.sharing()
    result = ToolResult(
        result_id="r_segment",
        status="ok",
        tool="segment_profile",
        params={"table": "accounts", "group_by": ["dbpasswordtext"]},
        data=ToolResultData(
            columns=["dbpasswordtext", "segment_size"],
            rows=[[marker, 3]],
            row_count=1,
        ),
        facts=[
            Fact(
                text=f"Сегмент {{'dbpasswordtext': '{marker}'}}: 3 строк.",
                kind="segment_sample",
                values={"group": {"dbpasswordtext": marker}, "size": 3},
            )
        ],
    )

    names = ("dbpasswordtext", "dbPasswordText")
    assert policy.result_has_sensitive_source(result, sensitive_columns=names) is True
    outputs = policy.sensitive_result_columns(result, sensitive_columns=names)
    assert outputs == {"dbpasswordtext"}
    payload = policy.result_data(
        result.data,
        context=DataContext.CURRENT_RESULT,
        sensitive_columns=outputs,
    )
    safe_fact = policy.sanitize_fact(result.facts[0], sensitive=True)

    assert payload["rows"] == [[REDACTED, 3]]
    assert marker not in str(payload)
    assert marker not in str(safe_fact)
    assert safe_fact["values"]["group"] == {"dbpasswordtext": REDACTED}
    assert safe_fact["values"]["size"] == 3


def test_string_result_columns_alone_are_sensitive() -> None:
    marker = "ordinaryCredential9!"
    result = ToolResult(
        result_id="r_string_columns",
        status="ok",
        tool="segment_profile",
        params={"table": "accounts"},
        data=ToolResultData(columns=["dbpasswordtext", "segment_size"], rows=[[marker, 3]]),
    )

    assert (
        DataTransferPolicy.sharing().result_has_sensitive_source(
            result, sensitive_columns=("dbpasswordtext", "dbPasswordText")
        )
        is True
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT dbpasswordtext AS value FROM accounts",
        "SELECT p AS value FROM (SELECT dbpasswordtext AS p FROM accounts) t",
        "WITH c AS (SELECT dbpasswordtext AS p FROM accounts) SELECT p AS value FROM c",
    ],
    ids=["alias", "subquery", "cte"],
)
def test_sql_projection_alias_preserves_sensitive_source(sql: str) -> None:
    marker = "ordinaryCredential9!"
    policy = DataTransferPolicy.sharing()
    result = ToolResult(
        result_id="r_sql_alias",
        status="ok",
        tool="run_sql",
        params={"sql": sql},
        data=ToolResultData(columns=["value"], rows=[[marker]], row_count=1),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    outputs = policy.sensitive_result_columns(result, sensitive_columns=names)

    assert outputs == {"value"}
    assert policy.result_has_sensitive_source(result, sensitive_columns=names) is True
    payload = policy.result_data(
        result.data,
        context=DataContext.CURRENT_RESULT,
        sensitive_columns=outputs,
    )
    assert payload["rows"] == [[REDACTED]]
    assert marker not in str(payload)


def test_sql_alias_of_heuristic_sensitive_column_is_sensitive() -> None:
    result = ToolResult(
        result_id="r_sql_heuristic_alias",
        status="ok",
        tool="run_sql",
        params={"sql": "SELECT password AS value FROM users"},
        data=ToolResultData(columns=["value"], rows=[["ordinaryCredential9!"]], row_count=1),
    )

    outputs = DataTransferPolicy.sharing().sensitive_result_columns(result)

    assert outputs == {"value"}
    assert DataTransferPolicy.sharing().result_has_sensitive_source(result) is True


def test_sql_alias_of_non_sensitive_column_is_not_sensitive() -> None:
    result = ToolResult(
        result_id="r_sql_safe_alias",
        status="ok",
        tool="run_sql",
        params={"sql": "SELECT name AS value FROM accounts"},
        data=ToolResultData(columns=["value"], rows=[["visible"]], row_count=1),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    assert (
        DataTransferPolicy.sharing().sensitive_result_columns(result, sensitive_columns=names)
        == set()
    )
    assert (
        DataTransferPolicy.sharing().result_has_sensitive_source(result, sensitive_columns=names)
        is False
    )


@pytest.mark.parametrize(
    "sql",
    [
        "WITH c(p) AS (SELECT dbpasswordtext FROM accounts) SELECT p AS value FROM c",
        "SELECT p AS value FROM (SELECT dbpasswordtext FROM accounts) t(p)",
        (
            "WITH c(p) AS (SELECT dbpasswordtext FROM accounts "
            "UNION ALL SELECT name FROM other) SELECT p AS value FROM c"
        ),
        "WITH c(p, q) AS (SELECT name, dbpasswordtext FROM accounts) SELECT q AS value FROM c",
    ],
    ids=["cte-alias-list", "subquery-alias-list", "union-alias-list", "positional-alias-list"],
)
def test_sql_column_alias_list_preserves_sensitive_source(sql: str) -> None:
    marker = "ordinaryCredential9!"
    policy = DataTransferPolicy.sharing()
    result = ToolResult(
        result_id="r_sql_column_alias",
        status="ok",
        tool="run_sql",
        params={"sql": sql},
        data=ToolResultData(columns=["value"], rows=[[marker]], row_count=1),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    outputs = policy.sensitive_result_columns(result, sensitive_columns=names)

    assert outputs == {"value"}
    assert policy.result_has_sensitive_source(result, sensitive_columns=names) is True
    payload = policy.result_data(
        result.data,
        context=DataContext.CURRENT_RESULT,
        sensitive_columns=outputs,
    )
    assert payload["rows"] == [[REDACTED]]
    assert marker not in str(payload)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH c(p, q) AS (SELECT name, dbpasswordtext FROM accounts) SELECT p AS value FROM c",
        "SELECT p AS value FROM (SELECT name, dbpasswordtext FROM accounts) t(p, q)",
    ],
    ids=["cte", "subquery"],
)
def test_sql_column_alias_list_of_non_sensitive_column_is_not_sensitive(sql: str) -> None:
    result = ToolResult(
        result_id="r_sql_column_alias_safe",
        status="ok",
        tool="run_sql",
        params={"sql": sql},
        data=ToolResultData(columns=["value"], rows=[["visible"]], row_count=1),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    assert (
        DataTransferPolicy.sharing().sensitive_result_columns(result, sensitive_columns=names)
        == set()
    )
    assert (
        DataTransferPolicy.sharing().result_has_sensitive_source(result, sensitive_columns=names)
        is False
    )


@pytest.mark.parametrize(
    ("sql", "columns", "expected"),
    [
        (
            "SELECT x FROM (SELECT * FROM (SELECT dbpasswordtext AS x FROM accounts) t) u",
            ["x"],
            {"x"},
        ),
        (
            "SELECT name FROM (SELECT * FROM (SELECT dbpasswordtext AS x, name FROM accounts) t) u",
            ["name"],
            set(),
        ),
    ],
    ids=["sensitive-star", "safe-star"],
)
def test_sql_nested_star_preserves_sensitive_source(
    sql: str, columns: list[str], expected: set[str]
) -> None:
    result = ToolResult(
        result_id="r_sql_nested_star",
        status="ok",
        tool="run_sql",
        params={"sql": sql},
        data=ToolResultData(columns=columns, rows=[["ordinaryCredential9!"]], row_count=1),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    assert (
        DataTransferPolicy.sharing().sensitive_result_columns(result, sensitive_columns=names)
        == expected
    )


def test_sql_generated_output_name_of_sensitive_expression_is_redacted() -> None:
    marker = "ordinaryCredential9!"
    result = ToolResult(
        result_id="r_sql_generated_name",
        status="ok",
        tool="run_sql",
        params={
            "sql": (
                "SELECT \"(dbpasswordtext || '')\" AS value "
                "FROM (SELECT dbpasswordtext || '' FROM accounts) t"
            )
        },
        data=ToolResultData(columns=["value"], rows=[[marker]], row_count=1),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    outputs = DataTransferPolicy.sharing().sensitive_result_columns(result, sensitive_columns=names)

    assert outputs == {"value"}
    assert (
        DataTransferPolicy.sharing().result_has_sensitive_source(result, sensitive_columns=names)
        is True
    )


@pytest.mark.parametrize(
    ("sql", "columns"),
    [
        (
            "SELECT * REPLACE (dbpasswordtext AS name) FROM accounts",
            ["dbpasswordtext", "name"],
        ),
        (
            "SELECT * RENAME (dbpasswordtext AS name) FROM accounts",
            ["name"],
        ),
    ],
    ids=["replace", "rename"],
)
def test_sql_star_modifier_preserves_sensitive_source(sql: str, columns: list[str]) -> None:
    result = ToolResult(
        result_id="r_sql_star_modifier",
        status="ok",
        tool="run_sql",
        params={"sql": sql},
        data=ToolResultData(columns=columns, rows=[["ordinaryCredential9!"]] * len(columns)),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    outputs = DataTransferPolicy.sharing().sensitive_result_columns(result, sensitive_columns=names)

    assert set(columns) <= outputs
    assert (
        DataTransferPolicy.sharing().result_has_sensitive_source(result, sensitive_columns=names)
        is True
    )


def test_sql_star_replace_of_non_sensitive_expression_is_not_sensitive() -> None:
    result = ToolResult(
        result_id="r_sql_star_replace_safe",
        status="ok",
        tool="run_sql",
        params={"sql": "SELECT * REPLACE (upper(name) AS name) FROM accounts"},
        data=ToolResultData(columns=["name"], rows=[["ALICE"]], row_count=1),
    )

    policy = DataTransferPolicy.sharing()
    assert policy.sensitive_result_columns(result) == set()
    assert policy.result_has_sensitive_source(result) is False


def test_sql_union_with_star_and_sensitive_branch_redacts_all_outputs() -> None:
    marker = "ordinaryCredential9!"
    result = ToolResult(
        result_id="r_sql_union_star",
        status="ok",
        tool="run_sql",
        params={"sql": "SELECT * FROM safe UNION ALL SELECT dbpasswordtext AS value FROM accounts"},
        data=ToolResultData(columns=["value"], rows=[[marker]], row_count=1),
    )

    names = ("dbpasswordtext", "dbPasswordText")
    outputs = DataTransferPolicy.sharing().sensitive_result_columns(result, sensitive_columns=names)

    assert outputs == {"value"}
    assert (
        DataTransferPolicy.sharing().result_has_sensitive_source(result, sensitive_columns=names)
        is True
    )


@pytest.mark.parametrize(
    "text",
    [
        'Connection config: {"password":"ordinaryCredential9!", '
        '"api_key":"reviewFixtureCredential9!"}',
        "Connection config: {“password”:“ordinaryCredential9!”, "
        "‘api_key’:‘reviewFixtureCredential9!’}",
        "password: correct horse battery staple",
    ],
)
def test_json_credentials_are_detected_and_redacted(text: str) -> None:
    policy = DataTransferPolicy.restricted()

    assert policy.contains_secret(text) is True
    safe = policy.sanitize_text(text)
    assert "ordinaryCredential9!" not in safe
    assert "reviewFixtureCredential9!" not in safe
    assert "correct horse battery staple" not in safe

    with pytest.raises(DataTransferRestricted):
        policy.guard("system", text, stage="understand")


@pytest.mark.parametrize(
    "text",
    [
        'password="prefix\\"ESCAPED_PASSWORD_TAIL_173"',
        "password='prefix\\'ESCAPED_PASSWORD_TAIL_173'",
        "password: “prefix\\”ESCAPED_PASSWORD_TAIL_173”",
        "password: ‘prefix\\’ESCAPED_PASSWORD_TAIL_173’",
    ],
    ids=["double", "single", "smart-double", "smart-single"],
)
def test_escaped_quote_in_non_json_assignment_is_redacted(text: str) -> None:
    policy = DataTransferPolicy.restricted()

    assert policy.contains_secret(text) is True
    safe = policy.sanitize_text(text)

    assert "ESCAPED_PASSWORD_TAIL_173" not in safe
    assert safe == REDACTED


def test_json_value_with_escaped_quote_is_redacted_as_a_whole() -> None:
    policy = DataTransferPolicy.restricted()
    text = json.dumps({"password": 'prefix"ESCAPED_PASSWORD_TAIL_173'})

    assert policy.contains_secret(text) is True
    safe = policy.sanitize_text(text)

    assert "ESCAPED_PASSWORD_TAIL_173" not in safe
    assert json.loads(safe) == {"password": REDACTED}
    assert policy.contains_secret(safe) is False

    with pytest.raises(DataTransferRestricted):
        policy.guard("system", text, stage="understand")


def test_nested_json_with_escaped_quote_is_redacted_structurally() -> None:
    policy = DataTransferPolicy.sharing()
    text = json.dumps(
        {
            "config": {"password": 'prefix"ESCAPED_PASSWORD_TAIL_173'},
            "visible": "ordinary value",
        }
    )

    safe = policy.sanitize_text(text)
    parsed = json.loads(safe)

    assert "ESCAPED_PASSWORD_TAIL_173" not in safe
    assert parsed["config"]["password"] == REDACTED
    assert parsed["visible"] == "ordinary value"


def test_guard_rejects_secrets_with_a_content_free_error() -> None:
    policy = DataTransferPolicy.restricted()

    with pytest.raises(DataTransferRestricted) as raised:
        policy.guard("system", f"password={SECRET}", stage="compose")

    assert raised.value.reason == "secret"
    assert raised.value.stage == "compose"
    assert str(raised.value) == POLICY_RESTRICTED_MESSAGE
    assert SECRET not in str(raised.value)
    policy.guard("system", "clean question", stage="compose")


def test_policy_for_client_prefers_client_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_DATA_SHARING", "true")

    class Client:
        data_policy = DataTransferPolicy.restricted()

    assert policy_for_client(Client()).mode is DataMode.RESTRICTED
    assert policy_for_client(object()).mode is DataMode.SHARING


def test_data_sharing_allowed_reads_environment_then_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_DATA_SHARING", raising=False)
    monkeypatch.setattr("djedai.agent.data_policy.dotenv_values", lambda *args, **kwargs: {})
    assert data_sharing_allowed() is False

    monkeypatch.setattr(
        "djedai.agent.data_policy.dotenv_values",
        lambda *args, **kwargs: {"ALLOW_DATA_SHARING": "true"},
    )
    assert data_sharing_allowed() is True

    monkeypatch.setenv("ALLOW_DATA_SHARING", "false")
    assert data_sharing_allowed() is False
