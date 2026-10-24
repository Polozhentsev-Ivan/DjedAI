"""Controlled model endpoint shared by transport and live chat deadline tests.

Also the orders/items/payments dataset shared by metric semantics tests (B2-9).
"""

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import duckdb
import pytest
from openai import DefaultAsyncHttpxClient

from djedai.data.service import CsvSource, ingest_csv_dataset

METRIC_FIXTURES = Path(__file__).parents[1] / "fixtures" / "metrics"


@pytest.fixture
def model_endpoint() -> Callable:
    @contextmanager
    def serve(reply: Callable[[int, dict[str, Any]], dict[str, Any]]) -> Iterator[SimpleNamespace]:
        calls: list[dict[str, Any]] = []
        started, peer_closed, stopped = (threading.Event() for _ in range(3))
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    calls.append(payload)
                    number = len(calls)
                options = reply(number, payload)
                started.set()
                try:
                    if options.get("mode") == "stall":
                        self.connection.settimeout(5)
                        if self.connection.recv(1) == b"":
                            peer_closed.set()
                        return
                    schema = payload["response_format"]["json_schema"]["name"]
                    bodies = {
                        "Understanding": {"intent": "data_question"},
                        "PlanResponse": {
                            "plan": [{"tool": "run_sql", "args": {"sql": "SELECT 1"}}]
                        },
                        "ComposeResponse": {
                            "answer_blocks": [{"type": "text", "markdown": "1 строка"}]
                        },
                        "VerifyResponse": {"is_valid": True, "corrected_markdown": ""},
                    }
                    body = {
                        "id": "fixture",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(bodies[schema]),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    status = options.get("status", 200)
                    encoded = json.dumps(
                        body if status == 200 else {"error": {"message": "fixture"}}
                    ).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    for name, value in options.get("headers", {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    if options.get("mode") == "drip":
                        for byte in encoded:
                            if stopped.is_set():
                                return
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.03)
                    else:
                        self.wfile.write(encoded)
                except OSError:
                    peer_closed.set()

            def log_message(self, *_args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch(
                "djedai.agent.transport.DefaultAsyncHttpxClient",
                lambda: DefaultAsyncHttpxClient(trust_env=False),
            ):
                yield SimpleNamespace(
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    calls=calls,
                    started=started,
                    peer_closed=peer_closed,
                )
        finally:
            stopped.set()
            server.shutdown()
            server.server_close()
            thread.join()

    return serve


@pytest.fixture(scope="session")
def metrics_dataset(tmp_path_factory):
    """orders/items/payments из ``tests/fixtures/metrics``: соединение и каталог (B2-9)."""
    path = tmp_path_factory.mktemp("metrics") / "dataset.duckdb"
    catalog = ingest_csv_dataset(
        [CsvSource(p, p.name) for p in sorted(METRIC_FIXTURES.glob("*.csv"))],
        path,
        dataset_id="ds_metrics",
        name="metrics",
        created_at=datetime.now(UTC),
    )
    with duckdb.connect(str(path), read_only=True) as connection:
        yield connection, catalog
