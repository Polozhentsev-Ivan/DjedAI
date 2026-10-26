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


@pytest.mark.anyio
async def test_active_owner_in_queue_does_not_block_eligible_owner() -> None:
    # Глобальный лимит 4, лимит на владельца 1
    controller = ChatAdmissionController(
        max_concurrent=4,
        max_concurrent_per_owner=1,
        max_queued=5,
        max_queued_per_owner=2,
        timeout_seconds=1.0,
    )

    # 1. Запускаем A1
    permit_a1 = await controller.acquire_permit("ownerA", "chat_a1")
    assert controller.snapshot()["active_chats_total"] == 1
    assert controller.snapshot()["active_chats_by_owner"]["ownerA"] == 1

    # 2. Ставим A2 в очередь (ownerA исчерпал лимит 1)
    task_a2 = asyncio.create_task(controller.acquire_permit("ownerA", "chat_a2"))
    await asyncio.sleep(0.02)
    assert controller.snapshot()["queued_chats_total"] == 1
    assert not task_a2.done()

    # 3. Приходит B1: свободны 3 слота, лимит ownerB (0/1) не исчерпан.
    # B1 должен получить слот немедленно, не блокируясь очередью ownerA!
    permit_b1 = await controller.acquire_permit("ownerB", "chat_b1")
    assert permit_b1 is not None
    assert permit_b1.owner_id == "ownerB"

    snapshot = controller.snapshot()
    assert snapshot["active_chats_total"] == 2
    assert snapshot["active_chats_by_owner"]["ownerA"] == 1
    assert snapshot["active_chats_by_owner"]["ownerB"] == 1
    assert snapshot["queued_chats_total"] == 1
    assert not task_a2.done()

    # 4. Освобождаем B1 и A1
    permit_b1.release()
    await asyncio.sleep(0.02)
    assert controller.snapshot()["active_chats_by_owner"]["ownerB"] == 0

    permit_a1.release()
    await asyncio.sleep(0.05)

    # Теперь task_a2 должен получить слот
    assert task_a2.done()
    permit_a2 = task_a2.result()
    assert permit_a2.owner_id == "ownerA"

    permit_a2.release()
    await asyncio.sleep(0.02)
    assert controller.snapshot()["active_chats_total"] == 0


@pytest.mark.anyio
async def test_concurrent_admission_stress_no_leaks() -> None:
    """Стресс-тест конкурентных запросов: проверка инвариантов и отсутствия утечек слотов."""
    controller = ChatAdmissionController(
        max_concurrent=4,
        max_concurrent_per_owner=2,
        max_queued=20,
        max_queued_per_owner=5,
        timeout_seconds=0.5,
    )

    max_observed_total = 0
    max_observed_per_owner: dict[str, int] = {}
    completed = 0
    rejected = 0

    async def worker_sim(owner_id: str, chat_id: str) -> None:
        nonlocal max_observed_total, completed, rejected
        try:
            permit = await controller.acquire_permit(owner_id, chat_id)
            try:
                # Фиксируем инварианты
                snap = controller.snapshot()
                total = snap["active_chats_total"]
                owner_cnt = snap["active_chats_by_owner"].get(owner_id, 0)
                if total > max_observed_total:
                    max_observed_total = total
                if owner_cnt > max_observed_per_owner.get(owner_id, 0):
                    max_observed_per_owner[owner_id] = owner_cnt

                assert total <= 4, f"Превышен общий лимит: {total}"
                assert owner_cnt <= 2, f"Превышен лимит владельца: {owner_cnt}"

                # Имитируем работу
                await asyncio.sleep(0.02)
                completed += 1
            finally:
                permit.release()
        except HTTPException as exc:
            assert exc.status_code in (409, 429)
            rejected += 1

    # Запускаем 30 параллельных задач от 6 владельцев
    tasks = []
    for i in range(30):
        owner = f"user_{i % 6}"
        chat = f"chat_{i}"
        tasks.append(asyncio.create_task(worker_sim(owner, chat)))

    await asyncio.gather(*tasks)
    await asyncio.sleep(0.05)

    # Все задачи либо завершились, либо получили корректный 429/409
    assert completed + rejected == 30
    assert completed > 0

    # Проверяем, что нет утечек: все слоты и очереди вернулись в 0
    final_snap = controller.snapshot()
    assert final_snap["active_chats_total"] == 0
    assert final_snap["queued_chats_total"] == 0
    assert all(cnt == 0 for cnt in final_snap["active_chats_by_owner"].values())
