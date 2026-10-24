"""CLI с настоящими графом, DuckDB, SQLite/Parquet и отдельным процессом проверки.

Только endpoint модели локальный: он проверяет запросы SDK и возвращает заданный JSON.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
@pytest.mark.parametrize(
    "case", ["success", "wrong_answer", "literal_sql", "unused_cte", "shadow_cte", "model_error"]
)
def test_smoke_cli_with_local_model(tmp_path: Path, case: str):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            assert self.path == "/v1/openai/chat/completions"
            assert self.headers["Authorization"] == "Bearer test-only"
            schema = request["response_format"]["json_schema"]["name"]
            if case == "model_error":
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"invalid test key"}}')
                return
            if schema == "Understanding":
                content = {"intent": "data_question", "resolved_question": "Count table rows"}
            elif schema == "PlanResponse":
                sql = (
                    "SELECT 3 AS n"
                    if case == "literal_sql"
                    else 'SELECT COUNT(*) AS n FROM "orders_utf8_comma"'
                )
                if case == "unused_cte":
                    sql = 'WITH unused AS (SELECT COUNT(*) FROM "orders_utf8_comma") SELECT 3 AS n'
                elif case == "shadow_cte":
                    sql = (
                        'WITH "orders_utf8_comma" AS (SELECT * FROM range(3)) '
                        'SELECT COUNT(*) AS n FROM "orders_utf8_comma"'
                    )
                content = {"plan": [{"tool": "run_sql", "args": {"sql": sql}}]}
            elif schema == "ComposeResponse":
                content = {
                    "answer_blocks": [
                        {"type": "text", "markdown": "4" if case == "wrong_answer" else "3"}
                    ]
                }
            else:
                assert schema == "VerifyResponse"
                content = {
                    "is_valid": True,
                    "issues": [],
                    "corrected_markdown": "4" if case == "wrong_answer" else "3",
                }
            body = json.dumps(
                {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": json.dumps(content)},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/smoke_cp1.py"),
                "--csv",
                str(ROOT / "tests/fixtures/csv/orders_utf8_comma.csv"),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}/v1/openai",
                "--env-file",
                str(tmp_path / "missing.env"),
                "--output",
                str(tmp_path / "run"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            env={**os.environ, "DEEPINFRA_API_KEY": "test-only", "PYTHONIOENCODING": "utf-8"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    report = json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8"))
    assert "test-only" not in process.stdout + process.stderr
    if case != "success":
        assert process.returncode == 1, process.stdout + process.stderr
        assert report["status"] == "failed"
        assert not report.get("restart_check")
        return
    assert process.returncode == 0, process.stdout + process.stderr
    assert report["status"] == "passed"
    assert report["expected_count"] == 3
    assert report["restart_check"]["status"] == "passed"
    assert report["restart_check"]["messages"] == 2
    assert report["results"][0]["data"]["rows"] == [[3]]
    assert report["results"][0]["sql"].endswith('FROM "orders_utf8_comma"')
    with sqlite3.connect(tmp_path / "run/app.db") as database:
        assert database.execute("SELECT owner_id FROM chats").fetchall() == [("u_smoke",)]
        assert database.execute("SELECT owner_id FROM results").fetchall() == [("u_smoke",)]
    assert [request["response_format"]["json_schema"]["name"] for request in requests] == [
        "Understanding",
        "PlanResponse",
        "ComposeResponse",
        "VerifyResponse",
    ]
    assert "orders_utf8_comma" in requests[0]["messages"][0]["content"]
    assert "Original question and response requirements" in requests[2]["messages"][1]["content"]
    parquet_files = list((tmp_path / "run/results").glob("*.parquet"))
    assert parquet_files
    parquet_files[0].rename(parquet_files[0].with_suffix(".missing"))
    verify = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/smoke_cp1.py"),
            "--verify-only",
            str(tmp_path / "run/report.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert verify.returncode == 1
    assert "Parquet" in verify.stderr
