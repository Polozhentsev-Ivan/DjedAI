from pathlib import Path

import duckdb
import pytest

from djedai.analytics.tools import run_sql
from djedai.config import LimitSettings, get_limits
from djedai.data.csv_ingest import load_raw_csv
from djedai.data.typed_views import create_typed_view


@pytest.fixture(autouse=True)
def clear_limits_cache():
    get_limits.cache_clear()
    yield
    get_limits.cache_clear()


@pytest.mark.parametrize(
    "field,value",
    [
        ("TYPE_PARSE_THRESHOLD", 0),
        ("TYPE_PARSE_THRESHOLD", 1.1),
        ("RELATION_MIN_OVERLAP", -1),
        ("SQL_MAX_ROWS", 0),
        ("TOOL_TIMEOUT_SECONDS", 0),
        ("TOOL_TIMEOUT_SECONDS", float("inf")),
        ("DASHBOARD_MAX_TOOL_CALLS", 0),
        ("DASHBOARD_MAX_OUTPUT_TOKENS", 0),
        ("MODEL_TIMEOUT_SECONDS", 0),
        ("MODEL_MAX_OUTPUT_TOKENS", 0),
        ("MODEL_CONTEXT_TOKENS", 2048),
        ("MODEL_REASONING_UNDERSTAND", "turbo"),
        ("MODEL_REASONING_VERIFY", "HIGH"),
        ("MODEL_REASONING_SUPPORTED", "maybe"),
    ],
)
def test_invalid_limits_fail_before_execution(field: str, value: float):
    with pytest.raises(ValueError):
        LimitSettings(_env_file=None, **{field: value})


def test_dashboard_output_limit_is_separate_from_chat_limit():
    limits = LimitSettings(_env_file=None)
    assert limits.model_max_output_tokens == 2048
    assert limits.dashboard_max_output_tokens == 4096


def test_reasoning_defaults_keep_the_provider_profile(monkeypatch):
    for name in (
        "MODEL_REASONING_UNDERSTAND",
        "MODEL_REASONING_PLAN",
        "MODEL_REASONING_VISUALIZE",
        "MODEL_REASONING_COMPOSE",
        "MODEL_REASONING_VERIFY",
        "MODEL_REASONING_SUPPORTED",
    ):
        monkeypatch.delenv(name, raising=False)
    limits = LimitSettings(_env_file=None)
    assert limits.model_reasoning_understand == "provider_default"
    assert limits.model_reasoning_plan == "provider_default"
    assert limits.model_reasoning_visualize == "provider_default"
    assert limits.model_reasoning_compose == "provider_default"
    assert limits.model_reasoning_verify == "provider_default"
    assert limits.model_reasoning_supported is False


def test_blank_reasoning_value_uses_provider_default(monkeypatch):
    monkeypatch.setenv("MODEL_REASONING_PLAN", "")
    limits = LimitSettings(_env_file=None)
    assert limits.model_reasoning_plan == "provider_default"


def test_sql_uses_configured_cap_even_when_caller_requests_more(monkeypatch):
    monkeypatch.setenv("SQL_MAX_ROWS", "2")
    result = run_sql("SELECT range FROM range(10) ORDER BY range", max_rows=1000)
    assert result.data.rows == [[0], [1]]
    assert result.data.truncated
    assert result.params["max_rows"] == 2
    assert run_sql("SELECT range FROM range(10)", max_rows=1).data.rows == [[0]]


@pytest.mark.parametrize("threshold,expected", [("0.95", "text"), ("0.75", "integer")])
def test_type_inference_uses_configured_threshold(
    tmp_path: Path, monkeypatch, threshold: str, expected: str
):
    monkeypatch.setenv("TYPE_PARSE_THRESHOLD", threshold)
    source = tmp_path / "measure.csv"
    source.write_text("amount\n10\n20\n30\nunknown\n", encoding="utf-8")
    with duckdb.connect() as connection:
        raw = load_raw_csv(connection, source, header=True)
        view = create_typed_view(connection, raw)
        assert view.columns[0].semantic_type == expected


def test_timeout_interrupts_real_query_in_a_bounded_subprocess(monkeypatch):
    import os
    import subprocess
    import sys

    monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "0.02")
    source_dir = str(Path(__file__).resolve().parents[2] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from djedai.analytics.tools import run_sql; "
            "r = run_sql('SELECT sum(a.range * b.range) FROM range(100000000) a, range(100) b'); "
            "assert r.status == 'error' and 'interrupt' in r.error.message.lower()",
        ],
        env={**os.environ, "PYTHONPATH": source_dir},
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


@pytest.mark.parametrize(
    "threshold,parsed,expected",
    [("0.95", 95, "integer"), ("0.95", 94, "text"), ("0.55", 55, "integer")],
)
def test_threshold_boundary_is_inclusive_and_exact(
    tmp_path: Path, monkeypatch, threshold: str, parsed: int, expected: str
):
    # 0.55 * 100 во float даёт 55.00000000000001: ровно 55 из 100 должно проходить порог
    monkeypatch.setenv("TYPE_PARSE_THRESHOLD", threshold)
    source = tmp_path / "measure.csv"
    rows = [str(n) for n in range(parsed)] + ["n/a"] * (100 - parsed)
    source.write_text("amount\n" + "\n".join(rows) + "\n", encoding="utf-8")
    with duckdb.connect() as connection:
        raw = load_raw_csv(connection, source, header=True)
        view = create_typed_view(connection, raw)
        assert view.columns[0].semantic_type == expected
