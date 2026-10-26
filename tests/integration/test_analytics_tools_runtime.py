"""Registry-wide auth/CSV/graph/SQL/storage acceptance gate (#171).

Only inference is scripted. Forwarding observers capture dispatch scope, real
SQL refusals and the pre-serialization result for comparison after an application
restart. Fault injection is confined to the mutation tests below.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median, pstdev

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from djedai.agent.client import ModelResponse
from djedai.analytics import queries, registry, tools
from djedai.analytics.scope import DatasetScopeError
from djedai.backend import chat_service
from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.backend.db.models import ToolResultModel
from djedai.execution import current_budget

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "protected-runtime-test-password"
FILTER = "ts >= DATE '2026-01-08'"
NESTED_FILTER = FILTER + " AND EXISTS (SELECT 1 FROM events AS permitted WHERE permitted.g = 1)"
EVENTS = [
    (i + 1, date(2026, 1, 1) + timedelta(days=i), 1 + i % 2, 100 if i == 40 else 10 + i % 3)
    for i in range(84)
]
# Explicit fixtures, not a copy of the registry: a new registered tool must fail
# the coverage check until it has both a real scenario and an independent oracle.
CASES = {
    "run_sql": {"sql": "SELECT event_id, amount FROM events ORDER BY CAST(event_id AS INTEGER)"},
    "describe_dataset": {},
    "describe_column": {"table": "events", "column": "amount"},
    "value_counts": {"table": "events", "column": "g"},
    "time_aggregate": {"table": "events", "time_col": "ts", "freq": "month", "agg": "count"},
    "find_duplicates": {"table": "events", "columns": ["g", "amount"]},
    "detect_anomalies": {"table": "events", "measure": "amount", "method": "iqr"},
    "seasonality": {
        "table": "events",
        "time_col": "ts",
        "measure": "amount",
        "freq": "day",
        "period": 7,
    },
    "segment_profile": {"table": "events", "group_by": ["g"], "measures": ["amount"]},
    "compare_segments": {
        "table": "events",
        "segment_col": "g",
        "a": "1",
        "b": "2",
        "measures": ["amount"],
    },
    "compute_metric": {"metric_id": "runtime_amount"},
}
SQL_FILTER_TOOLS = {
    "value_counts",
    "time_aggregate",
    "find_duplicates",
    "detect_anomalies",
    "seasonality",
    "segment_profile",
    "compare_segments",
}
TABLE_TOOLS = {name for name, args in CASES.items() if "table" in args}
METADATA = (
    "result_id",
    "status",
    "tool",
    "params",
    "sql",
    "facts",
    "chart_hint",
    "error",
    "source_result_id",
    "duration_ms",
    "metric",
    "quality",
)


class PlannedModel:
    def __init__(self, tool, args):
        self.tool, self.args = tool, args
        self.stages = []

    def generate_typed_response(self, system_prompt, user_prompt, response_model, **kwargs):
        name = response_model.__name__
        self.stages.append(name)
        values = {
            "Understanding": {"intent": "data_question", "resolved_question": user_prompt},
            "PlanResponse": {"plan": [{"tool": self.tool, "args": self.args}]},
            "ComposeResponse": {
                "answer_blocks": [{"type": "text", "markdown": "Расчёт завершён."}]
            },
            "VerifyResponse": {
                "is_valid": True,
                "issues": [],
                "corrected_markdown": "Расчёт завершён.",
            },
        }
        assert name in values, name
        return ModelResponse(status="success", data=response_model.model_validate(values[name]))


@dataclass(frozen=True)
class Seed:
    settings: Settings
    alice: dict
    bob: dict
    dataset: str
    other_dataset: str
    bob_dataset: str
    raw_table: str


@contextmanager
def client_for(seed: Seed, actor: str = "alice") -> Iterator[TestClient]:
    # Every context owns a new FastAPI app, engine, DatasetService and connection.
    with TestClient(create_app(seed.settings)) as client:
        client.headers.update(getattr(seed, actor))
        yield client


def upload(client, filename, content):
    response = client.post("/api/datasets/upload", files={"files": (filename, content, "text/csv")})
    assert response.status_code == 202, response.text
    dataset = response.json()["dataset_id"]
    assert "event: done" in client.get(f"/api/datasets/{dataset}/events").text
    return dataset


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    directory = tmp_path_factory.mktemp("protected-tools")
    settings = Settings(
        _env_file=None,
        cp1_local_mode=False,
        catalog_descriptions=False,
        app_database_url=f"sqlite:///{(directory / 'app.db').as_posix()}",
        datasets_data_dir=str(directory / "datasets"),
        results_data_dir=str(directory / "results"),
        diagnostics_dir=str(directory / "diagnostics"),
        auth_rate_limit_requests=1000,
    )
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(migration, "head")
    with TestClient(create_app(settings)) as client:
        headers = {}
        for actor in ("alice", "bob"):
            credentials = {"username": f"tools-{actor}", "password": PASSWORD}
            response = client.post("/api/auth/register", json=credentials)
            assert response.status_code == 201, response.text
            response = client.post("/api/auth/login", json=credentials)
            assert response.status_code == 200, response.text
            headers[actor] = {"Authorization": f"Bearer {response.json()['access_token']}"}
        client.headers.update(headers["alice"])
        content = "event_id,ts,g,amount\n" + "".join(
            f"{identifier},{ts},{group},{amount}\n" for identifier, ts, group, amount in EVENTS
        )
        dataset = upload(client, "events.csv", content.encode())
        catalog = client.get(f"/api/datasets/{dataset}/catalog").json()
        assert [table["table_id"] for table in catalog["tables"]] == ["events"]
        raw_table = catalog["tables"][0]["raw_table"]
        other_dataset = upload(client, "other_dataset.csv", b"secret\nSAME_OWNER_CANARY\n")
        client.headers.update(headers["bob"])
        bob_dataset = upload(client, "other_owner.csv", b"secret\nOTHER_OWNER_CANARY\n")
        for actor, foreign_dataset, table in (
            ("alice", other_dataset, "other_dataset"),
            ("bob", bob_dataset, "other_owner"),
        ):
            client.headers.update(headers[actor])
            response = client.post(
                f"/api/datasets/{foreign_dataset}/metrics",
                json={
                    "metric_id": f"{table}_count",
                    "title": f"Private {table} metric",
                    "kind": "count",
                    "grain": {"table": table, "key": "secret"},
                    "additivity": "additive",
                    "unit": "count",
                },
            )
            assert response.status_code == 201, response.text
        client.headers.update(headers["alice"])
        definition = {
            "metric_id": "runtime_amount",
            "title": "Сумма тестовых событий",
            "kind": "sum",
            "grain": {"table": "events", "key": "event_id"},
            "measure": {"table": "events", "column": "amount"},
            "additivity": "additive",
            "unit": "money",
            "currency": "RUB",
            "time_column": {"table": "events", "column": "ts"},
        }
        response = client.post(f"/api/datasets/{dataset}/metrics", json=definition)
        assert response.status_code == 201, response.text
        # Denied sources must really exist, so a missing-table failure cannot
        # masquerade as a working scope validator in the mutation checks.
        context = client.app.state.dataset_service.get_context(
            client.get("/api/auth/me").json()["user_id"], dataset
        )
        with context.open_connection() as connection:
            names = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
            assert {raw_table, "_djedai_sources"} <= names
    return Seed(
        settings, headers["alice"], headers["bob"], dataset, other_dataset, bob_dataset, raw_table
    )


class SavedResults(list):
    def __init__(self):
        super().__init__()
        self.dispatches = []
        self.refusals = []


@pytest.fixture
def saved(monkeypatch):
    snapshots = SavedResults()
    original = chat_service.save_tool_result
    original_dispatch = chat_service.execute_tool
    original_validate = queries.validate_dataset_query

    def observe(db, result, *args, **kwargs):
        snapshots.append(result.model_dump(mode="json"))
        return original(db, result, *args, **kwargs)

    def observe_dispatch(tool, args, **kwargs):
        snapshots.dispatches.append(
            (
                kwargs.get("allowed_tables"),
                kwargs.get("connection") is not None,
                current_budget() is not None,
            )
        )
        return original_dispatch(tool, args, **kwargs)

    def observe_scope(query, allowed_tables):
        try:
            return original_validate(query, allowed_tables)
        except DatasetScopeError:
            snapshots.refusals.append((query.sql(dialect="duckdb"), frozenset(allowed_tables)))
            raise

    monkeypatch.setattr(chat_service, "save_tool_result", observe)
    monkeypatch.setattr(chat_service, "execute_tool", observe_dispatch)
    monkeypatch.setattr(queries, "validate_dataset_query", observe_scope)
    monkeypatch.setattr(tools, "validate_dataset_query", observe_scope)
    return snapshots


def assert_registry_coverage():
    registered = [tool.name for tool in registry.TOOLS_REGISTRY]
    assert len(registered) == len(set(registered)), "duplicate registered tool"
    assert set(registered) == set(registry.TOOL_FUNCTIONS) == set(CASES), (
        f"runtime scenarios must match registry: registered={set(registered)}, cases={set(CASES)}"
    )
    schemas = {tool.name: tool.parameters_model.model_fields for tool in registry.TOOLS_REGISTRY}
    assert {name for name, fields in schemas.items() if "filter" in fields} == SQL_FILTER_TOOLS
    assert {name for name, fields in schemas.items() if "table" in fields} == TABLE_TOOLS


def test_every_registered_tool_has_a_protected_runtime_scenario():
    assert_registry_coverage()


def execute_turn(seed, monkeypatch, tool, args, saved):
    saved.clear()
    saved.dispatches.clear()
    saved.refusals.clear()
    model = PlannedModel(tool, args)
    monkeypatch.setattr(chat_service, "get_agent_client", lambda: model)
    with client_for(seed) as client:
        response = client.post("/api/chats", json={"dataset_id": seed.dataset})
        assert response.status_code == 201, response.text
        chat = response.json()["chat_id"]
        response = client.post(f"/api/chats/{chat}/messages", json={"text": "Выполни расчёт"})
        assert response.status_code == 200, response.text
        assert "event: done" in response.text, response.text
        assert "event: error\n" not in response.text, response.text
        assert_no_private_values(response.text)
        assert {"Understanding", "PlanResponse"} <= set(model.stages)
        assert saved, "the graph never persisted a tool result"
        assert saved.dispatches and all(
            scope == frozenset({"events"}) and connected and budgeted
            for scope, connected, budgeted in saved.dispatches
        ), "runtime must forward the selected scope, connection and budget"
        with client.app.state.session_factory() as db:
            records = list(
                db.scalars(select(ToolResultModel).where(ToolResultModel.chat_id == chat))
            )
            assert {record.result_id for record in records} == {item["result_id"] for item in saved}
            assert all(record.tool == tool for record in records)
            assert all(record.params == args for record in records)
        # Read while running, then require identical reads from a new app/engine.
        before = [client.get(f"/api/results/{item['result_id']}").json() for item in saved]
    with client_for(seed) as restarted:
        for expected, initial in zip(saved, before, strict=True):
            result_id = expected["result_id"]
            response = restarted.get(f"/api/results/{result_id}")
            assert response.status_code == 200, response.text
            actual = response.json()
            assert_no_private_values(response.text)
            assert actual == initial, "metadata changed after application restart"
            assert {key: actual[key] for key in METADATA} == {
                key: expected[key] for key in METADATA
            }, "serialized result metadata differs from the actual tool contract"
            data = restarted.get(f"/api/results/{result_id}/data")
            if expected["data"] is None:
                assert data.status_code == 404, data.text
                assert actual["data_ref"] is None
            else:
                assert data.status_code == 200, data.text
                assert data.json() == expected["data"], (
                    "saved rows differ from the actual tool result"
                )
                assert actual["data_ref"] == f"parquet://{result_id}.parquet"
                assert (Path(seed.settings.results_data_dir) / f"{result_id}.parquet").is_file()
            with restarted.app.state.session_factory() as db:
                record = db.get(ToolResultModel, result_id)
                assert record.owner_id == restarted.get("/api/auth/me").json()["user_id"]
                assert record.chat_id == chat
        history = restarted.get(f"/api/chats/{chat}").json()["messages"]
        assert_no_private_values(json.dumps(history))
        assert history[-1]["role"] == "assistant"
        # Failed attempts stay queryable in storage, but are deliberately not
        # presented as usable answer sources in the assistant's focus/history.
        assert set(history[-1]["result_ids"]) == {
            item["result_id"] for item in saved if item["status"] != "error"
        }
    return saved.copy(), chat, model


def fact(result, kind):
    matches = [item["values"] for item in result["facts"] if item["kind"] == kind]
    assert matches, f"missing independent fact {kind}"
    return matches[0]


def assert_success(tool, result, *, filtered=False):
    assert result["status"] == "ok", result
    assert result["error"] is None
    assert result["tool"] == tool
    assert result["facts"], "empty facts cannot replace an analytical result"
    data = result["data"]
    assert data and data["rows"]
    assert data["row_count"] == len(data["rows"])
    assert data["truncated"] is False
    source = EVENTS[7:] if filtered else EVENTS
    rows = data["rows"]
    if tool == "run_sql":
        # More than the 50-row preview: this also proves Parquet, not a cache or
        # a preview-only fallback, was restored after restart.
        assert rows == [[str(identifier), amount] for identifier, _, _, amount in source]
        assert fact(result, "summary")["row_count"] == len(source)
    elif tool == "describe_dataset":
        assert rows == [["events", 84, "event_id, ts, g, amount"]]
        assert fact(result, "dataset_summary") == {"table_count": 1, "tables": ["events"]}
    elif tool == "describe_column":
        assert fact(result, "profile") == {
            "total": 84,
            "non_null": 84,
            "distinct": 4,
            "null_count": 0,
        }
        assert dict(rows) == {10: 28, 11: 27, 12: 28, 100: 1}
    elif tool == "value_counts":
        assert {row[0]: row[1] for row in rows} == dict(Counter(event[2] for event in source))
        assert sum(row[1] for row in rows) == len(source)
    elif tool == "time_aggregate":
        expected = Counter(ts.strftime("%Y-%m-01") for _, ts, _, _ in source)
        assert {row[0][:10]: row[-1] for row in rows} == expected
    elif tool == "find_duplicates":
        counts = Counter((group, amount) for _, _, group, amount in source)
        assert {(row[0], row[1]): row[2] for row in rows} == {
            key: count for key, count in counts.items() if count > 1
        }
    elif tool == "detect_anomalies":
        assert len(rows) == 1 and rows[0][0] == 100
        assert fact(result, "anomaly_summary") == {"total": 1, "above": 1, "below": 0}
    elif tool == "seasonality":
        assert len(rows) == len(source)
        assert [row[1] for row in rows] == [event[3] for event in source]
        assert [row[0][:10] for row in rows] == [event[1].isoformat() for event in source]
        for row in rows:
            assert row[1] == pytest.approx(sum(row[2:]), abs=1e-7)
        assert 0 <= fact(result, "seasonal_summary")["strength_seasonal"] <= 1
    elif tool == "segment_profile":
        assert fact(result, "segment_summary") == {"segment_count": 2, "total_rows": len(source)}
        for group, size, average, mid, low, high, std in rows:
            values = [event[3] for event in source if event[2] == group]
            assert size == len(values)
            assert [average, mid, low, high, std] == pytest.approx(
                [mean(values), median(values), min(values), max(values), pstdev(values)]
            )
    elif tool == "compare_segments":
        expected_counts = Counter(str(event[2]) for event in source)
        summary = fact(result, "compare_summary")
        assert summary["n_a"] == expected_counts["1"]
        assert summary["n_b"] == expected_counts["2"]
        numerical = fact(result, "compare_numeric")
        assert numerical["measure"] == "amount"
        assert numerical["mean_a"] == pytest.approx(mean(e[3] for e in source if e[2] == 1))
        assert numerical["mean_b"] == pytest.approx(mean(e[3] for e in source if e[2] == 2))
    elif tool == "compute_metric":
        assert rows == [[sum(event[3] for event in source)]]
        assert result["metric"]["unit"] == "money"
        assert result["metric"]["currency"] == "RUB"
        assert result["metric"]["metric_id"] == "runtime_amount"
        assert result["metric"]["version"] == 1
        assert fact(result, "metric")["value"] == sum(event[3] for event in source)
    else:
        pytest.fail(f"missing independent oracle for {tool}")


@pytest.mark.parametrize("tool", CASES)
def test_each_tool_survives_protected_runtime_and_restart(seed, monkeypatch, saved, tool):
    results, _, model = execute_turn(seed, monkeypatch, tool, CASES[tool], saved)
    assert len(results) == 1
    assert_success(tool, results[0])
    assert {"ComposeResponse", "VerifyResponse"} <= set(model.stages)


FILTER_CASES = [
    (tool, nested)
    for tool in sorted(SQL_FILTER_TOOLS | {"run_sql", "compute_metric"})
    for nested in (False, True)
    if not (tool == "compute_metric" and nested)
]


@pytest.mark.parametrize("tool,nested", FILTER_CASES)
def test_allowed_filters_change_the_real_population(seed, monkeypatch, saved, tool, nested):
    args = dict(CASES[tool])
    condition = NESTED_FILTER if nested else FILTER
    if tool == "run_sql":
        args["sql"] = (
            f"SELECT event_id, amount FROM events WHERE {condition} "
            "ORDER BY CAST(event_id AS INTEGER)"
        )
    elif tool == "compute_metric":
        args["filters"] = [{"column": "events.ts", "op": "gte", "value": "2026-01-08"}]
    else:
        args["filter"] = condition
    results, _, _ = execute_turn(seed, monkeypatch, tool, args, saved)
    assert len(results) == 1
    assert_success(tool, results[0], filtered=True)


def assert_no_private_values(text):
    assert "SAME_OWNER_CANARY" not in text
    assert "OTHER_OWNER_CANARY" not in text


def assert_rejected(results, codes):
    assert results
    for result in results:
        assert result["status"] == "error", result
        assert result["error"]["code"] in codes, result["error"]
        assert result["data"] is None, "a refusal must not include private rows"
        assert result["facts"] == []
        assert_no_private_values(json.dumps(result))


@pytest.mark.parametrize("tool", sorted(TABLE_TOOLS | {"run_sql", "compute_metric"}))
@pytest.mark.parametrize("target", ["missing", "raw", "internal", "other_dataset", "other_owner"])
def test_direct_sources_outside_the_selected_catalog_are_rejected(
    seed, monkeypatch, saved, tool, target
):
    table = {"raw": seed.raw_table, "internal": "_djedai_sources"}.get(target, target)
    args = dict(CASES[tool])
    if tool == "run_sql":
        args["sql"] = f'SELECT * FROM "{table}"'
        codes = {"dataset_scope_violation"}
    elif tool == "compute_metric":
        args["group_by"] = [f"{table}.g"]
        codes = {"invalid_column"}
    else:
        args["table"] = table
        codes = {"execution_failed"}
    results, _, _ = execute_turn(seed, monkeypatch, tool, args, saved)
    assert_rejected(results, codes)


@pytest.mark.parametrize("tool", sorted(SQL_FILTER_TOOLS | {"run_sql"}))
@pytest.mark.parametrize(
    "target",
    ["missing", "raw", "internal", "information_schema.tables", "other_dataset", "other_owner"],
)
def test_nested_select_is_validated_in_final_sql(seed, monkeypatch, saved, tool, target):
    table = {"raw": seed.raw_table, "internal": "_djedai_sources"}.get(target, target)
    identifier = table if table == "information_schema.tables" else f'"{table}"'
    condition = f"EXISTS (SELECT 1 FROM {identifier})"
    args = dict(CASES[tool])
    if tool == "run_sql":
        args["sql"] = f"SELECT event_id, amount FROM events WHERE {condition}"
    else:
        args["filter"] = condition
    results, _, _ = execute_turn(seed, monkeypatch, tool, args, saved)
    # value_counts historically wraps DatasetScopeError as db_error. Require
    # proof of the actual scope refusal, not just an unrelated database failure.
    code = "db_error" if tool == "value_counts" else "dataset_scope_violation"
    assert_rejected(results, {code})
    assert saved.refusals, "the final SQL was never rejected by the real scope validator"
    assert all(scope == frozenset({"events"}) for _, scope in saved.refusals)
    assert all(table in sql for sql, _ in saved.refusals)


@pytest.mark.parametrize("tool", CASES)
def test_authentication_and_foreign_owner_block_before_inference(seed, monkeypatch, tool):
    model = PlannedModel(tool, CASES[tool])
    monkeypatch.setattr(chat_service, "get_agent_client", lambda: model)
    with client_for(seed) as client:
        chat = client.post("/api/chats", json={"dataset_id": seed.dataset}).json()["chat_id"]
        client.headers.clear()
        assert (
            client.post(f"/api/chats/{chat}/messages", json={"text": "Выполни расчёт"}).status_code
            == 401
        )
        client.headers.update(seed.bob)
        foreign = client.post(f"/api/chats/{chat}/messages", json={"text": "Выполни расчёт"})
        missing = client.post("/api/chats/absent/messages", json={"text": "Выполни расчёт"})
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        assert client.post("/api/chats", json={"dataset_id": seed.dataset}).status_code == 404
        assert client.get(f"/api/datasets/{seed.dataset}/catalog").status_code == 404
    assert model.stages == []


@pytest.mark.parametrize("tool", CASES)
def test_foreign_results_are_not_readable_after_restart(seed, monkeypatch, saved, tool):
    results, chat, _ = execute_turn(seed, monkeypatch, tool, CASES[tool], saved)
    result_id = results[0]["result_id"]
    with client_for(seed, "bob") as client:
        for suffix in ("", "/data"):
            foreign = client.get(f"/api/results/{result_id}{suffix}")
            missing = client.get(f"/api/results/absent{suffix}")
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json()
        assert client.get(f"/api/chats/{chat}").status_code == 404


def test_empty_and_domain_error_are_not_saved_as_empty_success(seed, monkeypatch, saved):
    empty_args = {"table": "events", "columns": ["event_id"]}
    results, _, _ = execute_turn(seed, monkeypatch, "find_duplicates", empty_args, saved)
    assert len(results) == 1
    assert results[0]["status"] == "empty"
    assert results[0]["data"]["rows"] == []
    assert fact(results[0], "duplicate_summary")["duplicate_groups"] == 0
    results, _, _ = execute_turn(
        seed, monkeypatch, "describe_column", {"table": "events", "column": "missing"}, saved
    )
    assert_rejected(results, {"invalid_column"})


def test_anomaly_filter_really_removes_the_outlier(seed, monkeypatch, saved):
    args = {**CASES["detect_anomalies"], "filter": "ts >= DATE '2026-03-01'"}
    results, _, _ = execute_turn(seed, monkeypatch, "detect_anomalies", args, saved)
    assert len(results) == 1
    assert results[0]["status"] == "empty"
    assert results[0]["data"]["rows"] == []
    assert any(item["kind"] == "empty_result" for item in results[0]["facts"])


@pytest.mark.parametrize("metric_id", ["other_dataset_count", "other_owner_count"])
def test_metric_definitions_from_other_datasets_are_not_loaded(seed, monkeypatch, saved, metric_id):
    results, _, _ = execute_turn(
        seed, monkeypatch, "compute_metric", {"metric_id": metric_id}, saved
    )
    assert_rejected(results, {"metric_undefined"})
    assert all(item["metric"] is None for item in results)


@pytest.mark.parametrize(
    "column", ["other_owner.secret", "events.g) OR EXISTS (SELECT 1 FROM raw_events)--"]
)
def test_metric_structured_filter_rejects_foreign_or_sql_shaped_columns(
    seed, monkeypatch, saved, column
):
    args = {**CASES["compute_metric"], "filters": [{"column": column, "op": "eq", "value": 1}]}
    results, _, _ = execute_turn(seed, monkeypatch, "compute_metric", args, saved)
    assert_rejected(results, {"invalid_column"})


def test_metric_filter_value_is_data_even_when_it_contains_sql(seed, monkeypatch, saved):
    attack = "1' OR EXISTS (SELECT 1 FROM raw_events) --"
    args = {
        **CASES["compute_metric"],
        "filters": [{"column": "events.event_id", "op": "eq", "value": attack}],
    }
    results, _, _ = execute_turn(seed, monkeypatch, "compute_metric", args, saved)
    assert len(results) == 1
    assert results[0]["status"] == "ok"
    assert results[0]["data"]["rows"] == [[None]]
    assert results[0]["metric"]["filters"][0]["value"] == attack


def test_registry_gate_detects_an_uncovered_registration(monkeypatch):
    extra = registry.TOOLS_REGISTRY[0].model_copy(update={"name": "uncovered_tool"})
    monkeypatch.setattr(registry, "TOOLS_REGISTRY", [*registry.TOOLS_REGISTRY, extra])
    monkeypatch.setitem(
        registry.TOOL_FUNCTIONS, "uncovered_tool", registry.TOOL_FUNCTIONS["run_sql"]
    )
    with pytest.raises(AssertionError, match="runtime scenarios must match registry"):
        assert_registry_coverage()


def test_success_oracle_detects_a_legacy_signature_without_scope(seed, monkeypatch, saved):
    # The regression from #152/#153: a registered function cannot accept the
    # server-owned scope argument. It must be visible as a failing success case.
    original = registry.TOOL_FUNCTIONS["describe_column"]

    def legacy(table, column, *, connection=None):
        return original(table, column, connection=connection)

    monkeypatch.setitem(registry.TOOL_FUNCTIONS, "describe_column", legacy)
    results, _, _ = execute_turn(
        seed, monkeypatch, "describe_column", CASES["describe_column"], saved
    )
    assert_rejected(results, {"execution_failed"})
    assert "allowed_tables" in results[0]["error"]["message"]
    with pytest.raises(AssertionError):
        assert_success("describe_column", results[0])


def test_scope_oracle_detects_removed_final_sql_validation(seed, monkeypatch, saved):
    # Keep registry.table validation and the allowed_tables argument intact,
    # removing only validation of the generated SELECT/filter.
    monkeypatch.setattr("djedai.analytics.queries.validate_dataset_query", lambda *args: None)
    args = {**CASES["time_aggregate"], "filter": f"EXISTS (SELECT 1 FROM {seed.raw_table})"}
    results, _, _ = execute_turn(seed, monkeypatch, "time_aggregate", args, saved)
    assert results[0]["status"] == "ok"
    with pytest.raises(AssertionError):
        assert_rejected(results, {"dataset_scope_violation"})


def test_runtime_oracle_detects_a_dropped_scope_argument(seed, monkeypatch, saved):
    original_dispatch = chat_service.execute_tool

    def unscoped(tool, args, **kwargs):
        kwargs.pop("allowed_tables", None)
        return original_dispatch(tool, args, **kwargs)

    monkeypatch.setattr(chat_service, "execute_tool", unscoped)
    with pytest.raises(AssertionError, match="runtime must forward the selected scope"):
        execute_turn(seed, monkeypatch, "describe_dataset", CASES["describe_dataset"], saved)


@pytest.mark.parametrize("field", ["facts", "metric", "error"])
def test_roundtrip_oracle_detects_serialization_loss(seed, monkeypatch, saved, field):
    # Save observer is already installed. Alter only its real repository call,
    # leaving the pre-save snapshot and the public assertion unchanged.
    from djedai.backend.db import repositories

    original_save = repositories.save_tool_result

    def corrupt(db, result, *args, **kwargs):
        record = original_save(db, result, *args, **kwargs)
        if field == "facts":
            record.facts = []
        elif field == "metric":
            record.metric = {**record.metric, "unit": "number", "currency": None}
        else:
            record.error = {**record.error, "message": "lost original error"}
        db.commit()
        return record

    # The existing observer captured its original save callable. Replace that
    # callable's DB output with a forwarding observer that preserves the snapshot.
    def observe_corrupt(db, result, *args, **kwargs):
        saved.append(result.model_dump(mode="json"))
        return corrupt(db, result, *args, **kwargs)

    monkeypatch.setattr(chat_service, "save_tool_result", observe_corrupt)
    tool = "compute_metric" if field == "metric" else "describe_column"
    args = CASES[tool] if field != "error" else {"table": "events", "column": "missing"}
    with pytest.raises(AssertionError, match="serialized result metadata differs"):
        execute_turn(seed, monkeypatch, tool, args, saved)
