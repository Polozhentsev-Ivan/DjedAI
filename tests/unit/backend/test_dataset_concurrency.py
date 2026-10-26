"""Тесты очередей обработки датасетов, лимитов загрузки и дисковых квот (#172)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.dataset_service import DatasetJobQueue, DatasetService, StorageQuotaManager
from djedai.backend.db.models import DatasetModel, UserModel
from djedai.backend.db.session import create_db_engine, init_db
from djedai.config import LimitSettings
from djedai.data.service import CsvSource


@pytest.fixture
def db_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test_ds.db'}")
    init_db(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        for u in ("user1", "u_rec", "owner1", "owner2"):
            db.add(UserModel(id=u, username=u, password_hash="dummy"))
        db.commit()
    return factory


def test_storage_quota_manager_reserve_and_release() -> None:
    disk_usage = {"u1": 100}

    def get_usage(owner: str) -> int:
        return disk_usage.get(owner, 0)

    qm = StorageQuotaManager(get_usage, max_owner_bytes=200)

    # Успешный резерв 50 байт (100 + 50 <= 200)
    qm.reserve("u1", 50)
    assert qm.snapshot()["u1"] == 50

    # Попытка резерва еще 60 байт (100 + 50 + 60 = 210 > 200) -> 413
    with pytest.raises(HTTPException) as exc:
        qm.reserve("u1", 60)
    assert exc.value.status_code == 413
    assert "Превышена дисковая квота" in exc.value.detail

    # Освобождение резерва
    qm.release("u1", 50)
    assert qm.snapshot()["u1"] == 0

    # Теперь 60 байт проходят
    qm.reserve("u1", 60)
    assert qm.snapshot()["u1"] == 60


@pytest.mark.anyio
async def test_dataset_job_queue_limits_and_fairness() -> None:
    processed_order: list[str] = []
    job_started = threading.Event()
    job_can_finish = threading.Event()

    def process_func(owner_id: str, dataset_id: str) -> None:
        processed_order.append(f"{owner_id}:{dataset_id}")
        job_started.set()
        job_can_finish.wait()

    queue = DatasetJobQueue(
        process_func,
        max_concurrent=1,
        max_concurrent_per_owner=1,
        max_queued=5,
        max_queued_per_owner=2,
    )

    try:
        # 1. Первая задача запускается сразу
        queue.enqueue("owner1", "ds_1_1")
        await asyncio.to_thread(job_started.wait, 2.0)
        assert queue.snapshot()["active_dataset_jobs_total"] == 1

        # 2. Вторая задача owner1 встает в очередь (1/2)
        queue.enqueue("owner1", "ds_1_2")
        assert queue.snapshot()["queued_dataset_jobs_by_owner"]["owner1"] == 1

        # 3. Третья задача owner1 встает в очередь (2/2)
        queue.enqueue("owner1", "ds_1_3")
        assert queue.snapshot()["queued_dataset_jobs_by_owner"]["owner1"] == 2

        # 4. Четвертая задача owner1 превышает max_queued_per_owner=2 -> HTTP 429
        with pytest.raises(HTTPException) as exc:
            queue.enqueue("owner1", "ds_1_4")
        assert exc.value.status_code == 429

        # 5. Задача от owner2 встает в очередь
        queue.enqueue("owner2", "ds_2_1")
        assert queue.snapshot()["queued_dataset_jobs_by_owner"]["owner2"] == 1

        # Завершаем первую задачу. Round-robin должен запустить owner2, а не owner1!
        job_started.clear()
        job_can_finish.set()
        await asyncio.sleep(0.05)

        # Ожидаем второй запуск
        await asyncio.to_thread(job_started.wait, 2.0)
        # Проверяем, что owner2 запустился строго перед следующей задачей owner1
        assert processed_order[:2] == ["owner1:ds_1_1", "owner2:ds_2_1"]
    finally:
        job_can_finish.set()
        await queue.close()


@pytest.mark.anyio
async def test_dataset_upload_file_and_request_limits(tmp_path: Path, db_factory) -> None:
    limits = LimitSettings(
        _env_file=None,
        max_upload_file_bytes=50,
        max_upload_request_bytes=80,
        max_owner_storage_bytes=1000,
    )
    service = DatasetService(db_factory, tmp_path / "datasets", limits=limits)

    # 1. Файл размером ~120 байт превышает max_upload_file_bytes=50 -> HTTP 413
    oversized_file = tmp_path / "oversized.csv"
    oversized_file.write_text("a,b\n" + "1,2\n" * 30, encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        await service.ingest_csv("user1", [CsvSource(oversized_file, "oversized.csv")])
    assert exc.value.status_code == 413
    assert "Размер файла" in exc.value.detail
    # Каталог датасета должен быть очищен
    assert not any(p.is_dir() for p in (tmp_path / "datasets").glob("*"))

    # 2. Два файла по 24 и 36 байт (< 50 байт), сумма 60 > 55 байт -> HTTP 413
    limits_req = LimitSettings(
        _env_file=None,
        max_upload_file_bytes=50,
        max_upload_request_bytes=55,
        max_owner_storage_bytes=1000,
    )
    service_req = DatasetService(db_factory, tmp_path / "datasets_req", limits=limits_req)
    f1 = tmp_path / "f1.csv"
    f1.write_bytes(b"a,b\n" + b"1,2\n" * 5)  # 24 байта
    f2 = tmp_path / "f2.csv"
    f2.write_bytes(b"a,b\n" + b"1,2\n" * 8)  # 36 байт

    with pytest.raises(HTTPException) as exc2:
        await service_req.ingest_csv("user1", [CsvSource(f1, "f1.csv"), CsvSource(f2, "f2.csv")])
    assert exc2.value.status_code == 413
    assert "Суммарный размер файлов запроса" in exc2.value.detail
    assert not any(p.is_dir() for p in (tmp_path / "datasets_req").glob("*"))

    await service_req.close()
    await service.close()


@pytest.mark.anyio
async def test_dataset_recovery_respects_queue_limits(tmp_path: Path, db_factory) -> None:
    limits = LimitSettings(
        _env_file=None,
        max_concurrent_dataset_jobs=1,
        max_concurrent_dataset_jobs_per_owner=1,
        max_queued_dataset_jobs=2,
        max_queued_dataset_jobs_per_owner=1,
    )
    service = DatasetService(db_factory, tmp_path / "datasets", limits=limits)

    # Создаем 3 незавершенных датасета в БД для одного пользователя (при лимите очереди 1)
    with db_factory() as db:
        for i in range(3):
            db.add(
                DatasetModel(
                    dataset_id=f"ds_rec_{i}",
                    owner_id="u_rec",
                    name=f"Dataset {i}",
                    source_kind="csv",
                    status="processing",
                    duckdb_path=f"ds_rec_{i}/dataset.duckdb",
                )
            )
        db.commit()

    # Запускаем восстановление
    await service.recover_interrupted()

    # Проверяем: первые задачи попали в очередь/обработку, а превысившие лимит помечены как error
    with db_factory() as db:
        overflow_model = db.get(DatasetModel, "ds_rec_2")
        assert overflow_model is not None
        assert overflow_model.status == "error"
        assert "очередь задач переполнена" in overflow_model.error_message

    await service.close()


@pytest.mark.anyio
async def test_partial_copy_failure_releases_reserved_quota(tmp_path: Path, db_factory) -> None:
    # Лимит файла 65536 байт, лимит хранилища 200 КБ
    limits = LimitSettings(
        _env_file=None,
        max_upload_file_bytes=65536,
        max_upload_request_bytes=100000,
        max_owner_storage_bytes=200000,
    )
    service = DatasetService(db_factory, tmp_path / "datasets_partial", limits=limits)

    # Файл размером 65537 байт (65536 байт запишутся в 1-м чанке, на 2-м чанке 1 байт упадет с 413)
    oversized_file = tmp_path / "oversized.csv"
    oversized_file.write_bytes(b"x" * 65537)

    # Загрузка должна упасть с 413
    with pytest.raises(HTTPException) as exc:
        await service.ingest_csv("user1", [CsvSource(oversized_file, "oversized.csv")])
    assert exc.value.status_code == 413

    # Резерв должен быть полностью очищен (0 байт), а не зависнуть на 65536
    assert service.quota_manager.snapshot().get("user1", 0) == 0

    # Повторные попытки не должны исчерпать квоту
    for _ in range(5):
        with pytest.raises(HTTPException) as exc_repeat:
            await service.ingest_csv("user1", [CsvSource(oversized_file, "oversized.csv")])
        assert exc_repeat.value.status_code == 413
        assert service.quota_manager.snapshot().get("user1", 0) == 0

    await service.close()


@pytest.mark.anyio
async def test_dataset_queue_overflow_rolls_back_storage_and_records(
    tmp_path: Path, db_factory
) -> None:
    """При переполнении очереди датасет не оставляет файлов и записей (#172 / PR #199)."""
    limits = LimitSettings(
        _env_file=None,
        max_concurrent_dataset_jobs=1,
        max_concurrent_dataset_jobs_per_owner=1,
        max_queued_dataset_jobs=1,
        max_queued_dataset_jobs_per_owner=1,
        max_upload_file_bytes=10000,
        max_upload_request_bytes=20000,
        max_owner_storage_bytes=100000,
    )
    data_dir = tmp_path / "datasets_rollback"
    service = DatasetService(
        db_factory,
        data_dir,
        source_encryption_key=Fernet.generate_key().decode("ascii"),
        limits=limits,
    )

    job1_started = threading.Event()
    job1_can_proceed = threading.Event()

    def blocking_process(owner_id: str, dataset_id: str) -> None:
        job1_started.set()
        job1_can_proceed.wait()

    service._job_queue.process_func = blocking_process

    f = tmp_path / "sample.csv"
    f.write_text("col1,col2\n1,2\n3,4\n", encoding="utf-8")

    try:
        # 1. Первая задача запускается и становится активной
        res1 = await service.ingest_csv("owner1", [CsvSource(f, "sample.csv")])
        await asyncio.to_thread(job1_started.wait, 2.0)
        assert service._job_queue.snapshot()["active_dataset_jobs_total"] == 1

        # 2. Вторая задача встает в очередь (достигнут лимит очереди per-owner=1)
        res2 = await service.ingest_csv("owner1", [CsvSource(f, "sample.csv")])
        assert service._job_queue.snapshot()["queued_dataset_jobs_total"] == 1

        # 3. Третья попытка загрузки CSV должна отклониться с 429
        with pytest.raises(HTTPException) as exc_csv:
            await service.ingest_csv("owner1", [CsvSource(f, "sample.csv")])
        assert exc_csv.value.status_code == 429
        assert "Превышен лимит очереди" in exc_csv.value.detail

        # 4. Попытка подключения БД также должна отклониться с 429 без создания каталогов
        with pytest.raises(HTTPException) as exc_db:
            await service.connect_db(
                "owner1", "postgresql://user:pass@localhost:5432/db", kind="postgresql"
            )
        assert exc_db.value.status_code == 429

        # 5. Проверяем состояние БД: должны остаться ТОЛЬКО 2 датасета (res1 и res2)
        with db_factory() as db:
            models = db.scalars(select(DatasetModel).where(DatasetModel.owner_id == "owner1")).all()
            ids = {m.dataset_id for m in models}
            assert ids == {res1.dataset_id, res2.dataset_id}

        # 6. Проверяем файловую систему: на диске ТОЛЬКО каталоги res1 и res2
        existing_dirs = {p.name for p in data_dir.iterdir() if p.is_dir()}
        assert existing_dirs == {res1.dataset_id, res2.dataset_id}

        # 7. Проверяем счетчики резервирования: очереди и диска
        assert service._job_queue.snapshot()["reserved_dataset_jobs_total"] == 0
        assert service.quota_manager.snapshot().get("owner1", 0) == 0

        # 8. Проверяем прямой откат через _start: если запись и файлы уже созданы,
        # но enqueue падает с 429, они должны быть удалены
        test_orphan_id = "test_orphan_dataset"
        orphan_dir = data_dir / test_orphan_id
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "some_file.csv").write_text("data", encoding="utf-8")
        with db_factory() as db:
            db.add(
                DatasetModel(
                    dataset_id=test_orphan_id,
                    owner_id="owner1",
                    name="Orphan",
                    source_kind="csv",
                    status="processing",
                    duckdb_path=f"{test_orphan_id}/dataset.duckdb",
                )
            )
            db.commit()

        # Вызов _start должен выбросить 429 и автоматически откатить каталог и запись в БД
        with pytest.raises(HTTPException) as exc_start:
            service._start("owner1", test_orphan_id)
        assert exc_start.value.status_code == 429
        assert not orphan_dir.exists()
        with db_factory() as db:
            assert db.get(DatasetModel, test_orphan_id) is None
    finally:
        job1_can_proceed.set()
        await service.close()
