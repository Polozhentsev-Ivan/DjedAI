"""Тесты реестра результатов и Data API (§7.1, B5-5 / #96).

Проверяют:
- Раздельный доступ к метаданным (ToolResult без строк) и табличным данным (ToolResultData);
- Восстановление происхождения расчёта: tool, params, sql, source_result_id, facts, chart_hint;
- Чтение небольших результатов (in-memory preview) и больших результатов через Parquet;
- Обработку отсутствующих результатов (404 not_found);
- Контроль доступа: изоляция чужих результатов и без владельца (404, no data leakage);
- Обработку результатов со статусами error и empty;
- Защиту от отключенного локального режима (503).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.app import create_app
from djedai.backend.config import Settings, get_settings
from djedai.backend.db.models import ToolResultModel, UserModel
from djedai.backend.db.repositories import save_tool_result
from djedai.backend.db.session import create_db_engine, init_db
from djedai.contracts.common import ErrorResponse
from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultColumn,
    ToolResultData,
)


@pytest.fixture
def results_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, sessionmaker[Session], Settings]]:
    db_path = tmp_path / "app.db"
    results_dir = tmp_path / "results_storage"
    results_dir.mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{db_path}"
    engine = create_db_engine(db_url)
    init_db(engine)
    factory = sessionmaker(bind=engine)

    # Создаём тестовых пользователей: alice (текущий в сессии) и bob (чужой)
    with factory() as db:
        alice = UserModel(id="u_alice", username="alice", is_active=True)
        bob = UserModel(id="u_bob", username="bob", is_active=True)
        db.add_all([alice, bob])
        db.commit()

    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("RESULTS_DATA_DIR", str(results_dir))
    monkeypatch.setenv("CP1_LOCAL_MODE", "true")
    monkeypatch.setenv("CP1_LOCAL_USER_ID", "u_alice")
    get_settings.cache_clear()

    settings = Settings(
        _env_file=None,
        app_database_url=db_url,
        results_data_dir=str(results_dir),
        cp1_local_mode=True,
        cp1_local_user_id="u_alice",
    )

    app = create_app(settings)
    try:
        with TestClient(app) as client:
            yield client, factory, settings
    finally:
        get_settings.cache_clear()
        engine.dispose()


def test_get_small_result_metadata_and_data(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Небольшой результат отдаёт метаданные без data и полные строки через /data."""
    client, factory, settings = results_env

    tool_res = ToolResult(
        result_id="res_small_1",
        status="ok",
        data=ToolResultData(
            columns=[
                ToolResultColumn(name="category", type="string", label="Категория"),
                ToolResultColumn(name="revenue", type="money", label="Выручка"),
            ],
            rows=[["Electronics", 125000.5], ["Books", 45000.0]],
            row_count=2,
            truncated=False,
        ),
        tool="run_sql",
        params={"sql": "SELECT category, revenue FROM sales"},
        sql="SELECT category, revenue FROM sales",
        duration_ms=15,
    )

    with factory() as db:
        save_tool_result(
            db,
            tool_res,
            owner_id="u_alice",
            persist_data=False,  # small result, only data_preview
            storage_dir=settings.results_data_dir,
        )

    # 1. Проверка метаданных: GET /api/results/{id}
    res_meta = client.get("/api/results/res_small_1")
    assert res_meta.status_code == 200
    meta_json = res_meta.json()

    assert meta_json["result_id"] == "res_small_1"
    assert meta_json["status"] == "ok"
    assert meta_json["tool"] == "run_sql"
    assert meta_json["sql"] == "SELECT category, revenue FROM sales"
    assert meta_json["params"] == {"sql": "SELECT category, revenue FROM sales"}
    assert meta_json["duration_ms"] == 15
    # Ключевое требование: метаданные отдаются раздельно от данных
    assert meta_json["data"] is None

    # 2. Проверка данных: GET /api/results/{id}/data
    res_data = client.get("/api/results/res_small_1/data")
    assert res_data.status_code == 200
    data_json = res_data.json()

    assert data_json["row_count"] == 2
    assert data_json["truncated"] is False
    assert len(data_json["rows"]) == 2
    assert data_json["rows"] == [["Electronics", 125000.5], ["Books", 45000.0]]
    assert len(data_json["columns"]) == 2
    assert data_json["columns"][0]["name"] == "category"
    assert data_json["columns"][1]["name"] == "revenue"


