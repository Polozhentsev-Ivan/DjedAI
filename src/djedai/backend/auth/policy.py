"""Bound auth work and keep session responses out of HTTP caches.

Limits belong to one backend process, matching the SQLite deployment contract.
Client addresses come from the ASGI server, never directly from forwarded headers.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from threading import BoundedSemaphore, Lock

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from djedai.contracts import ErrorResponse, ToolError

AUTH_CACHE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
MAX_AUTH_BODY_BYTES = 16 * 1024
AUTH_BODY_TIMEOUT_SECONDS = 10


class AuthRateLimiter:
    """Fixed windows with a bounded number of active clients and concurrent hashes."""

    def __init__(
        self, requests: int, window_seconds: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = Lock()
        self._buckets: OrderedDict[tuple[str, str], tuple[float, int]] = OrderedDict()
        self._slots = BoundedSemaphore(4)

    def check(self, client: str, operation: str) -> int | None:
        """Return Retry-After seconds when blocked; rejected calls do not extend a window."""
        with self._lock:
            now = self._clock()
            while self._buckets:
                started, _ = next(iter(self._buckets.values()))
                if now - started < self.window_seconds:
                    break
                self._buckets.popitem(last=False)
            key = (client, operation)
            if key not in self._buckets and len(self._buckets) >= 4096:
                first, _ = next(iter(self._buckets.values()))
                return max(1, math.ceil(first + self.window_seconds - now))
            started, count = self._buckets.get(key, (now, 0))
            if count >= self.requests:
                return max(1, math.ceil(started + self.window_seconds - now))
            self._buckets[key] = (started, count + 1)
        return None


class AuthPolicyMiddleware:
    """Apply auth-only cache, request-size and rate controls before parsing credentials."""

    def __init__(self, app: ASGIApp, *, prefix: str, limiter: AuthRateLimiter) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/") + "/auth"
        self.private_prefixes = tuple(
            prefix.rstrip("/") + "/" + resource
            for resource in ("auth", "datasets", "chats", "results", "dashboards")
        )
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not any(
            path == prefix or path.startswith(prefix + "/") for prefix in self.private_prefixes
        ):
            await self.app(scope, receive, send)
            return

        async def no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {b"cache-control", b"pragma"}
                ] + [
                    (key.lower().encode(), value.encode())
                    for key, value in AUTH_CACHE_HEADERS.items()
                ]
            await send(message)

        async def reject(status: int, code: str, message: str, retry: int | None = None) -> None:
            payload = ErrorResponse(error=ToolError(code=code, message=message))
            response = JSONResponse(
                payload.model_dump(mode="json"),
                status_code=status,
                headers={"Retry-After": str(retry)} if retry else None,
            )
            await response(scope, receive, no_store)

        guarded = scope["method"] == "POST" and path.rstrip("/") in {
            self.prefix + "/register",
            self.prefix + "/login",
        }
        if not guarded:
            await self.app(scope, receive, no_store)
            return
        client = scope.get("client")
        retry = self.limiter.check(client[0] if client else "unknown", path.rstrip("/"))
        if retry is not None:
            await reject(429, "rate_limit", "Слишком много попыток. Повторите позже", retry)
            return
        if not self.limiter._slots.acquire(blocking=False):
            await reject(429, "rate_limit", "Слишком много одновременных попыток", 1)
            return
        try:
            body = bytearray()
            try:
                async with asyncio.timeout(AUTH_BODY_TIMEOUT_SECONDS):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > MAX_AUTH_BODY_BYTES:
                            await reject(
                                413, "request_too_large", "Слишком большой запрос авторизации"
                            )
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await reject(408, "request_timeout", "Время передачи запроса авторизации истекло")
                return

            async def replay() -> Message:
                nonlocal body
                if body is not None:
                    value, body = bytes(body), None
                    return {"type": "http.request", "body": value, "more_body": False}
                return await receive()

            await self.app(scope, replay, no_store)
        finally:
            self.limiter._slots.release()
