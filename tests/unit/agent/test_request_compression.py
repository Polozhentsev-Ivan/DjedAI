"""DeepInfra receives unchanged JSON while the transport sends a smaller body."""

import gzip
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import pytest
from openai import DefaultAsyncHttpxClient, DefaultHttpxClient

from djedai.agent import transport
from djedai.agent.client import BIModelClient
from djedai.contracts.agent import Understanding
from djedai.execution import ExecutionBudget, TurnCancelled, TurnTimeout

URL = "https://api.deepinfra.com/v1/openai/chat/completions"
SYSTEM = "Контекст каталога и доступные инструменты. " * 1000


@pytest.fixture
def route_deepinfra(monkeypatch):
    """Keep the real SDK origin check; route only the final socket to a local server."""
    original_sync = DefaultHttpxClient._send_single_request
    original_async = DefaultAsyncHttpxClient._send_single_request

    def route(endpoint):
        def redirect(request):
            assert request.url.host == "api.deepinfra.com"
            request.url = type(request.url)(endpoint.base_url + "/chat/completions")

        def sync_send(self, request):
            redirect(request)
            return original_sync(self, request)

        async def async_send(self, request):
            redirect(request)
            return await original_async(self, request)

        monkeypatch.setattr(DefaultHttpxClient, "_send_single_request", sync_send)
        monkeypatch.setattr(DefaultAsyncHttpxClient, "_send_single_request", async_send)

    return route


def model_client(**options):
    return BIModelClient(
        "fixture-key",
        "https://api.deepinfra.com/v1/openai",
        "fixture-model",
        max_output_tokens=2048,
        retry_delay=0.01,
        **options,
    )


def assert_gzipped(endpoint):
    for wire, payload in zip(endpoint.wire_calls, endpoint.calls, strict=True):
        headers = {k.lower(): v for k, v in wire["headers"].items()}
        assert headers.get("content-encoding") == "gzip"
        assert int(headers["content-length"]) == len(wire["body"])
        assert "transfer-encoding" not in headers
        assert json.loads(gzip.decompress(wire["body"])) == payload
        assert len(wire["body"]) < len(json.dumps(payload).encode())


@pytest.mark.parametrize("leased", [True, False])
def test_large_request_preserves_payload_through_the_real_sdk(
    model_endpoint, route_deepinfra, leased
):
    with model_endpoint(lambda *_: {}) as endpoint:
        route_deepinfra(endpoint)
        client = model_client(reasoning={"understand": "none"}, reasoning_supported=True)
        try:
            if leased:
                result = client.generate_typed_response(
                    SYSTEM, "Question", Understanding, stage="understand"
                )
                assert result.status == "success"
            else:
                client.client.chat.completions.create(
                    model=client.model,
                    messages=[{"role": "system", "content": SYSTEM}],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "Understanding"},
                    },
                )
            assert len(endpoint.calls) == 1
            assert_gzipped(endpoint)
            payload = endpoint.calls[0]
            assert payload["model"] == "fixture-model"
            assert payload["messages"][0] == {"role": "system", "content": SYSTEM}
            if leased:
                assert payload["messages"][1] == {"role": "user", "content": "Question"}
                assert (
                    payload["response_format"]["json_schema"]["schema"]
                    == Understanding.model_json_schema()
                )
                assert payload["reasoning_effort"] == "none"
                assert payload["max_tokens"] == 2048
                assert payload["temperature"] == 0.1
        finally:
            client.client.close()


def test_retry_sends_one_gzip_layer_with_the_same_json(model_endpoint, route_deepinfra):
    with model_endpoint(lambda n, _: {"status": 503 if n == 1 else 200}) as endpoint:
        route_deepinfra(endpoint)
        client = model_client(max_retries=1)
        try:
            result = client.generate_typed_response(SYSTEM, "Question", Understanding)
            assert result.status == "success"
            assert len(endpoint.calls) == 2
            assert endpoint.calls[0] == endpoint.calls[1]
            assert_gzipped(endpoint)
        finally:
            client.client.close()


