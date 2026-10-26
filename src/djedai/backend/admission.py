"""Управление допуском (Admission Control) и параллелизмом вычислений."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic, perf_counter
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class StreamingUploadLimitMiddleware:
    """Защита от DoS при загрузке: отсекает тела запросов > MAX_UPLOAD_REQUEST_BYTES

    до того, как python-multipart начнет сбрасывать временные файлы в /tmp.
    """

    def __init__(self, app: Any, max_bytes: int, path_prefix: str = "/api/datasets/upload") -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefix = path_prefix

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or not scope.get("path", "").startswith(self.path_prefix)
            or scope.get("method") != "POST"
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        # 64 KiB запас на multipart-заголовки и границы частей
        allowed_max = self.max_bytes + 65536
        if content_length is not None:
            try:
                cl = int(content_length)
                if cl > allowed_max:
                    response = JSONResponse(
                        {
                            "error": {
                                "code": "payload_too_large",
                                "message": "Превышен максимальный размер тела запроса загрузки",
                            }
                        },
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        # Для chunked запросов без Content-Length считаем фактические потоковые байты
        received_bytes = 0

        async def limited_receive() -> Any:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > allowed_max:
                    raise HTTPException(
                        status_code=413,
                        detail="Превышен максимальный размер загружаемого запроса",
                    )
            return message

        try:
            await self.app(scope, limited_receive, send)
        except HTTPException as exc:
            if exc.status_code == 413:
                response = JSONResponse(
                    {"error": {"code": "payload_too_large", "message": str(exc.detail)}},
                    status_code=413,
                )
                await response(scope, receive, send)
            else:
                raise


@dataclass
class ChatWaiter:
    owner_id: str
    chat_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    enqueued_at: float = field(default_factory=monotonic)
    cancelled: bool = False


class Permit:
    """Слот допуска к вычислениям агента.

    Удерживается на протяжении всего фактического времени работы рабочего потока ОС.
    Освобождается в блоке finally потока вычислений, гарантируя, что слот CPU
    не освободится раньше, чем поток физически завершит работу.
    """

    def __init__(
        self,
        controller: ChatAdmissionController,
        owner_id: str,
        chat_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.controller = controller
        self.owner_id = owner_id
        self.chat_id = chat_id
        self.loop = loop
        self.acquired_at = perf_counter()
        self._released = threading.Event()

    @property
    def is_released(self) -> bool:
        return self._released.is_set()

    def release(self) -> None:
        """Потокобезопасное освобождение слота из любого потока (включая worker)."""
        if self._released.is_set():
            return
        self._released.set()
        duration_ms = int((perf_counter() - self.acquired_at) * 1000)
        try:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(
                    self.controller._on_permit_released, self.owner_id, self.chat_id, duration_ms
                )
        except RuntimeError:
            # Event loop может быть уже остановлен при завершении сервиса
            pass


class ChatAdmissionController:
    """Атомарный контроллер допуска и очередей для чат-вычислений агента (§7.4 / #172).

    Обеспечивает:
    - Защиту от одновременных ходов в одном и том же чате (HTTP 409).
    - Соблюдение глобальных и per-owner лимитов активных воркеров.
    - Ограниченную очередь ожидания слота с дедлайном и отменой при disconnect.
    - Справедливое распределение (Fair Queueing) между пользователями.
    - Удержание слота CPU до физического завершения потока.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 4,
        max_concurrent_per_owner: int = 1,
        max_queued: int = 10,
        max_queued_per_owner: int = 2,
        timeout_seconds: float = 10.0,
        diagnostics_logger: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_concurrent_per_owner = max_concurrent_per_owner
        self.max_queued = max_queued
        self.max_queued_per_owner = max_queued_per_owner
        self.timeout_seconds = timeout_seconds
        self.diagnostics_logger = diagnostics_logger

        self._lock = asyncio.Lock()
        self._active_chat_ids: set[str] = set()
        self._active_by_owner: dict[str, int] = defaultdict(int)
        self._active_total: int = 0

        # Очереди ожидания по пользователям для справедливой round-robin диспетчеризации
        self._waiters_by_owner: dict[str, deque[ChatWaiter]] = defaultdict(deque)
        self._queued_by_owner: dict[str, int] = defaultdict(int)
        self._queued_total: int = 0
        self._owner_queue_order: deque[str] = deque()

    async def acquire_permit(
        self,
        owner_id: str,
        chat_id: str,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> Permit:
        """Атомарно запрашивает допуск к вычислениям или ожидает слот в очереди."""
        loop = asyncio.get_running_loop()
        waiter: ChatWaiter | None = None

        async with self._lock:
            # 1. Запрет одновременных вычислений в одном и том же чате
            if chat_id in self._active_chat_ids:
                self._log_event(
                    "admission_rejected",
                    owner_id=owner_id,
                    chat_id=chat_id,
                    reason="same_chat_conflict",
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "В этом чате уже выполняется генерация ответа. "
                        "Дождитесь завершения или отмените текущий запрос."
                    ),
                )

            # 2. Проверка немедленной доступности слота
            if (
                self._active_total < self.max_concurrent
                and self._active_by_owner[owner_id] < self.max_concurrent_per_owner
                and self._queued_total == 0
            ):
                self._active_total += 1
                self._active_by_owner[owner_id] += 1
                self._active_chat_ids.add(chat_id)
                self._log_event(
                    "admission_acquired",
                    owner_id=owner_id,
                    chat_id=chat_id,
                    wait_ms=0,
                )
                return Permit(self, owner_id, chat_id, loop)

            # 3. Проверка лимитов очередей перед постановкой в очередь
            if self._queued_by_owner[owner_id] >= self.max_queued_per_owner:
                self._log_event(
                    "admission_rejected",
                    owner_id=owner_id,
                    chat_id=chat_id,
                    reason="owner_queue_full",
                )
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "Превышен лимит параллельных запросов пользователя. "
                        "Дождитесь ответа на предыдущие вопросы."
                    ),
                )

            if self._queued_total >= self.max_queued:
                self._log_event(
                    "admission_rejected",
                    owner_id=owner_id,
                    chat_id=chat_id,
                    reason="global_queue_full",
                )
                raise HTTPException(
                    status_code=429,
                    detail="Очередь вычислений сервера перегружена. Повторите попытку позже.",
                )

            # 4. Постановка в очередь ожидания
            waiter = ChatWaiter(owner_id=owner_id, chat_id=chat_id)
            self._waiters_by_owner[owner_id].append(waiter)
            self._queued_by_owner[owner_id] += 1
            self._queued_total += 1
            if owner_id not in self._owner_queue_order:
                self._owner_queue_order.append(owner_id)

            self._log_event(
                "admission_enqueued",
                owner_id=owner_id,
                chat_id=chat_id,
                queue_position=self._queued_total,
            )

        # 5. Ожидание допуска вне блокировки с проверкой дедлайна и дисконнекта
        start_wait = monotonic()
        deadline = start_wait + self.timeout_seconds

        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    async with self._lock:
                        self._remove_waiter(waiter)
                    self._log_event(
                        "admission_rejected",
                        owner_id=owner_id,
                        chat_id=chat_id,
                        reason="timeout",
                    )
                    raise HTTPException(
                        status_code=429,
                        detail="Превышено время ожидания допуска к вычислениям. Сервер перегружен.",
                    )

                if is_disconnected and await is_disconnected():
                    async with self._lock:
                        self._remove_waiter(waiter)
                    self._log_event(
                        "admission_cancelled",
                        owner_id=owner_id,
                        chat_id=chat_id,
                        reason="client_disconnect",
                    )
                    raise asyncio.CancelledError("Client disconnected during admission wait")

                try:
                    # Ожидаем события слота порциями по 0.1 с для регулярной проверки дисконнекта
                    await asyncio.wait_for(waiter.event.wait(), timeout=min(0.1, remaining))
                    if waiter.event.is_set():
                        wait_ms = int((monotonic() - start_wait) * 1000)
                        self._log_event(
                            "admission_acquired",
                            owner_id=owner_id,
                            chat_id=chat_id,
                            wait_ms=wait_ms,
                        )
                        return Permit(self, owner_id, chat_id, loop)
                except TimeoutError:
                    continue
        except (asyncio.CancelledError, HTTPException):
            async with self._lock:
                self._remove_waiter(waiter)
            raise

    def _remove_waiter(self, waiter: ChatWaiter) -> None:
        """Удаляет заявку из очереди (вызывать под self._lock)."""
        if waiter.cancelled:
            return
        waiter.cancelled = True
        owner_queue = self._waiters_by_owner.get(waiter.owner_id)
        if owner_queue and waiter in owner_queue:
            owner_queue.remove(waiter)
            self._queued_by_owner[waiter.owner_id] = max(
                0, self._queued_by_owner[waiter.owner_id] - 1
            )
            self._queued_total = max(0, self._queued_total - 1)
            if not owner_queue and waiter.owner_id in self._owner_queue_order:
                self._owner_queue_order.remove(waiter.owner_id)

    def _on_permit_released(self, owner_id: str, chat_id: str, duration_ms: int) -> None:
        """Обратный вызов освобождения слота (выполняется в цикле событий FastAPI)."""
        self._active_chat_ids.discard(chat_id)
        self._active_by_owner[owner_id] = max(0, self._active_by_owner[owner_id] - 1)
        self._active_total = max(0, self._active_total - 1)

        # Справедливость: владелец, только что завершивший вычисление, уступает очередь другим
        if (
            self._owner_queue_order
            and self._owner_queue_order[0] == owner_id
            and len(self._owner_queue_order) > 1
        ):
            self._owner_queue_order.rotate(-1)

        self._log_event(
            "admission_released",
            owner_id=owner_id,
            chat_id=chat_id,
            duration_ms=duration_ms,
        )

        # Проверяем, можно ли выдать слот следующему ожидающему по Fair Round-Robin
        self._dispatch_next()

    def _dispatch_next(self) -> None:
        """Справедливо активирует следующую заявку в очереди (Round-Robin между владельцами)."""
        if self._active_total >= self.max_concurrent or self._queued_total == 0:
            return

        # Ищем следующего владельца в round-robin порядке, у которого не исчерпан лимит
        attempts = len(self._owner_queue_order)
        for _ in range(attempts):
            if not self._owner_queue_order:
                break
            candidate_owner = self._owner_queue_order[0]
            # Перемещаем в конец для соблюдения справедливости
            self._owner_queue_order.rotate(-1)

            if self._active_by_owner[candidate_owner] < self.max_concurrent_per_owner:
                owner_waiters = self._waiters_by_owner.get(candidate_owner)
                while owner_waiters:
                    next_waiter = owner_waiters.popleft()
                    self._queued_by_owner[candidate_owner] = max(
                        0, self._queued_by_owner[candidate_owner] - 1
                    )
                    self._queued_total = max(0, self._queued_total - 1)
                    if not owner_waiters and candidate_owner in self._owner_queue_order:
                        self._owner_queue_order.remove(candidate_owner)

                    if next_waiter.cancelled:
                        continue

                    # Проверяем chat_id кандидата
                    if next_waiter.chat_id in self._active_chat_ids:
                        # В чате уже кто-то активен, отменяем этого официанта с 409
                        continue

                    # Выдаем слот
                    self._active_total += 1
                    self._active_by_owner[candidate_owner] += 1
                    self._active_chat_ids.add(next_waiter.chat_id)
                    next_waiter.event.set()
                    return

    def _log_event(self, event_type: str, **kwargs: Any) -> None:
        if self.diagnostics_logger is not None:
            try:
                self.diagnostics_logger({"type": event_type, **kwargs})
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        """Возвращает безопасный снимок очередей и активных слотов без пользовательских данных."""
        return {
            "active_chats_total": self._active_total,
            "active_chats_by_owner": dict(self._active_by_owner),
            "queued_chats_total": self._queued_total,
            "queued_chats_by_owner": dict(self._queued_by_owner),
            "active_chat_ids_count": len(self._active_chat_ids),
        }
