"""Cancellable async HTTP behind the existing synchronous SDK interface."""

from __future__ import annotations

import asyncio
import gzip
import threading
from collections.abc import Iterator
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from time import monotonic
from typing import Any

from openai import APITimeoutError, DefaultAsyncHttpxClient, DefaultHttpxClient

from djedai.execution import ExecutionBudget

_attempt: ContextVar[tuple[ExecutionBudget, float, bool] | None] = ContextVar(
    "model_attempt", default=None
)
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _gzip_json_request(request: Any) -> Any:
    """Compress large JSON only for DeepInfra's documented Chat Completions endpoint.

    Rebuild using the SDK's request type: OpenAI uses httpx2, while other clients
    may use httpx. Keep the original request intact for retries and caller hooks.
    """
    url = request.url
    if (
        request.method != "POST"
        or url.scheme != "https"
        or url.host != "api.deepinfra.com"
        or url.port not in {None, 443}
        or url.path != "/v1/openai/chat/completions"
        or "content-encoding" in request.headers
        or request.headers.get("content-type", "").partition(";")[0].strip().lower()
        != "application/json"
    ):
        return request
    body = request.read()
    if len(body) < 4096:
        return request
    compressed = gzip.compress(body, mtime=0)
    if len(compressed) >= len(body):
        return request
    headers = request.headers.copy()
    headers["Content-Encoding"] = "gzip"
    headers["Content-Length"] = str(len(compressed))
    headers.pop("transfer-encoding", None)
    return type(request)(
        request.method,
        url,
        headers=headers,
        content=compressed,
        extensions=request.extensions.copy(),
    )


def _network_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None:
            ready = threading.Event()

            def serve() -> None:
                global _loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                _loop = loop
                ready.set()
                loop.run_forever()

            threading.Thread(target=serve, name="model-network", daemon=True).start()
            ready.wait()
        assert _loop is not None
        return _loop


@contextmanager
def attempt_lease(
    budget: ExecutionBudget, seconds: float, *, read_error_body: bool = False
) -> Iterator[None]:
    budget.check()
    token = _attempt.set((budget, min(monotonic() + seconds, budget.deadline), read_error_body))
    try:
        yield
    finally:
        _attempt.reset(token)


class CancellableHttpClient(DefaultHttpxClient):
    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    def send(self, request: Any, **kwargs: Any) -> Any:
        lease = _attempt.get()
        if lease is None:
            return super().send(_gzip_json_request(request), **kwargs)
        if kwargs.get("stream"):
            raise ValueError("The typed model transport requires a buffered response")
        budget, deadline, read_error_body = lease
        closed = threading.Event()

        def check_attempt() -> None:
            budget.check()
            if monotonic() >= deadline:
                raise APITimeoutError(request=request)

        async def perform() -> Any:
            try:
                check_attempt()
                async with DefaultAsyncHttpxClient() as client:
                    check_attempt()
                    response = await client.send(request, **{**kwargs, "stream": True})
                    try:
                        if response.status_code >= 400 and not (
                            read_error_body and response.status_code in {400, 422}
                        ):
                            # Retry policy needs status/headers, not an unbounded error
                            # body. A closed buffered response prevents SDK reads too.
                            headers = {
                                key: value
                                for key, value in response.headers.items()
                                if key.lower()
                                not in {"content-length", "content-encoding", "transfer-encoding"}
                            }
                            return type(response)(
                                response.status_code,
                                headers=headers,
                                content=b"",
                                request=request,
                            )
                        await response.aread()
                        return response
                    finally:
                        await response.aclose()
            finally:
                closed.set()

        check_attempt()
        request = _gzip_json_request(request)
        check_attempt()
        future = asyncio.run_coroutine_threadsafe(perform(), _network_loop())
        try:
            while True:
                check_attempt()
                try:
                    response = future.result(timeout=max(0.0, min(0.05, deadline - monotonic())))
                    budget.check()
                    return response
                except FutureTimeout:
                    if future.done():
                        # Distinguish a transport timeout from waiting on the future.
                        return future.result()
        finally:
            if not future.done():
                future.cancel()
            # Cancellation closes the async response/socket without waiting for the
            # provider. A blocked OS DNS resolver does not hold the chat worker.
            closed.wait(1.0)