def test_get_large_result_metadata_and_data_via_parquet(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Большой результат сохраняется в Parquet, отдаёт data_ref и читается через /data."""
    client, factory, settings = results_env

    # Генерируем 10 000 строк
    n_rows = 10_000
    rows = [[i, f"item_{i}", i * 1.5] for i in range(n_rows)]
    columns = ["id", "name", "price"]

    tool_res = ToolResult(
        result_id="res_large_1",
        status="ok",
        data=ToolResultData(columns=columns, rows=rows, row_count=n_rows),
        tool="run_sql",
        params={"sql": "SELECT id, name, price FROM items"},
        sql="SELECT id, name, price FROM items",
        duration_ms=120,
    )

    with factory() as db:
        save_tool_result(
            db,
            tool_res,
            owner_id="u_alice",
            persist_data=True,
            storage_dir=settings.results_data_dir,
        )

    # Проверяем, что Parquet-файл действительно создан
    parquet_file = Path(settings.results_data_dir) / "res_large_1.parquet"
    assert parquet_file.is_file()

    # 1. Запрос метаданных не загружает 10_000 строк в память и возвращает data_ref
    res_meta = client.get("/api/results/res_large_1")
    assert res_meta.status_code == 200
    meta_json = res_meta.json()

    assert meta_json["result_id"] == "res_large_1"
    assert meta_json["data"] is None
    assert meta_json["data_ref"] == "parquet://res_large_1.parquet"
    assert meta_json["sql"] == "SELECT id, name, price FROM items"

    # 2. Запрос данных читает Parquet через DuckDB и отдаёт все строки
    res_data = client.get("/api/results/res_large_1/data")
    assert res_data.status_code == 200
    data_json = res_data.json()

    assert data_json["row_count"] == n_rows
    assert len(data_json["rows"]) == n_rows
    assert data_json["columns"] == columns
    assert data_json["rows"][0] == [0, "item_0", 0.0]
    assert data_json["rows"][-1] == [9999, "item_9999", 14998.5]


def test_provenance_and_calculation_origin(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Возврат происхождения расчёта: инструмент, параметры, SQL, source_result_id и факты."""
    client, factory, settings = results_env

    tool_res = ToolResult(
        result_id="res_derived_1",
        status="ok",
        data=ToolResultData(columns=["metric", "value"], rows=[["total_sum", 99000]]),
        tool="aggregate_column",
        params={"metric": "total_sum", "column": "amount", "operation": "sum"},
        sql="SELECT 'total_sum' AS metric, SUM(amount) AS value FROM orders",
        source_result_id="res_parent_source_42",
        duration_ms=45,
        facts=[Fact(text="Общая сумма заказов составила 99 000 руб.", values={"total_sum": 99000})],
        chart_hint=ChartHint(type="bar", x="metric", y=["value"]),
    )

    with factory() as db:
        save_tool_result(
            db,
            tool_res,
            owner_id="u_alice",
            persist_data=True,
            storage_dir=settings.results_data_dir,
        )

    res_meta = client.get("/api/results/res_derived_1")
    assert res_meta.status_code == 200
    meta = res_meta.json()

    assert meta["tool"] == "aggregate_column"
    assert meta["params"] == {"metric": "total_sum", "column": "amount", "operation": "sum"}
    assert meta["sql"] == "SELECT 'total_sum' AS metric, SUM(amount) AS value FROM orders"
    assert meta["source_result_id"] == "res_parent_source_42"
    assert meta["duration_ms"] == 45
    assert len(meta["facts"]) == 1
    assert meta["facts"][0]["text"] == "Общая сумма заказов составила 99 000 руб."
    assert meta["chart_hint"]["type"] == "bar"
    assert meta["chart_hint"]["x"] == "metric"
    assert meta["chart_hint"]["y"] == ["value"]


def test_missing_result_returns_404_error_response(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Несуществующий ID возвращает понятную ошибку 404 по общему контракту ErrorResponse."""
    client, _, _ = results_env

    for path in ("/api/results/non_existent_id", "/api/results/non_existent_id/data"):
        res = client.get(path)
        assert res.status_code == 404
        err = ErrorResponse.model_validate(res.json())
        assert err.error.code == "not_found"
        assert "не найден" in err.error.message.lower()


def test_foreign_user_result_is_hidden_with_404(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Результат другого пользователя возвращает 404 без раскрытия данных или ID."""
    client, factory, settings = results_env

    secret_tool_res = ToolResult(
        result_id="res_bob_secret",
        status="ok",
        data=ToolResultData(columns=["secret_col"], rows=[["confidential_data"]]),
        tool="run_sql",
        sql="SELECT confidential_data FROM secret_table",
        params={"sql": "SELECT confidential_data FROM secret_table"},
    )

    with factory() as db:
        # Сохраняем от имени bob
        save_tool_result(
            db,
            secret_tool_res,
            owner_id="u_bob",
            persist_data=True,
            storage_dir=settings.results_data_dir,
        )

    # Alice делает запрос к результату Bob'а
    for path in ("/api/results/res_bob_secret", "/api/results/res_bob_secret/data"):
        res = client.get(path)
        assert res.status_code == 404
        err = ErrorResponse.model_validate(res.json())
        assert err.error.code == "not_found"
        # Данные или SQL не должны утекать в тело ответа
        assert "confidential" not in res.text
        assert "secret_table" not in res.text


def test_unowned_result_is_hidden_with_404(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Результат без владельца (owner_id is None) недоступен пользователю через API."""
    client, factory, _ = results_env

    with factory() as db:
        db.add(
            ToolResultModel(
                result_id="res_unowned_audit",
                owner_id=None,
                status="ok",
                tool="audit_check",
            )
        )
        db.commit()

    for path in ("/api/results/res_unowned_audit", "/api/results/res_unowned_audit/data"):
        res = client.get(path)
        assert res.status_code == 404
        assert ErrorResponse.model_validate(res.json()).error.code == "not_found"


def test_error_status_result_metadata_and_data(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Результат со статусом error отдаёт метаданные с объектом ошибки и 404 на /data."""
    client, factory, settings = results_env

    error_res = ToolResult(
        result_id="res_err_1",
        status="error",
        error=ToolError(
            code="table_not_found",
            message="Таблица 'missing_table' не найдена в каталоге",
            suggestion="Проверьте правильность написания имени таблицы",
        ),
        tool="run_sql",
        sql="SELECT * FROM missing_table",
        params={"sql": "SELECT * FROM missing_table"},
    )

    with factory() as db:
        save_tool_result(
            db,
            error_res,
            owner_id="u_alice",
            persist_data=False,
            storage_dir=settings.results_data_dir,
        )

    # Метаданные доступны
    res_meta = client.get("/api/results/res_err_1")
    assert res_meta.status_code == 200
    meta = res_meta.json()
    assert meta["status"] == "error"
    assert meta["error"]["code"] == "table_not_found"
    assert meta["error"]["suggestion"] == "Проверьте правильность написания имени таблицы"
    assert meta["data"] is None

    # Попытка прочитать данные ошибочного результата даёт понятный 404
    res_data = client.get("/api/results/res_err_1/data")
    assert res_data.status_code == 404
    err = ErrorResponse.model_validate(res_data.json())
    assert err.error.code == "not_found"
    assert "не найдены" in err.error.message.lower()


def test_empty_status_result_metadata_and_data(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Результат со статусом empty (пустая выборка) отдаёт 0 строк через /data."""
    client, factory, settings = results_env

    empty_res = ToolResult(
        result_id="res_empty_1",
        status="empty",
        data=ToolResultData(columns=["id", "val"], rows=[], row_count=0),
        tool="run_sql",
        sql="SELECT id, val FROM t WHERE 1=0",
        params={"sql": "SELECT id, val FROM t WHERE 1=0"},
    )

    with factory() as db:
        save_tool_result(
            db,
            empty_res,
            owner_id="u_alice",
            persist_data=False,
            storage_dir=settings.results_data_dir,
        )

    res_meta = client.get("/api/results/res_empty_1")
    assert res_meta.status_code == 200
    assert res_meta.json()["status"] == "empty"

    res_data = client.get("/api/results/res_empty_1/data")
    assert res_data.status_code == 200
    data = res_data.json()
    assert data["row_count"] == 0
    assert data["rows"] == []


def test_disabled_local_mode_returns_503(tmp_path: Path) -> None:
    """При выключенном CP1_LOCAL_MODE доступ блокируется со статусом 503."""
    db_url = f"sqlite:///{tmp_path / 'disabled.db'}"
    settings = Settings(
        _env_file=None,
        app_database_url=db_url,
        cp1_local_mode=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        res = client.get("/api/results/res_any")
        assert res.status_code == 503
        err = ErrorResponse.model_validate(res.json())
        assert err.error.code == "unavailable"


def test_corrupted_parquet_graceful_fallback(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Повреждение Parquet-файла не ломает чтение и откатывается к preview с truncated=True."""
    client, factory, settings = results_env

    tool_res = ToolResult(
        result_id="res_corrupt_test",
        status="ok",
        data=ToolResultData(
            columns=["col_a", "col_b"],
            rows=[[1, "alpha"], [2, "beta"]],
            row_count=2,
        ),
        tool="run_sql",
        sql="SELECT 1, 'alpha'",
    )

    with factory() as db:
        save_tool_result(
            db,
            tool_res,
            owner_id="u_alice",
            persist_data=True,
            storage_dir=settings.results_data_dir,
        )

    # Ломаем файл на диске
    parquet_path = Path(settings.results_data_dir) / "res_corrupt_test.parquet"
    assert parquet_path.is_file()
    parquet_path.write_bytes(b"CORRUPTED_GARBAGE_BYTES_NOT_A_PARQUET")

    # Метаданные не страдают
    res_meta = client.get("/api/results/res_corrupt_test")
    assert res_meta.status_code == 200
    assert res_meta.json()["result_id"] == "res_corrupt_test"

    # Данные возвращаются из сохранённого превью без падения с 500
    res_data = client.get("/api/results/res_corrupt_test/data")
    assert res_data.status_code == 200
    data = res_data.json()
    assert data["rows"] == [[1, "alpha"], [2, "beta"]]
    assert data["truncated"] is True


def test_missing_parquet_without_preview_returns_404(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Отсутствующий parquet-файл без preview отдаёт понятный 404."""
    client, factory, _ = results_env

    with factory() as db:
        db.add(
            ToolResultModel(
                result_id="res_no_file",
                owner_id="u_alice",
                status="ok",
                tool="run_sql",
                data_ref="parquet://res_no_file.parquet",
                data_preview=None,
            )
        )
        db.commit()

    res_data = client.get("/api/results/res_no_file/data")
    assert res_data.status_code == 404
    err = ErrorResponse.model_validate(res_data.json())
    assert err.error.code == "not_found"
    assert "не найдены" in err.error.message.lower()


def test_data_ref_path_traversal_is_blocked(
    results_env: tuple[TestClient, sessionmaker[Session], Settings],
) -> None:
    """Попытка Path Traversal в data_ref отклоняется без утечки файлов."""
    client, factory, _ = results_env

    with factory() as db:
        db.add(
            ToolResultModel(
                result_id="res_traversal",
                owner_id="u_alice",
                status="ok",
                tool="run_sql",
                data_ref="parquet://../../../../etc/passwd",
                data_preview=None,
            )
        )
        db.commit()

    res_data = client.get("/api/results/res_traversal/data")
    assert res_data.status_code == 404
    err = ErrorResponse.model_validate(res_data.json())
    assert err.error.code == "not_found"
    assert "root:" not in res_data.text