@pytest.mark.parametrize("abort", ["timeout", "cancel"])
def test_compressed_request_keeps_total_deadline_and_cancellation(
    model_endpoint, route_deepinfra, abort
):
    with model_endpoint(lambda *_: {"mode": "stall"}) as endpoint:
        route_deepinfra(endpoint)
        client = model_client(timeout=5, max_retries=3)
        cancelled = threading.Event()
        budget = ExecutionBudget(0.3 if abort == "timeout" else 5, cancelled.is_set)

        def run():
            with budget.activate():
                return client.generate_typed_response(SYSTEM, "Question", Understanding)

        try:
            started = monotonic()
            with ThreadPoolExecutor() as pool:
                task = pool.submit(run)
                assert endpoint.started.wait(1)
                if abort == "cancel":
                    cancelled.set()
                with pytest.raises(TurnTimeout if abort == "timeout" else TurnCancelled):
                    task.result(2)
            assert monotonic() - started < 1.5
            assert endpoint.peer_closed.wait(1)
            assert len(endpoint.calls) == 1
            assert_gzipped(endpoint)
        finally:
            client.client.close()


@pytest.mark.parametrize(
    "body,error", [("not json", "INVALID_JSON"), ({"intent": "unknown"}, "SCHEMA_MISMATCH")]
)
def test_compression_preserves_response_validation(model_endpoint, route_deepinfra, body, error):
    with model_endpoint(lambda *_: {"body": body}) as endpoint:
        route_deepinfra(endpoint)
        client = model_client()
        try:
            result = client.generate_typed_response(SYSTEM, "Question", Understanding)
            assert result.status == "error"
            assert result.error_type == error
            assert_gzipped(endpoint)
        finally:
            client.client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/v1/openai/chat/completions",
        "https://api.deepinfra.com.evil.example/v1/openai/chat/completions",
        "https://sub.api.deepinfra.com/v1/openai/chat/completions",
        "http://api.deepinfra.com/v1/openai/chat/completions",
        "https://api.deepinfra.com:8443/v1/openai/chat/completions",
        "https://api.deepinfra.com/v1/inference/model",
    ],
)
def test_other_origins_and_paths_keep_the_original_request(url):
    with transport.CancellableHttpClient() as http:
        request = http.build_request("POST", url, json={"message": SYSTEM})
        assert transport._gzip_json_request(request) is request


@pytest.mark.parametrize(
    "method,headers,body",
    [
        ("POST", {"Content-Type": "application/json"}, b'{"small":true}'),
        ("POST", {"Content-Type": "text/plain"}, SYSTEM.encode()),
        ("GET", {"Content-Type": "application/json"}, SYSTEM.encode()),
        (
            "POST",
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            gzip.compress(SYSTEM.encode()),
        ),
    ],
    ids=["small", "plain-text", "get", "already-encoded"],
)
def test_small_non_json_or_already_encoded_requests_are_unchanged(method, headers, body):
    with transport.CancellableHttpClient() as http:
        request = http.build_request(method, URL, headers=headers, content=body)
        assert transport._gzip_json_request(request) is request
        assert request.read() == body


def test_preparing_gzip_keeps_the_original_request_and_extensions():
    with transport.CancellableHttpClient() as http:
        request = http.build_request("POST", URL, json={"message": SYSTEM})
        request.extensions["custom_trace"] = "fixture"
        body = request.read()
        compressed = transport._gzip_json_request(request)
        assert compressed is not request
        assert gzip.decompress(compressed.read()) == body
        assert compressed.extensions == request.extensions
        assert request.read() == body
        assert "Content-Encoding" not in request.headers
        assert transport._gzip_json_request(compressed) is compressed


def test_budget_expires_during_compression_without_starting_a_socket(
    model_endpoint,
    route_deepinfra,
    monkeypatch,
):
    original = gzip.compress

    def slow_compress(*a, **kw):
        sleep(0.05)
        return original(*a, **kw)

    monkeypatch.setattr(transport.gzip, "compress", slow_compress)
    with model_endpoint(lambda *_: {}) as endpoint:
        route_deepinfra(endpoint)
        client = model_client()
        try:
            with pytest.raises(TurnTimeout), ExecutionBudget(0.03).activate():
                client.generate_typed_response(SYSTEM, "Question", Understanding)
            assert endpoint.calls == []
        finally:
            client.client.close()
