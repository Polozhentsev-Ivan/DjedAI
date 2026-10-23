"""One monotonic budget shared by a chat's graph, model attempts and tools."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic


class TurnTimeout(TimeoutError):
    """The complete turn exhausted its budget, rather than one network attempt."""


class TurnCancelled(Exception):
    """The caller revoked the turn; no subsequent work may be started."""


class ExecutionBudget:
    def __init__(self, seconds: float, is_cancelled: Callable[[], bool] | None = None) -> None:
        self.deadline = monotonic() + seconds
        self.is_cancelled = is_cancelled or (lambda: False)

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - monotonic())

    def check(self) -> None:
        if self.remaining <= 0:
            raise TurnTimeout()
        if self.is_cancelled():
            raise TurnCancelled()

    def wait(self, seconds: float) -> None:
        until = monotonic() + seconds
        while True:
            self.check()
            remaining = until - monotonic()
            if remaining <= 0:
                return
            threading.Event().wait(min(0.05, remaining, self.remaining))

    @contextmanager
    def activate(self) -> Iterator[None]:
        self.check()
        token = _budget.set(self)
        try:
            yield
            self.check()
        finally:
            _budget.reset(token)


_budget: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)


def current_budget() -> ExecutionBudget | None:
    return _budget.get()


class BudgetTimer:
    """Interrupt a query on tool timeout, turn timeout or cancellation."""

    def __init__(
        self, seconds: float, callback: Callable[[], None], budget: ExecutionBudget
    ) -> None:
        self.deadline = min(monotonic() + seconds, budget.deadline)
        self.callback = callback
        self.budget = budget
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="query-budget", daemon=True)

    def _watch(self) -> None:
        while not self.stopped.is_set():
            remaining = self.deadline - monotonic()
            if remaining <= 0 or self.budget.is_cancelled():
                if not self.stopped.is_set():
                    self.callback()
                return
            self.stopped.wait(min(0.05, remaining))

    def start(self) -> None:
        self.budget.check()
        self.thread.start()

    def cancel(self) -> None:
        self.stopped.set()
        if self.thread.ident is not None:
            # No delayed interrupt may reach the next query on the same connection.
            self.thread.join()


def query_timer(seconds: float, callback: Callable[[], None]) -> threading.Timer | BudgetTimer:
    budget = current_budget()
    return BudgetTimer(seconds, callback, budget) if budget else threading.Timer(seconds, callback)
