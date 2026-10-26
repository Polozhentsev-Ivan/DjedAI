"""Единая политика передачи данных (#173): категории, режимы и очистка."""

from __future__ import annotations

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
)
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.tool_result import ChartHint, Fact, ToolError, ToolResult, ToolResultData

SECRET = "sk-live-1234567890abcdef"
DSN = "postgresql://analyst:s3cr3t@db.internal:5432/warehouse"


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
    [("password", "Пароль"), ("userpassword", "userPassword")],
    ids=["name", "original-name"],
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
