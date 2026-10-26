"""Controlled model endpoint shared by transport and live chat deadline tests.

Also the orders/items/payments dataset shared by metric semantics tests (B2-9) and
the dataset with typing losses and ambiguities for data quality tests (B2-10).
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
                        "VisualizeResponse": {"charts": []},
                        "TableDescriptions": {"table": "orders", "columns": []},
                        "DatasetSummary": {"summary": "Данные заказа"},
                    }
                    content = options.get("body", bodies[schema])
                    if not isinstance(content, str):
                        content = json.dumps(content)
                    body = {
                        "id": "fixture",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": options.get("finish_reason", "stop"),
                            }
                        ],
                    }
                    status = options.get("status", 200)
                    encoded = json.dumps(
                        body
                        if status == 200
                        else options.get("error_body", {"error": {"message": "fixture"}})
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


# Датасет качества (B2-10): заказы 1–3 без суммы, 4–7 с нераспознанной суммой, у заказов
# 10–11 нераспознанная дата; позиции есть только у заказов 1–60, по две на заказ.
QUALITY_EMPTY = range(1, 4)
QUALITY_FAILED = {4: "abc", 5: "n/a", 6: "—", 7: "x1"}
QUALITY_BAD_DATES = {10: "скоро", 11: "31.02.2024"}
QUALITY_ORDERS = 103
QUALITY_ITEMS_UNTIL = 60


def quality_orders() -> list[dict[str, str]]:
    rows = []
    for i in range(1, QUALITY_ORDERS + 1):
        if i in QUALITY_EMPTY:
            amount = ""
        else:
            amount = QUALITY_FAILED.get(i, f"{i},50")
        date = QUALITY_BAD_DATES.get(i, f"{i % 28 + 1:02}.{1 if i <= 50 else 2:02}.2024")
        region = "north" if i % 2 else "south"
        rows.append(
            {"order_id": f"o{i:03}", "region": region, "amount": amount, "order_date": date}
        )
    return rows


def _write_quality_csvs(directory: Path) -> list[Path]:
    def write(name: str, header: list[str], rows: list[list[str]]) -> Path:
        path = directory / name
        lines = [";".join(header), *(";".join(row) for row in rows)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    orders = [list(row.values()) for row in quality_orders()]
    items = [[f"o{i:03}", str(qty)] for i in range(1, QUALITY_ITEMS_UNTIL + 1) for qty in (1, 2)]
    ambiguous_days = [f"{d:02}/{d % 12 + 1:02}/2026" for d in range(1, 13)]
    edges = [
        [
            ambiguous_days[i % 12],  # день и месяц не различить
            "25/03/2026" if i == 0 else ambiguous_days[i % 12],  # формат выбран по одной дате
            "100 ₽" if i % 2 else "$5",  # две валюты
            "0,15" if i == 0 else f"{i}%",  # одно значение без знака процента
            f"{i + 1},234",  # одиночный разделитель
            f"{i + 1} ₽",
            f"${i + 1}",
        ]
        for i in range(20)
    ]
    thresholds = [[str(i) if i < 95 else "?", str(i) if i < 94 else "?"] for i in range(100)]
    return [
        write("orders.csv", ["order_id", "region", "amount", "order_date"], orders),
        write("items.csv", ["order_id", "qty"], items),
        write(
            "edges.csv",
            ["d_ambiguous", "d_partial", "price_mixed", "share", "sep", "rub", "usd"],
            edges,
        ),
        write("thresholds.csv", ["v95", "v94"], thresholds),
    ]


@pytest.fixture
def quality_spec() -> SimpleNamespace:
    """Строки и номера заказов, из которых собран ``quality_dataset``: эталоны тестов."""
    return SimpleNamespace(
        orders=quality_orders(),
        empty=QUALITY_EMPTY,
        failed=QUALITY_FAILED,
        bad_dates=QUALITY_BAD_DATES,
        items_until=QUALITY_ITEMS_UNTIL,
    )


@pytest.fixture(scope="session")
def quality_dataset(tmp_path_factory):
    """Потери и неоднозначности типизации: соединение и каталог (B2-10)."""
    directory = tmp_path_factory.mktemp("quality")
    path = directory / "dataset.duckdb"
    catalog = ingest_csv_dataset(
        [CsvSource(p, p.name) for p in _write_quality_csvs(directory)],
        path,
        dataset_id="ds_quality",
        name="quality",
        created_at=datetime.now(UTC),
    )
    with duckdb.connect(str(path), read_only=True) as connection:
        yield connection, catalog
