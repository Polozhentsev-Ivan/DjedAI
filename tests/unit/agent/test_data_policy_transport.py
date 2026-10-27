"""Spy transport: every graph/description stage obeys one data policy (#173).

The tests use the real ``BIModelClient`` and a controlled HTTP endpoint, so a
prohibited marker would only be visible if it actually crossed the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from djedai.agent.client import SERVER_ERROR_MESSAGE, BIModelClient
from djedai.agent.data_policy import (
    POLICY_RESTRICTED_MESSAGE,
    REDACTED,
    RESTRICTED_CONTEXT_MESSAGE,
    DataTransferPolicy,
)
from djedai.agent.diagnostics import RunDiagnostics
from djedai.agent.graph import MODEL_ERROR_MARKDOWN, build_agent_graph
from djedai.contracts.agent import Message, Understanding
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.tool_result import ChartHint, Fact, ToolError, ToolResult, ToolResultData
from djedai.data.descriptions import describe_catalog

EXAMPLE = (
    Path(__file__).parents[3] / "src" / "djedai" / "contracts" / "examples" / "dataset_catalog.json"
)
META_MARKER = "META_MARKER_173"
USER_MARKER = "USER_MARKER_173"
CURRENT_CELL_MARKER = "CURRENT_CELL_MARKER_173"
HISTORY_CELL_MARKER = "HISTORY_CELL_MARKER_173"
CURRENT_FREE_TEXT_MARKER = "CURRENT_FREE_TEXT_MARKER_173"
HISTORY_FREE_TEXT_MARKER = "HISTORY_FREE_TEXT_MARKER_173"
FACT_MARKER = "FACT_MARKER_173"
HISTORY_FACT_MARKER = "HISTORY_FACT_MARKER_173"
CURRENT_AGGREGATE_MARKER = "CURRENT_AGGREGATE_MARKER_173"
HISTORY_AGGREGATE_MARKER = "HISTORY_AGGREGATE_MARKER_173"
CATALOG_SAMPLE_MARKER = "CATALOG_SAMPLE_MARKER_173"
COLUMN_PASSWORD_MARKER = "COLUMN_PASSWORD_MARKER_173"
SENSITIVE_STAT_MIN = 89462718
SENSITIVE_STAT_MAX = 89462719
SENSITIVE_PERIOD_MIN = "1990-01-01"
SENSITIVE_PERIOD_MAX = "1991-02-02"
SECRET_MARKER = "SECRET_MARKER_173"
ERROR_SECRET_MARKER = "ERROR_SECRET_MARKER_173"


def _catalog(
    *,
    with_samples: bool = False,
    described: bool = True,
    sensitive_sample: str | None = None,
    sensitive_stats: bool = False,
    sensitive_period: tuple[str, str] | None = None,
    compound_column: tuple[str, str] | None = None,
) -> DatasetCatalog:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["name"] = META_MARKER
    data["summary"] = META_MARKER if described else None
    data["tables"] = [data["tables"][0]]
    data["relations"] = []
    data["transformations"] = []
    data["warnings"] = []
    table = data["tables"][0]
    table["description"] = META_MARKER if described else None
    for column in table["columns"]:
        column["description"] = META_MARKER if described else None
        if with_samples:
            column["sample_values"] = [CATALOG_SAMPLE_MARKER, f"password={SECRET_MARKER}"]
            column["top_values"] = [{"value": CATALOG_SAMPLE_MARKER, "count": 1}]
        else:
            column["sample_values"] = []
            column["top_values"] = []
    if sensitive_sample is not None:
        column = table["columns"][-1]
        column["name"] = "password"
        column["original_name"] = "Пароль"
        column["sample_values"] = [sensitive_sample]
        column["top_values"] = [{"value": sensitive_sample, "count": 1}]
    if sensitive_stats:
        column = table["columns"][3]  # amount
        column["name"] = "password"
        column["original_name"] = "Пароль"
        column["physical_type"] = "BIGINT"
        column["semantic_type"] = "integer"
        column["role"] = "measure"
        column["currency"] = None
        column["sample_values"] = []
        column["top_values"] = []
        column["stats"] = {"min": SENSITIVE_STAT_MIN, "max": SENSITIVE_STAT_MAX}
    if sensitive_period is not None:
        column = table["columns"][2]  # created_at
        column["name"] = "userpassword"
        column["original_name"] = "userPassword"
        column["physical_type"] = "DATE"
        column["semantic_type"] = "date"
        column["role"] = "time"
        column["sample_values"] = []
        column["top_values"] = []
        column["stats"] = {
            "min": sensitive_period[0],
            "max": sensitive_period[1],
            "granularity": "day",
        }
        table["time_columns"] = ["userpassword"]
    if compound_column is not None:
        column = table["columns"][-1]
        column["name"], column["original_name"] = compound_column
        column["sample_values"] = []
        column["top_values"] = []
    return DatasetCatalog.model_validate(data)


def _result(result_id: str, cell: str, free_text: str, fact: str, aggregate: str) -> ToolResult:
    return ToolResult(
        result_id=result_id,
        status="ok",
        tool="run_sql",
        data=ToolResultData(
            columns=["category", "amount", "note"],
            rows=[[cell, 10, free_text], [f"{cell}_second", 20, f"{free_text}_second"]],
            row_count=2,
        ),
        facts=[Fact(text=f"{fact} ({aggregate})", values={"total": 30, "aggregate": aggregate})],
        chart_hint=ChartHint(type="bar", x="category", y=["amount"]),
    )


def _client(endpoint: Any, policy: DataTransferPolicy) -> BIModelClient:
    return BIModelClient(
        "fixture-key",
        endpoint.base_url,
        "fixture-model",
        timeout=5,
        max_retries=0,
        data_policy=policy,
    )


def _body_for(schema: str, *, invalid_verify: bool = False) -> dict[str, Any]:
    if schema == "Understanding":
        return {"intent": "data_question"}
    if schema == "PlanResponse":
        return {"plan": [{"tool": "run_sql", "args": {"sql": "SELECT 1"}}]}
    if schema == "VisualizeResponse":
        return {"charts": []}
    if schema == "ComposeResponse":
        return {"answer_blocks": [{"type": "text", "markdown": "Ответ по фактам"}]}
    if schema == "VerifyResponse":
        return {"is_valid": not invalid_verify, "corrected_markdown": ""}
    raise AssertionError(f"unexpected schema {schema}")


def _reply(
    *, verify_invalid_first: bool = False
) -> Callable[[int, dict[str, Any]], dict[str, Any]]:
    verify_calls = 0

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal verify_calls
        schema = payload["response_format"]["json_schema"]["name"]
        invalid = False
        if schema == "VerifyResponse":
            verify_calls += 1
            invalid = verify_invalid_first and verify_calls == 1
        return {"body": _body_for(schema, invalid_verify=invalid)}

    return reply


def _state(*, history: ToolResult | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "chat_id": "chat_173",
        "dataset_id": "ds_173",
        "messages": [Message(role="user", content=f"{USER_MARKER}: покажи график по категориям")],
    }
    if history is not None:
        state["results"] = [history]
        state["focus_result_ids"] = [history.result_id]
    return state


def _config(
    client: BIModelClient,
    *,
    diagnostics: RunDiagnostics | None = None,
    dispatcher: Callable[[str, dict[str, Any]], ToolResult] | None = None,
    catalog: DatasetCatalog | None = None,
) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": f"thread_{id(client)}",
        "client": client,
        "catalog_loader": lambda _dataset_id: catalog if catalog is not None else _catalog(),
        "data_policy": client.data_policy,
    }
    if diagnostics is not None:
        configurable["diagnostics"] = diagnostics
    if dispatcher is not None:
        configurable["tool_dispatcher"] = dispatcher
    return {"configurable": configurable}


def _network_text(endpoint: Any) -> str:
    return json.dumps(endpoint.calls, ensure_ascii=False)


def _stage_calls(endpoint: Any) -> dict[str, list[dict[str, Any]]]:
    stages: dict[str, list[dict[str, Any]]] = {}
    for call in endpoint.calls:
        schema = call["response_format"]["json_schema"]["name"]
        stages.setdefault(schema, []).append(call)
    return stages


@pytest.mark.parametrize(
    ("policy", "history_visible"),
    [
        (DataTransferPolicy.restricted(), False),
        (DataTransferPolicy.sharing(), True),
    ],
    ids=["restricted", "sharing"],
)
def test_all_graph_stages_use_one_policy(
    model_endpoint: Callable, policy: DataTransferPolicy, history_visible: bool
) -> None:
    history = _result(
        "r_history",
        HISTORY_CELL_MARKER,
        HISTORY_FREE_TEXT_MARKER,
        HISTORY_FACT_MARKER,
        HISTORY_AGGREGATE_MARKER,
    )
    current = _result(
        "r_current",
        CURRENT_CELL_MARKER,
        CURRENT_FREE_TEXT_MARKER,
        FACT_MARKER,
        CURRENT_AGGREGATE_MARKER,
    )
    events: list[dict[str, Any]] = []
    diagnostics = RunDiagnostics(
        events.append,
        owner_id="owner",
        chat_id="chat_173",
        dataset_id="ds_173",
        user_message_id="message_173",
        run_id="run_173",
    )

    with model_endpoint(_reply()) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(history=history),
                config=_config(
                    client,
                    diagnostics=diagnostics,
                    dispatcher=lambda *_: current,
                ),
            )
        finally:
            client.client.close()

    diagnostics.finish("success")
    network = _network_text(endpoint)
    calls = _stage_calls(endpoint)

    assert {"Understanding", "PlanResponse", "VisualizeResponse", "ComposeResponse"} <= set(calls)
    assert {"VerifyResponse"} <= set(calls)
    assert META_MARKER in network
    assert USER_MARKER in network
    assert CURRENT_CELL_MARKER in network
    assert CURRENT_FREE_TEXT_MARKER in network
    assert FACT_MARKER in network
    assert CURRENT_AGGREGATE_MARKER in network
    assert HISTORY_FACT_MARKER in network
    assert HISTORY_AGGREGATE_MARKER in network
    assert (HISTORY_CELL_MARKER in network) is history_visible
    assert (HISTORY_FREE_TEXT_MARKER in network) is history_visible
    assert SECRET_MARKER not in network
    assert ERROR_SECRET_MARKER not in network

    # Current-result rows/free text are allowed in both modes; historical values
    # only in sharing. Facts and aggregates from history remain allowed.
    assert CURRENT_CELL_MARKER in json.dumps(calls["ComposeResponse"], ensure_ascii=False)
    assert CURRENT_FREE_TEXT_MARKER in json.dumps(calls["ComposeResponse"], ensure_ascii=False)
    assert CURRENT_CELL_MARKER in json.dumps(calls["VerifyResponse"], ensure_ascii=False)
    assert CURRENT_FREE_TEXT_MARKER in json.dumps(calls["VerifyResponse"], ensure_ascii=False)
    assert CURRENT_AGGREGATE_MARKER in json.dumps(calls["ComposeResponse"], ensure_ascii=False)
    assert CURRENT_AGGREGATE_MARKER in json.dumps(calls["VerifyResponse"], ensure_ascii=False)
    assert (HISTORY_CELL_MARKER in json.dumps(calls["Understanding"], ensure_ascii=False)) is (
        history_visible
    )
    assert (HISTORY_CELL_MARKER in json.dumps(calls["PlanResponse"], ensure_ascii=False)) is (
        history_visible
    )
    assert HISTORY_AGGREGATE_MARKER in json.dumps(calls["Understanding"], ensure_ascii=False)
    assert HISTORY_AGGREGATE_MARKER in json.dumps(calls["PlanResponse"], ensure_ascii=False)
    assert result["is_verified"] is True
    assert all(block.type != "error" for block in result["answer_blocks"])

    # Diagnostics remain content-free even though the prompts carried all categories.
    assert META_MARKER not in json.dumps(events, ensure_ascii=False)
    assert USER_MARKER not in json.dumps(events, ensure_ascii=False)
    assert CURRENT_CELL_MARKER not in json.dumps(events, ensure_ascii=False)
    assert FACT_MARKER not in json.dumps(events, ensure_ascii=False)
    assert HISTORY_CELL_MARKER not in json.dumps(events, ensure_ascii=False)


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_retry_and_repair_never_resend_error_secrets(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    calls = {"execute": 0}
    success = _result(
        "r_current",
        CURRENT_CELL_MARKER,
        CURRENT_FREE_TEXT_MARKER,
        FACT_MARKER,
        CURRENT_AGGREGATE_MARKER,
    )

    def dispatcher(_tool: str, _args: dict[str, Any]) -> ToolResult:
        calls["execute"] += 1
        if calls["execute"] == 1:
            return ToolResult(
                result_id="r_error",
                status="error",
                tool="run_sql",
                error=ToolError(
                    code="database_error",
                    message=f"password={ERROR_SECRET_MARKER}",
                    suggestion=f"api_key={SECRET_MARKER}",
                ),
            )
        return success

    with model_endpoint(_reply(verify_invalid_first=True)) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(),
                config=_config(client, dispatcher=dispatcher),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    stages = _stage_calls(endpoint)

    assert calls["execute"] == 2
    assert len(stages["PlanResponse"]) == 2
    assert len(stages["ComposeResponse"]) == 2
    assert len(stages["VerifyResponse"]) == 2
    assert ERROR_SECRET_MARKER not in network
    assert SECRET_MARKER not in network
    assert "[REDACTED]" in json.dumps(stages["PlanResponse"][1], ensure_ascii=False)
    assert CURRENT_CELL_MARKER in json.dumps(stages["ComposeResponse"][0], ensure_ascii=False)
    assert CURRENT_CELL_MARKER in json.dumps(stages["ComposeResponse"][1], ensure_ascii=False)
    assert FACT_MARKER in json.dumps(stages["VerifyResponse"][0], ensure_ascii=False)
    assert FACT_MARKER in json.dumps(stages["VerifyResponse"][1], ensure_ascii=False)
    assert all(block.type != "error" for block in result["answer_blocks"])


def test_policy_rejection_happens_before_the_network(model_endpoint: Callable) -> None:
    with model_endpoint(_reply()) as endpoint:
        client = _client(endpoint, DataTransferPolicy.restricted())
        try:
            response = client.generate_typed_response(
                "system", f"password={SECRET_MARKER}", Understanding, stage="understand"
            )
            assert response.status == "error"
            assert response.error_type == "POLICY_RESTRICTED"
            assert response.message == POLICY_RESTRICTED_MESSAGE
            assert SECRET_MARKER not in response.message
            assert endpoint.calls == []

            result = build_agent_graph().invoke(
                _state()
                | {"messages": [Message(role="user", content=f"password={SECRET_MARKER}")]},
                config=_config(client),
            )
        finally:
            client.client.close()

    assert endpoint.calls == []
    assert result["answer_blocks"][0].type == "error"
    assert result["answer_blocks"][0].markdown == RESTRICTED_CONTEXT_MESSAGE
    assert SECRET_MARKER not in result["answer_blocks"][0].markdown


@pytest.mark.parametrize(
    ("policy", "expect_compose"),
    [
        (DataTransferPolicy.restricted(), False),
        (DataTransferPolicy.sharing(), True),
    ],
    ids=["restricted", "sharing"],
)
def test_insufficient_permitted_context_is_explicit(
    model_endpoint: Callable, policy: DataTransferPolicy, expect_compose: bool
) -> None:
    history = ToolResult(
        result_id="r_history",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["category", "amount"], rows=[[HISTORY_CELL_MARKER, 10]]),
    )

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "Understanding":
            return {
                "body": {
                    "intent": "modify_previous",
                    "references": ["r_history"],
                }
            }
        if schema == "PlanResponse":
            return {"body": {"plan": []}}
        if schema == "VisualizeResponse":
            return {"body": {"charts": []}}
        return {"body": _body_for(schema)}

    with model_endpoint(reply) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                {
                    "chat_id": "chat_173",
                    "dataset_id": "ds_173",
                    "messages": [Message(role="user", content="Объясни те же суммы")],
                    "results": [history],
                },
                config=_config(client),
            )
        finally:
            client.client.close()

    schemas = [call["response_format"]["json_schema"]["name"] for call in endpoint.calls]
    if expect_compose:
        assert "ComposeResponse" in schemas
        assert all(block.type != "error" for block in result["answer_blocks"])
    else:
        assert "ComposeResponse" not in schemas
        assert result["answer_blocks"][0].type == "error"
        assert result["answer_blocks"][0].markdown == RESTRICTED_CONTEXT_MESSAGE
        assert HISTORY_CELL_MARKER not in _network_text(endpoint)


def test_restricted_follow_up_uses_history_facts_without_rows(model_endpoint: Callable) -> None:
    history = ToolResult(
        result_id="r_history",
        status="ok",
        tool="run_sql",
        data=ToolResultData(columns=["category", "amount"], rows=[[HISTORY_CELL_MARKER, 10]]),
        facts=[Fact(text=f"aggregate {HISTORY_AGGREGATE_MARKER}", values={"total": 10})],
    )

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "Understanding":
            return {
                "body": {
                    "intent": "modify_previous",
                    "references": ["r_history"],
                }
            }
        if schema == "PlanResponse":
            return {"body": {"plan": []}}
        if schema == "VisualizeResponse":
            return {"body": {"charts": []}}
        return {"body": _body_for(schema)}

    with model_endpoint(reply) as endpoint:
        client = _client(endpoint, DataTransferPolicy.restricted())
        try:
            result = build_agent_graph().invoke(
                {
                    "chat_id": "chat_173",
                    "dataset_id": "ds_173",
                    "messages": [Message(role="user", content="Объясни те же суммы")],
                    "results": [history],
                },
                config=_config(client),
            )
        finally:
            client.client.close()

    calls = _stage_calls(endpoint)
    compose = json.dumps(calls["ComposeResponse"], ensure_ascii=False)
    assert HISTORY_AGGREGATE_MARKER in compose
    assert "rows_omitted" in compose
    assert HISTORY_CELL_MARKER not in compose
    assert all(block.type != "error" for block in result["answer_blocks"])


def test_provider_error_and_graph_error_are_content_free(model_endpoint: Callable) -> None:
    with model_endpoint(
        lambda *_: {
            "status": 500,
            "error_body": {"error": {"message": f"upstream {SECRET_MARKER}"}},
        }
    ) as endpoint:
        client = _client(endpoint, DataTransferPolicy.restricted())
        try:
            response = client.generate_typed_response("system", "question", Understanding)
            assert response.status == "error"
            assert response.error_type == "SERVER_ERROR"
            assert response.message == SERVER_ERROR_MESSAGE
            assert SECRET_MARKER not in response.message

            result = build_agent_graph().invoke(_state(), config=_config(client))
        finally:
            client.client.close()

    assert result["answer_blocks"][0].type == "error"
    assert result["answer_blocks"][0].markdown == MODEL_ERROR_MARKDOWN
    assert SECRET_MARKER not in result["answer_blocks"][0].markdown
    assert SECRET_MARKER not in _network_text(endpoint)


@pytest.mark.parametrize(
    ("policy", "sample_visible"),
    [
        (DataTransferPolicy.restricted(), False),
        (DataTransferPolicy.sharing(), True),
    ],
    ids=["restricted", "sharing"],
)
def test_descriptions_use_the_same_catalog_policy(
    model_endpoint: Callable, policy: DataTransferPolicy, sample_visible: bool
) -> None:
    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "TableDescriptions":
            columns = json.loads(payload["messages"][1]["content"])["columns"]
            return {
                "body": {
                    "table": "orders",
                    "columns": [
                        {"name": column["name"], "description": "Описание колонки"}
                        for column in columns
                    ],
                }
            }
        assert schema == "DatasetSummary"
        return {"body": {"summary": "Описание датасета"}}

    connection = duckdb.connect(":memory:")
    try:
        with model_endpoint(reply) as endpoint:
            client = _client(endpoint, policy)
            try:
                report = describe_catalog(
                    connection, _catalog(with_samples=True, described=False), client
                )
            finally:
                client.client.close()
    finally:
        connection.close()

    network = _network_text(endpoint)
    assert report.model_calls == 2
    assert report.catalog.tables[0].columns[0].description == "Описание колонки"
    assert (CATALOG_SAMPLE_MARKER in network) is sample_visible
    assert SECRET_MARKER not in network
    assert "password=" not in network


def test_sensitive_column_examples_never_reach_network(model_endpoint: Callable) -> None:
    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "TableDescriptions":
            columns = json.loads(payload["messages"][1]["content"])["columns"]
            return {
                "body": {
                    "table": "orders",
                    "columns": [
                        {"name": column["name"], "description": "Описание колонки"}
                        for column in columns
                    ],
                }
            }
        assert schema == "DatasetSummary"
        return {"body": {"summary": "Описание датасета"}}

    connection = duckdb.connect(":memory:")
    try:
        with model_endpoint(reply) as endpoint:
            client = _client(endpoint, DataTransferPolicy.sharing())
            try:
                report = describe_catalog(
                    connection,
                    _catalog(
                        with_samples=True,
                        described=False,
                        sensitive_sample=COLUMN_PASSWORD_MARKER,
                    ),
                    client,
                )
            finally:
                client.client.close()
    finally:
        connection.close()

    network = _network_text(endpoint)
    assert report.model_calls == 2
    assert report.catalog.tables[0].columns[0].description == "Описание колонки"
    assert COLUMN_PASSWORD_MARKER not in network
    assert REDACTED in network
    assert "password=" not in network


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_catalog_sensitive_stats_never_reach_network(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    catalog = _catalog(sensitive_stats=True)

    with model_endpoint(_reply()) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(),
                config=_config(client, catalog=catalog),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    prompt = result["catalog_prompt"]
    assert str(SENSITIVE_STAT_MIN) not in network
    assert str(SENSITIVE_STAT_MAX) not in network
    assert str(SENSITIVE_STAT_MIN) not in prompt
    assert str(SENSITIVE_STAT_MAX) not in prompt
    assert "[REDACTED] … [REDACTED]" in prompt


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_sensitive_column_facts_never_reach_compose(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    marker = "PASSWORD_FACT_MARKER_173"
    described = ToolResult(
        result_id="r_password",
        status="ok",
        tool="describe_column",
        params={"table": "s", "column": "password"},
        data=ToolResultData(columns=["password", "count"], rows=[[marker, 2]], row_count=1),
        facts=[
            Fact(
                text="Всего строк: 2, пропусков: 0, уникальных: 2",
                kind="profile",
                values={"total": 2, "non_null": 2, "distinct": 2, "null_count": 0},
            ),
            Fact(
                text=f"Значение '{marker}' встречается 2 раз.",
                kind="freq",
                values={"value": marker, "count": 2},
            ),
            Fact(
                text=f"Примеры значений: {marker}",
                kind="examples",
                values={"examples": [marker]},
            ),
        ],
    )

    def dispatcher(tool: str, _params: dict[str, Any]) -> ToolResult:
        assert tool == "describe_column"
        return described

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "PlanResponse":
            return {
                "body": {
                    "plan": [
                        {"tool": "describe_column", "args": {"table": "s", "column": "password"}}
                    ]
                }
            }
        return {"body": _body_for(schema)}

    with model_endpoint(reply) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(),
                config=_config(client, dispatcher=dispatcher),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    assert marker not in network
    assert REDACTED in network
    assert all(block.type != "error" for block in result["answer_blocks"])


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_sensitive_period_original_name_never_reaches_summary(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "TableDescriptions":
            columns = json.loads(payload["messages"][1]["content"])["columns"]
            return {
                "body": {
                    "table": "orders",
                    "columns": [
                        {"name": column["name"], "description": "Описание колонки"}
                        for column in columns
                    ],
                }
            }
        assert schema == "DatasetSummary"
        return {"body": {"summary": "Описание датасета"}}

    connection = duckdb.connect(":memory:")
    try:
        with model_endpoint(reply) as endpoint:
            client = _client(endpoint, policy)
            try:
                report = describe_catalog(
                    connection,
                    _catalog(
                        described=False,
                        sensitive_period=(SENSITIVE_PERIOD_MIN, SENSITIVE_PERIOD_MAX),
                    ),
                    client,
                )
            finally:
                client.client.close()
    finally:
        connection.close()

    network = _network_text(endpoint)
    summary_calls = _stage_calls(endpoint)["DatasetSummary"]
    summary_text = json.dumps(summary_calls, ensure_ascii=False)
    assert report.model_calls == 2
    assert SENSITIVE_PERIOD_MIN not in network
    assert SENSITIVE_PERIOD_MAX not in network
    assert SENSITIVE_PERIOD_MIN not in summary_text
    assert SENSITIVE_PERIOD_MAX not in summary_text
    assert REDACTED in summary_text


@pytest.mark.parametrize(
    "text",
    [
        'Connection config: {"password":"ordinaryCredential9!", '
        '"api_key":"reviewFixtureCredential9!"}',
        "Connection config: {“password”:“ordinaryCredential9!”, "
        "‘api_key’:‘reviewFixtureCredential9!’}",
    ],
    ids=["json", "unicode-quotes"],
)
@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_json_credentials_are_rejected_before_network(
    model_endpoint: Callable, policy: DataTransferPolicy, text: str
) -> None:
    with model_endpoint(_reply()) as endpoint:
        client = _client(endpoint, policy)
        try:
            response = client.generate_typed_response(
                "system", text, Understanding, stage="understand"
            )
            assert response.status == "error"
            assert response.error_type == "POLICY_RESTRICTED"
            assert response.message == POLICY_RESTRICTED_MESSAGE
            assert endpoint.calls == []

            result = build_agent_graph().invoke(
                _state() | {"messages": [Message(role="user", content=text)]},
                config=_config(client),
            )
        finally:
            client.client.close()

    assert endpoint.calls == []
    assert result["answer_blocks"][0].type == "error"
    assert result["answer_blocks"][0].markdown == RESTRICTED_CONTEXT_MESSAGE
    assert "ordinaryCredential9!" not in result["answer_blocks"][0].markdown
    assert "reviewFixtureCredential9!" not in result["answer_blocks"][0].markdown


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_normalized_compound_sensitive_column_facts_never_reach_compose(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    marker = "ordinaryCredential9!"
    described = ToolResult(
        result_id="r_db_password",
        status="ok",
        tool="describe_column",
        params={"table": "s", "column": "dbpassword"},
        data=ToolResultData(columns=["dbpassword", "count"], rows=[[marker, 2]], row_count=1),
        facts=[
            Fact(
                text=f"Значение '{marker}' встречается 2 раз.",
                kind="freq",
                values={"value": marker, "count": 2},
            ),
            Fact(
                text=f"Примеры значений: {marker}",
                kind="examples",
                values={"examples": [marker]},
            ),
        ],
    )

    def dispatcher(tool: str, params: dict[str, Any]) -> ToolResult:
        assert tool == "describe_column"
        assert params["column"] == "dbpassword"
        return described

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "PlanResponse":
            return {
                "body": {
                    "plan": [
                        {
                            "tool": "describe_column",
                            "args": {"table": "s", "column": "dbpassword"},
                        }
                    ]
                }
            }
        return {"body": _body_for(schema)}

    with model_endpoint(reply) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(),
                config=_config(client, dispatcher=dispatcher),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    assert marker not in network
    assert REDACTED in network
    assert all(block.type != "error" for block in result["answer_blocks"])


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_original_compound_column_sensitivity_survives_normalization(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    marker = "ordinaryCredential9!"
    described = ToolResult(
        result_id="r_db_password_text",
        status="ok",
        tool="describe_column",
        params={"table": "orders", "column": "dbpasswordtext"},
        data=ToolResultData(columns=["dbpasswordtext", "count"], rows=[[marker, 2]], row_count=1),
        facts=[
            Fact(
                text=f"Значение '{marker}' встречается 2 раз.",
                kind="freq",
                values={"value": marker, "count": 2},
            ),
            Fact(
                text=f"Примеры значений: {marker}",
                kind="examples",
                values={"examples": [marker]},
            ),
        ],
    )

    def dispatcher(tool: str, params: dict[str, Any]) -> ToolResult:
        assert tool == "describe_column"
        assert params["column"] == "dbpasswordtext"
        return described

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "PlanResponse":
            return {
                "body": {
                    "plan": [
                        {
                            "tool": "describe_column",
                            "args": {"table": "orders", "column": "dbpasswordtext"},
                        }
                    ]
                }
            }
        return {"body": _body_for(schema)}

    with model_endpoint(reply) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(),
                config=_config(
                    client,
                    dispatcher=dispatcher,
                    catalog=_catalog(compound_column=("dbpasswordtext", "dbPasswordText")),
                ),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    assert marker not in network
    assert REDACTED in network
    assert all(block.type != "error" for block in result["answer_blocks"])


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_segment_profile_grouped_by_sensitive_column_never_reaches_network(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    marker = "ordinaryCredential9!"
    described = ToolResult(
        result_id="r_segment_password",
        status="ok",
        tool="segment_profile",
        params={
            "table": "orders",
            "group_by": ["dbpasswordtext"],
            "measures": [],
            "filter": None,
            "top_n": 100,
        },
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

    def dispatcher(tool: str, params: dict[str, Any]) -> ToolResult:
        assert tool == "segment_profile"
        assert params["group_by"] == ["dbpasswordtext"]
        return described

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "PlanResponse":
            return {
                "body": {
                    "plan": [
                        {
                            "tool": "segment_profile",
                            "args": {"table": "orders", "group_by": ["dbpasswordtext"]},
                        }
                    ]
                }
            }
        return {"body": _body_for(schema)}

    with model_endpoint(reply) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(),
                config=_config(
                    client,
                    dispatcher=dispatcher,
                    catalog=_catalog(compound_column=("dbpasswordtext", "dbPasswordText")),
                ),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    assert marker not in network
    assert REDACTED in network
    assert all(block.type != "error" for block in result["answer_blocks"])


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT dbpasswordtext AS value FROM accounts",
        "WITH c(p) AS (SELECT dbpasswordtext FROM accounts) SELECT p AS value FROM c",
    ],
    ids=["alias", "cte-alias-list"],
)
def test_sql_alias_of_sensitive_column_never_reaches_network(
    model_endpoint: Callable, policy: DataTransferPolicy, sql: str
) -> None:
    marker = "ordinaryCredential9!"
    described = ToolResult(
        result_id="r_sql_alias_password",
        status="ok",
        tool="run_sql",
        params={"sql": sql, "max_rows": 100},
        data=ToolResultData(columns=["value"], rows=[[marker]], row_count=1),
        facts=[Fact(text="Запрос выполнен успешно, получено строк: 1", values={"row_count": 1})],
    )

    def dispatcher(tool: str, params: dict[str, Any]) -> ToolResult:
        assert tool == "run_sql"
        assert params["sql"] == sql
        return described

    def reply(_number: int, payload: dict[str, Any]) -> dict[str, Any]:
        schema = payload["response_format"]["json_schema"]["name"]
        if schema == "PlanResponse":
            return {"body": {"plan": [{"tool": "run_sql", "args": {"sql": sql}}]}}
        return {"body": _body_for(schema)}

    with model_endpoint(reply) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state(),
                config=_config(
                    client,
                    dispatcher=dispatcher,
                    catalog=_catalog(compound_column=("dbpasswordtext", "dbPasswordText")),
                ),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    assert marker not in network
    assert REDACTED in network
    assert all(block.type != "error" for block in result["answer_blocks"])


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_private_key_in_history_never_reaches_network(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    body = pem.splitlines()[1]
    text = "P" * 140 + " " + pem
    assert len(text) > 250

    with model_endpoint(_reply()) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state()
                | {
                    "messages": [
                        Message(role="user", content=text),
                        Message(role="user", content=f"{USER_MARKER}: покажи график"),
                    ]
                },
                config=_config(client),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    assert endpoint.calls
    assert body[:40] not in network
    assert "PRIVATE KEY" not in network
    assert "[REDACTED]" in network
    assert all(block.type != "error" for block in result["answer_blocks"])


@pytest.mark.parametrize(
    "policy",
    [DataTransferPolicy.restricted(), DataTransferPolicy.sharing()],
    ids=["restricted", "sharing"],
)
def test_json_escaped_quote_in_history_never_reaches_network(
    model_endpoint: Callable, policy: DataTransferPolicy
) -> None:
    text = json.dumps({"password": 'prefix"ESCAPED_PASSWORD_TAIL_173'})

    with model_endpoint(_reply()) as endpoint:
        client = _client(endpoint, policy)
        try:
            result = build_agent_graph().invoke(
                _state()
                | {
                    "messages": [
                        Message(role="user", content=text),
                        Message(role="user", content=f"{USER_MARKER}: покажи график"),
                    ]
                },
                config=_config(client),
            )
        finally:
            client.client.close()

    network = _network_text(endpoint)
    assert endpoint.calls
    assert "ESCAPED_PASSWORD_TAIL_173" not in network
    assert "[REDACTED]" in network
    assert all(block.type != "error" for block in result["answer_blocks"])
