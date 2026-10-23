"""Content-free diagnostics shared by graph nodes and the model transport."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any
from uuid import uuid4

from openai import DefaultHttpxClient

NODES = frozenset(
    {"load_ctx", "understand", "plan", "execute", "visualize", "compose", "verify", "clarify"}
)
_call: ContextVar[ModelCall | None] = ContextVar("diagnostic_model_call", default=None)
_run: ContextVar[RunDiagnostics | None] = ContextVar("diagnostic_run", default=None)


def _tokens(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass(frozen=True)
class Tariff:
    """Operator-supplied dated standard rates; never claims an actual charge."""

    input_usd: Decimal
    output_usd: Decimal
    reference: str

    def estimate(self, usage: dict[str, int | None]) -> str | None:
        incoming, outgoing = usage["input_tokens"], usage["output_tokens"]
        if incoming is None or outgoing is None:
            return None
        return str((incoming * self.input_usd + outgoing * self.output_usd) / 1_000_000)


class RunDiagnostics:
    def __init__(
        self,
        sink: Callable[[dict[str, Any]], None],
        *,
        owner_id: str,
        chat_id: str,
        dataset_id: str,
        user_message_id: str,
        tariff: Tariff | None = None,
    ) -> None:
        self.sink = sink
        self.tariff = tariff
        self.run_id = f"run_{uuid4().hex}"
        self.context = dict(
            run_id=self.run_id,
            owner_id=owner_id,
            chat_id=chat_id,
            dataset_id=dataset_id,
            user_message_id=user_message_id,
        )
        self.started = perf_counter()
        self.node: str | None = None
        self.last_error: str | None = None
        self.error_node: str | None = None
        self.error_count = 0
        self.finished = False
        self.trace: list[dict[str, Any]] = []
        self.emit("run_started")

    def emit(self, event: str, **fields: Any) -> None:
        self.sink(
            {
                "version": 1,
                "timestamp": datetime.now(UTC).isoformat(),
                **self.context,
                "event": event,
                "node": self.node,
                **fields,
            }
        )

    @contextmanager
    def stage(self, node: str) -> Iterator[None]:
        self.node = node if node in NODES else "unknown"
        token = _run.set(self)
        started = perf_counter()
        previous_errors = self.error_count
        error: str | None = None
        exception_type: str | None = None
        self.emit("stage_started")
        try:
            yield
        except Exception as exc:
            error = "STAGE_EXCEPTION"
            exception_type = type(exc).__name__
            self.last_error = error
            self.error_node = self.node
            self.error_count += 1
            raise
        finally:
            if self.error_count != previous_errors:
                error = self.last_error
            duration = round((perf_counter() - started) * 1000, 3)
            self.trace.append({"node": self.node, "duration_ms": duration, "error_type": error})
            self.emit(
                "stage_finished",
                duration_ms=duration,
                error_type=error,
                exception_type=exception_type,
            )
            _run.reset(token)

    def finish(self, status: str, error_type: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        self.emit(
            "run_finished",
            status=status,
            error_type=error_type,
            error_node=self.error_node if error_type else None,
            duration_ms=round((perf_counter() - self.started) * 1000, 3),
            trace=self.trace,
        )


class ModelCall:
    def __init__(self, run: RunDiagnostics, model: str, schema: str) -> None:
        self.run = run
        self.call_id = f"call_{uuid4().hex}"
        self.model = model
        self.schema = schema
        self.attempt = 0
        self.started = perf_counter()
        self.usage: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
        }
        self.emit("model_call_started")

    def emit(self, event: str, **fields: Any) -> None:
        self.run.emit(event, call_id=self.call_id, model=self.model, schema=self.schema, **fields)

    def capture_usage(self, usage: Any) -> None:
        if usage is not None:
            self.usage = {
                "input_tokens": _tokens(getattr(usage, "prompt_tokens", None)),
                "output_tokens": _tokens(getattr(usage, "completion_tokens", None)),
                "reasoning_tokens": _tokens(
                    getattr(
                        getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
                    )
                ),
            }

    def finish(self, error_type: str | None) -> None:
        if error_type:
            self.run.last_error = error_type
            self.run.error_node = self.run.node
            self.run.error_count += 1
        tariff = self.run.tariff
        self.emit(
            "model_call_finished",
            status="error" if error_type else "success",
            error_type=error_type,
            duration_ms=round((perf_counter() - self.started) * 1000, 3),
            attempts=self.attempt,
            **self.usage,
            estimated_cost_usd=tariff.estimate(self.usage) if tariff else None,
            tariff={
                "input_usd_per_million": str(tariff.input_usd),
                "output_usd_per_million": str(tariff.output_usd),
                "reference": tariff.reference,
                "assumption": "standard_rates_without_cache_discounts",
            }
            if tariff
            else None,
            actual_charge_usd=None,
        )


@contextmanager
def model_call(model: str, schema: str) -> Iterator[ModelCall | None]:
    run = _run.get()
    call = ModelCall(run, model, schema) if run is not None else None
    token = _call.set(call)
    try:
        yield call
    finally:
        _call.reset(token)


def capture_usage(usage: Any) -> None:
    if call := _call.get():
        call.capture_usage(usage)


class DiagnosticHttpClient(DefaultHttpxClient):
    """Observe SDK attempts including full response reads, without copying requests."""

    def __del__(self) -> None:
        # Keep the SDK's default owned-client cleanup when replacing its wrapper.
        with suppress(Exception):
            self.close()

    def send(self, request: Any, **kwargs: Any) -> Any:
        call = _call.get()
        if call is None:
            return super().send(request, **kwargs)
        call.attempt += 1
        attempt = call.attempt
        started = perf_counter()
        call.emit("model_attempt_started", attempt=attempt)
        status: int | None = None
        error: str | None = None
        exception_type: str | None = None
        cause_type: str | None = None
        try:
            response = super().send(request, **kwargs)
            status = response.status_code
            if status >= 400:
                error = "HTTP_ERROR"
            return response
        except Exception as exc:
            exception_type = type(exc).__name__
            cause_type = type(exc.__cause__).__name__ if exc.__cause__ else None
            error = "TIMEOUT" if "Timeout" in type(exc).__name__ else "CONNECTION_ERROR"
            raise
        finally:
            call.emit(
                "model_attempt_finished",
                attempt=attempt,
                http_status=status,
                status="error" if error else "success",
                error_type=error,
                exception_type=exception_type,
                cause_type=cause_type,
                duration_ms=round((perf_counter() - started) * 1000, 3),
            )
