"""Сколько строк результата видят compose и verify (#233, политика #173)."""

from types import SimpleNamespace

from djedai.agent.data_policy import REDACTED, DataTransferPolicy
from djedai.agent.graph import _results_for_plan, compose, verify
from djedai.contracts.agent import Block, ComposeResponse, Message, VerifyResponse
from djedai.contracts.tool_result import Fact, ToolError, ToolResult, ToolResultData

PRIVATE = "private-cell-marker"


class RecordingClient:
    def __init__(self, policy: DataTransferPolicy) -> None:
        self.data_policy = policy
        self.prompts: dict[str, str] = {}

    def generate_typed_response(self, system, _user, schema, stage=None):
        self.prompts[stage] = system
        data = (
            ComposeResponse(answer_blocks=[Block(type="text", markdown="ok")])
            if schema is ComposeResponse
            else VerifyResponse(is_valid=True, issues=[], corrected_markdown="ok")
        )
        return SimpleNamespace(status="success", data=data, error_type=None)


def _monthly(rows: int, result_id: str = "r_months") -> ToolResult:
    return ToolResult(
        result_id=result_id,
        status="ok",
        tool="time_aggregate",
        params={"table": "jobs", "time_col": "started", "freq": "month"},
        data=ToolResultData(
            columns=["time_bucket", "value"],
            rows=[[f"month-{index:03d}", 1000 + index] for index in range(rows)],
            row_count=rows,
        ),
    )


def _run(state: dict, policy: DataTransferPolicy) -> dict[str, str]:
    client = RecordingClient(policy)
    config = {"configurable": {"client": client}}
    state = {
        "messages": [Message(role="user", content="Покажи все месяцы")],
        "understanding": None,
        **state,
    }
    compose(state, config)
    verify({**state, "answer_blocks": [Block(type="text", markdown="ok")]}, config)
    return client.prompts


def test_compose_and_verify_see_all_24_monthly_points() -> None:
    prompts = _run({"results": [_monthly(24)]}, DataTransferPolicy.restricted())

    for prompt in (prompts["compose"], prompts["verify"]):
        assert "month-000" in prompt
        assert "month-023" in prompt
        assert '"rows_truncated": true' not in prompt
        assert '"truncation_note":' not in prompt


def test_rows_over_the_limit_are_marked_for_compose_and_verify() -> None:
    prompts = _run({"results": [_monthly(130)]}, DataTransferPolicy.restricted())

    for prompt in (prompts["compose"], prompts["verify"]):
        assert "month-099" in prompt
        assert "month-100" not in prompt
        assert '"rows_truncated": true' in prompt
        assert '"rows_shown": 100' in prompt
        assert '"row_count": 130' in prompt
        assert "конце данных" in prompt


def test_raw_rows_keep_the_small_limit() -> None:
    raw = ToolResult(
        result_id="r_raw",
        status="ok",
        tool="run_sql",
        sql="SELECT label FROM jobs",
        params={"sql": "SELECT label FROM jobs"},
        data=ToolResultData(
            columns=["label"],
            rows=[[f"raw-{index:03d}"] for index in range(30)],
            row_count=30,
        ),
    )

    prompts = _run({"results": [raw]}, DataTransferPolicy.restricted())

    assert "raw-019" in prompts["compose"]
    assert "raw-020" not in prompts["compose"]
    assert '"rows_truncated": true' in prompts["compose"]


def test_restricted_history_rows_stay_hidden_even_for_aggregates() -> None:
    fact = Fact(text="Всего бакетов: 24", kind="time_summary", values={"n": 24})
    history = _monthly(24, "r_old").model_copy(update={"facts": [fact]})
    history.data.rows[0][0] = PRIVATE
    state = {"results": [], "history_results": [history], "focus_result_ids": ["r_old"]}

    prompts = _run(state, DataTransferPolicy.restricted())

    assert PRIVATE not in prompts["compose"]
    assert PRIVATE not in prompts["verify"]
    assert '"rows_omitted": "data_policy"' in prompts["compose"]


def test_sensitive_aggregate_is_redacted_and_keeps_small_limit() -> None:
    sql = "SELECT dbPasswordText AS key, COUNT(*) AS value FROM jobs GROUP BY 1"
    result = ToolResult(
        result_id="r_secret",
        status="ok",
        tool="run_sql",
        sql=sql,
        params={"sql": sql},
        data=ToolResultData(
            columns=["key", "value"],
            rows=[[f"{PRIVATE}-{index}", index] for index in range(30)],
            row_count=30,
        ),
    )

    prompts = _run(
        {"results": [result], "sensitive_columns": ("dbpasswordtext",)},
        DataTransferPolicy.sharing(),
    )

    assert PRIVATE not in prompts["compose"]
    assert PRIVATE not in prompts["verify"]
    assert REDACTED in prompts["compose"]
    assert '"rows_shown": 20' in prompts["compose"]


def test_plan_retry_context_includes_hint_without_secrets() -> None:
    dsn = "postgresql://analyst:s3cr3t@db.internal:5432/warehouse"
    failed = ToolResult(
        result_id="r_err",
        status="error",
        tool="run_sql",
        error=ToolError(code="sql_parse_error", message="Сбой", suggestion=f"Кавычки {dsn}"),
    )

    text = _results_for_plan({"results": [failed]}, DataTransferPolicy.restricted())

    assert "Сбой" in text
    assert "Подсказка: Кавычки" in text
    assert "s3cr3t" not in text
