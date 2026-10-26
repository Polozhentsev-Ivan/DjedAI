"""Тесты контроллера допуска вычислений и очередей (ChatAdmissionController)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from djedai.backend.admission import ChatAdmissionController


@pytest.mark.anyio
async def test_immediate_permit_acquisition_and_release() -> None:
    controller = ChatAdmissionController(max_concurrent=2, max_concurrent_per_owner=1)
    permit = await controller.acquire_permit("owner1", "c_1")
    assert permit is not None
    assert not permit.is_released

    snapshot = controller.snapshot()
    assert snapshot["active_chats_total"] == 1
    assert snapshot["active_chats_by_owner"]["owner1"] == 1
    assert snapshot["active_chat_ids_count"] == 1

    permit.release()
    # Permit release schedules callback on event loop
    await asyncio.sleep(0.01)

    snapshot_after = controller.snapshot()
    assert snapshot_after["active_chats_total"] == 0
    assert snapshot_after["active_chats_by_owner"]["owner1"] == 0
    assert snapshot_after["active_chat_ids_count"] == 0


@pytest.mark.anyio
async def test_same_chat_conflict_raises_409() -> None:
    controller = ChatAdmissionController(max_concurrent=4, max_concurrent_per_owner=2)
    permit1 = await controller.acquire_permit("owner1", "c_same")
    try:
        with pytest.raises(HTTPException) as exc_info:
            await controller.acquire_permit("owner1", "c_same")
        assert exc_info.value.status_code == 409
        assert "уже выполняется генерация" in exc_info.value.detail
    finally:
        permit1.release()
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_owner_queue_limit_rejection() -> None:
    controller = ChatAdmissionController(
        max_concurrent=4,
        max_concurrent_per_owner=1,
        max_queued=10,
        max_queued_per_owner=1,
        timeout_seconds=0.5,
    )
    permit1 = await controller.acquire_permit("owner1", "c_1")
    try:
        # Второй запрос попадает в очередь
        task2 = asyncio.create_task(controller.acquire_permit("owner1", "c_2"))
        await asyncio.sleep(0.02)
        assert controller.snapshot()["queued_chats_by_owner"]["owner1"] == 1

        # Третий запрос превышает max_queued_per_owner=1 -> HTTP 429
        with pytest.raises(HTTPException) as exc_info:
            await controller.acquire_permit("owner1", "c_3")
        assert exc_info.value.status_code == 429
        assert "лимит параллельных запросов" in exc_info.value.detail

        task2.cancel()
        try:
            await task2
        except (asyncio.CancelledError, HTTPException):
            pass
    finally:
        permit1.release()
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_admission_timeout_rejection() -> None:
    controller = ChatAdmissionController(
        max_concurrent=1,
        max_concurrent_per_owner=1,
        max_queued=5,
        max_queued_per_owner=2,
        timeout_seconds=0.05,
    )
    permit1 = await controller.acquire_permit("owner1", "c_1")
    try:
        with pytest.raises(HTTPException) as exc_info:
            await controller.acquire_permit("owner2", "c_2")
        assert exc_info.value.status_code == 429
        assert "время ожидания" in exc_info.value.detail
    finally:
        permit1.release()
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_client_disconnect_during_wait_cleans_queue() -> None:
    controller = ChatAdmissionController(
        max_concurrent=1,
        max_concurrent_per_owner=1,
        max_queued=5,
        max_queued_per_owner=2,
        timeout_seconds=1.0,
    )
    permit1 = await controller.acquire_permit("owner1", "c_1")
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    try:
        wait_task = asyncio.create_task(
            controller.acquire_permit("owner2", "c_2", is_disconnected=is_disconnected)
        )
        await asyncio.sleep(0.02)
        assert controller.snapshot()["queued_chats_total"] == 1

        # Клиент оборвал связь
        disconnected = True
        with pytest.raises(asyncio.CancelledError):
            await wait_task

        # Очередь должна быть очищена
        assert controller.snapshot()["queued_chats_total"] == 0
        assert controller.snapshot()["queued_chats_by_owner"]["owner2"] == 0
    finally:
        permit1.release()
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_fair_round_robin_queue_dispatch() -> None:
    controller = ChatAdmissionController(
        max_concurrent=1,
        max_concurrent_per_owner=1,
        max_queued=10,
        max_queued_per_owner=5,
        timeout_seconds=2.0,
    )
    permit1 = await controller.acquire_permit("owner1", "c_1")

    # owner1 ставит еще одну задачу, затем owner2 ставит задачу
    task_owner1 = asyncio.create_task(controller.acquire_permit("owner1", "c_1_2"))
    await asyncio.sleep(0.01)
    task_owner2 = asyncio.create_task(controller.acquire_permit("owner2", "c_2_1"))
    await asyncio.sleep(0.01)

    assert controller.snapshot()["queued_chats_total"] == 2

    # Освобождаем слот owner1. Round-Robin должен отдать слот owner2, а не второй задаче owner1!
    permit1.release()
    await asyncio.sleep(0.05)

    assert task_owner2.done()
    permit_owner2 = task_owner2.result()
    assert permit_owner2.owner_id == "owner2"

    assert not task_owner1.done()

    permit_owner2.release()
    await asyncio.sleep(0.05)

    assert task_owner1.done()
    permit_owner1 = task_owner1.result()
    assert permit_owner1.owner_id == "owner1"
    permit_owner1.release()
    await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_permit_release_is_idempotent() -> None:
    controller = ChatAdmissionController(max_concurrent=2, max_concurrent_per_owner=2)
    permit = await controller.acquire_permit("owner1", "c_1")
    assert controller.snapshot()["active_chats_total"] == 1

    permit.release()
    permit.release()
    permit.release()
    await asyncio.sleep(0.02)

    snapshot = controller.snapshot()
    assert snapshot["active_chats_total"] == 0
    assert snapshot["active_chats_by_owner"]["owner1"] == 0


@pytest.mark.anyio
async def test_cancellation_after_dispatch_does_not_leak_slot() -> None:
    controller = ChatAdmissionController(
        max_concurrent=1,
        max_concurrent_per_owner=1,
        max_queued=5,
        max_queued_per_owner=2,
        timeout_seconds=2.0,
    )
    permit1 = await controller.acquire_permit("owner1", "c_1")

    # owner2 встает в очередь
    task2 = asyncio.create_task(controller.acquire_permit("owner2", "c_2"))
    await asyncio.sleep(0.02)
    assert controller.snapshot()["queued_chats_total"] == 1

    # owner3 встает в очередь
    task3 = asyncio.create_task(controller.acquire_permit("owner3", "c_3"))
    await asyncio.sleep(0.02)
    assert controller.snapshot()["queued_chats_total"] == 2

    # Освобождаем слот permit1 - диспетчер назначит слот task2 (owner2)
    permit1.release()
    # Но task2 отменяется сразу же
    task2.cancel()
    await asyncio.sleep(0.05)

    with pytest.raises(asyncio.CancelledError):
        await task2

    # Слот не должен утечь, а должен перейти к task3 (owner3)!
    assert task3.done()
    permit3 = task3.result()
    assert permit3.owner_id == "owner3"
    assert controller.snapshot()["active_chats_total"] == 1

    permit3.release()
    await asyncio.sleep(0.02)
    assert controller.snapshot()["active_chats_total"] == 0


@pytest.mark.anyio
async def test_max_concurrent_chats_strictly_enforced() -> None:
    controller = ChatAdmissionController(
        max_concurrent=4,
        max_concurrent_per_owner=2,
        max_queued=10,
        max_queued_per_owner=2,
        timeout_seconds=0.1,
    )
    permits = []
    # Занимаем все 4 слота: 2 для owner1, 2 для owner2
    permits.append(await controller.acquire_permit("owner1", "c_1"))
    permits.append(await controller.acquire_permit("owner1", "c_2"))
    permits.append(await controller.acquire_permit("owner2", "c_3"))
    permits.append(await controller.acquire_permit("owner2", "c_4"))

    assert controller.snapshot()["active_chats_total"] == 4

    # 5-й запрос от owner3 не может получить слот немедленно и встает в очередь
    task5 = asyncio.create_task(controller.acquire_permit("owner3", "c_5"))
    await asyncio.sleep(0.02)
    assert not task5.done()
    assert controller.snapshot()["queued_chats_total"] == 1
    assert controller.snapshot()["active_chats_total"] == 4

    # Освобождаем один слот
    permits[0].release()
    await asyncio.sleep(0.05)

    # Теперь task5 получил слот, а общее число активных по-прежнему ровно 4
    assert task5.done()
    permit5 = task5.result()
    permits.append(permit5)
    assert controller.snapshot()["active_chats_total"] == 4

    for p in permits:
        p.release()
    await asyncio.sleep(0.02)
    assert controller.snapshot()["active_chats_total"] == 0
