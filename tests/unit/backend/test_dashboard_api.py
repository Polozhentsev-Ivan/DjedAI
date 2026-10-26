"""Тесты Dashboard API и refresh (§7.2, B5-6 / #97).

Проверяют:
- Создание, чтение, изменение (PATCH) и список (GET) дашбордов;
- Реконструкцию воспроизводимых рецептов (WidgetRecipe) из цепочки source_result_id;
- Автономный refresh без активного чата в DuckDB;
- Вычисление зависимых шагов через представление source_result;
- Генерацию новых result_id при каждом refresh;
- Изолирование частичного сбоя (Partial Failure): ошибки одного виджета не ломают остальные;
- Соблюдение лимитов бюджета вызовов (DASHBOARD_MAX_TOOL_CALLS);
- Изоляцию прав владельца (AUTH-3 / AUTH-5) в двух сессиях (Alice и Bob).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from djedai.backend.app import create_app
from djedai.backend.auth import create_session, hash_password
from djedai.backend.config import Settings, get_settings
from djedai.backend.db.models import (
    ChatModel,
    DatasetModel,
    UserModel,
)
from djedai.backend.db.repositories import save_tool_result
from djedai.backend.db.session import create_db_engine, get_db, init_db
from djedai.contracts.catalog import Column, DatasetCatalog, DatasetSource, Table
from djedai.contracts.tool_result import ToolResult, ToolResultData


@pytest.fixture(scope="module")
def account_password_hash() -> str:
    return hash_password("test-dashboard-password")


@pytest.fixture
def dashboard_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_password_hash: str
) -> Iterator[tuple[TestClient, sessionmaker[Session], Settings, str, str, str, Path]]:
    db_path = tmp_path / "app.db"
    datasets_dir = tmp_path / "datasets_storage"
    results_dir = tmp_path / "results_storage"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{db_path}"
    engine = create_db_engine(db_url)
    init_db(engine)
    factory = sessionmaker(bind=engine)

    # Создаём реальный DuckDB файл датасета с таблицей sales
    duck_dir = datasets_dir / "ds_alice"
    duck_dir.mkdir(parents=True, exist_ok=True)
    duck_file = duck_dir / "dataset.duckdb"
    with duckdb.connect(str(duck_file)) as con:
        con.execute("CREATE TABLE sales (id INTEGER, category VARCHAR, amount DOUBLE)")
        con.execute(
            "INSERT INTO sales VALUES "
            "(1, 'electronics', 100.0), (2, 'electronics', 200.0), (3, 'books', 50.0)"
        )

    # Пользователи: alice и bob
    with factory() as db:
        alice = UserModel(
            id="u_alice", username="alice", password_hash=account_password_hash, is_active=True
        )
        bob = UserModel(
            id="u_bob", username="bob", password_hash=account_password_hash, is_active=True
        )
        db.add_all([alice, bob])
        db.commit()

        # Датасет Алисы
        catalog = DatasetCatalog(
            dataset_id="ds_alice",
            name="Alice Dataset",
            created_at=datetime.now(UTC),
            source=DatasetSource(kind="csv_upload"),
            tables=[
                Table(
                    table_id="sales",
                    original_name="sales",
                    row_count=3,
                    duplicate_rows=0,
                    columns=[
                        Column(
                            name="id",
                            original_name="id",
                            physical_type="INTEGER",
                            semantic_type="integer",
                            role="dimension",
                            null_ratio=0.0,
                            distinct_count=3,
                            is_unique=True,
                        ),
                        Column(
                            name="category",
                            original_name="category",
                            physical_type="VARCHAR",
                            semantic_type="category",
                            role="dimension",
                            null_ratio=0.0,
                            distinct_count=2,
                            is_unique=False,
                        ),
                        Column(
                            name="amount",
                            original_name="amount",
                            physical_type="DOUBLE",
                            semantic_type="float",
                            role="measure",
                            null_ratio=0.0,
                            distinct_count=3,
                            is_unique=False,
                        ),
                    ],
                )
            ],
            relations=[],
        )
        alice_ds = DatasetModel(
            dataset_id="ds_alice",
            owner_id="u_alice",
            name="Alice Dataset",
            source_kind="csv",
            status="ready",
            duckdb_path="ds_alice/dataset.duckdb",
            catalog=catalog.model_dump(mode="json"),
        )
        # Чат Алисы
        alice_chat = ChatModel(
            chat_id="chat_alice",
            owner_id="u_alice",
            dataset_id="ds_alice",
            title="Чат Алисы",
        )
        db.add_all([alice_ds, alice_chat])
        db.commit()

        alice_token = create_session(db, alice.id)
        bob_token = create_session(db, bob.id)

    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("DATASETS_DATA_DIR", str(datasets_dir))
    monkeypatch.setenv("RESULTS_DATA_DIR", str(results_dir))
    get_settings.cache_clear()

    settings = Settings(
        _env_file=None,
        app_database_url=db_url,
        datasets_data_dir=str(datasets_dir),
        results_data_dir=str(results_dir),
        cp1_local_mode=False,
    )

    app = create_app(settings)
    app.state.session_factory = factory

    def override_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, factory, settings, alice_token, bob_token, "ds_alice", results_dir


def test_create_and_read_independent_dashboard(dashboard_env):
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env

    # 1. Создаём ToolResult в БД Алисы (как будто он только что вычислен в чате)
    with factory() as db:
        res = ToolResult(
            result_id="res_sales_cat",
            status="ok",
            tool="run_sql",
            sql="SELECT category, SUM(amount) AS total FROM sales GROUP BY category",
            params={"sql": "SELECT category, SUM(amount) AS total FROM sales GROUP BY category"},
            data=ToolResultData(
                columns=["category", "total"],
                rows=[["books", 50.0], ["electronics", 300.0]],
                row_count=2,
            ),
        )
        save_tool_result(
            db,
            res,
            chat_id="chat_alice",
            owner_id="u_alice",
            persist_data=True,
            storage_dir=results_dir,
        )

    # 2. Создаем дашборд через POST /dashboards
    chart = {
        "chart_id": "chart_1",
        "result_id": "res_sales_cat",
        "type": "bar",
        "title": "Продажи по категориям",
        "subtitle": "Сводка продаж",
        "y": [{"field": "total", "label": "Сумма"}],
        "x": {"field": "category", "label": "Категория"},
    }
    payload = {
        "dataset_id": dataset_id,
        "title": "Дашборд продаж",
        "result_id": "res_sales_cat",
        "chart": chart,
    }

    headers = {"Authorization": f"Bearer {alice_token}"}
    print("--> BEFORE POST")
    resp = client.post("/api/dashboards", json=payload, headers=headers)
    print("--> AFTER POST", resp.status_code)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "Дашборд продаж"
    assert data["dataset_id"] == dataset_id
    assert len(data["widgets"]) == 1
    widget = data["widgets"][0]
    assert widget["status"] == "ok"
    assert widget["result_id"] == "res_sales_cat"
    assert widget["tool_call"] is not None
    assert widget["recipe"] is None
    dash_id = data["dashboard_id"]

    # 3. Читаем через GET /dashboards/{id}
    get_resp = client.get(f"/api/dashboards/{dash_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["dashboard_id"] == dash_id

    # 4. Список дашбордов через GET /dashboards?dataset_id=...
    list_resp = client.get(f"/api/dashboards?dataset_id={dataset_id}", headers=headers)
    assert list_resp.status_code == 200
    summaries = list_resp.json()
    assert len(summaries) == 1
    assert summaries[0]["dashboard_id"] == dash_id


def test_add_second_widget_and_patch(dashboard_env):
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    # Создаём 2 результата
    with factory() as db:
        r1 = ToolResult(
            result_id="res_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT 1"},
            data=ToolResultData(columns=["n"], rows=[[1]], row_count=1),
        )
        r2 = ToolResult(
            result_id="res_2",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT 2"},
            data=ToolResultData(columns=["n"], rows=[[2]], row_count=1),
        )
        save_tool_result(db, r1, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)
        save_tool_result(db, r2, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)

    # Создаем дашборд с первым виджетом
    c1 = {"chart_id": "c1", "result_id": "res_1", "type": "kpi", "title": "Виджет 1"}
    resp1 = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "res_1", "chart": c1},
        headers=headers,
    )
    assert resp1.status_code == 200
    dash_id = resp1.json()["dashboard_id"]
    w1_id = resp1.json()["widgets"][0]["widget_id"]

    # Добавляем второй виджет в существующий дашборд
    c2 = {"chart_id": "c2", "result_id": "res_2", "type": "kpi", "title": "Виджет 2"}
    resp2 = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "dashboard_id": dash_id, "result_id": "res_2", "chart": c2},
        headers=headers,
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["widgets"]) == 2
    w2 = data2["widgets"][1]
    assert w2["layout"]["y"] >= 4  # Расположен ниже первого виджета
    w2_id = w2["widget_id"]

    # PATCH: переименование и удаление первого виджета
    patch_resp = client.patch(
        f"/api/dashboards/{dash_id}",
        json={"title": "Обновлённый дашборд", "remove_widget_ids": [w1_id]},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["title"] == "Обновлённый дашборд"
    assert len(patched["widgets"]) == 1
    assert patched["widgets"][0]["widget_id"] == w2_id


def test_reconstruct_recipe_with_dependent_computation(dashboard_env):
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    # Цепочка вычислений:
    # Шаг 1: r_step1 = run_sql (SELECT id, amount FROM sales WHERE amount > 80)
    # Шаг 2: r_step2 = run_sql с source_result_id="r_step1"
    with factory() as db:
        r1 = ToolResult(
            result_id="r_step1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT id, amount FROM sales WHERE amount > 80"},
            data=ToolResultData(
                columns=["id", "amount"], rows=[[1, 100.0], [2, 200.0]], row_count=2
            ),
        )
        r2 = ToolResult(
            result_id="r_step2",
            source_result_id="r_step1",
            status="ok",
            tool="run_sql",
            params={
                "sql": "SELECT COUNT(*) AS high_sales FROM source_result",
                "source_result_id": "r_step1",
            },
            data=ToolResultData(columns=["high_sales"], rows=[[2]], row_count=1),
        )
        save_tool_result(db, r1, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)
        save_tool_result(db, r2, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)

    chart = {
        "chart_id": "c_recipe",
        "result_id": "r_step2",
        "type": "kpi",
        "title": "Число крупных продаж",
    }
    resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "r_step2", "chart": chart},
        headers=headers,
    )
    assert resp.status_code == 200
    widget = resp.json()["widgets"][0]
    assert widget["tool_call"] is None
    assert widget["recipe"] is not None
    recipe = widget["recipe"]
    assert len(recipe["steps"]) == 2
    assert recipe["steps"][0]["step_id"] == "step_1"
    assert recipe["steps"][0]["source_step_id"] is None
    assert recipe["steps"][1]["step_id"] == "step_2"
    assert recipe["steps"][1]["source_step_id"] == "step_1"
    assert recipe["output_step_id"] == "step_2"
    # Убеждаемся, что временный UUID вычищен из params шагов
    assert "source_result_id" not in recipe["steps"][1]["call"]["params"]


def test_refresh_generates_new_results_and_executes_dependencies(dashboard_env):
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    # Создаем зависимую цепочку
    with factory() as db:
        r1 = ToolResult(
            result_id="r_initial_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT id, category, amount FROM sales"},
            data=ToolResultData(
                columns=["id", "category", "amount"],
                rows=[[1, "electronics", 100.0], [2, "electronics", 200.0], [3, "books", 50.0]],
                row_count=3,
            ),
        )
        r2 = ToolResult(
            result_id="r_initial_2",
            source_result_id="r_initial_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT COUNT(*) AS n FROM source_result"},
            data=ToolResultData(columns=["n"], rows=[[3]], row_count=1),
        )
        save_tool_result(db, r1, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)
        save_tool_result(db, r2, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)

    chart = {"chart_id": "c_kpi", "result_id": "r_initial_2", "type": "kpi", "title": "Total Rows"}
    create_resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "r_initial_2", "chart": chart},
        headers=headers,
    )
    assert create_resp.status_code == 200
    dash_id = create_resp.json()["dashboard_id"]

    # Вызываем POST /dashboards/{id}/refresh
    refresh_resp = client.post(f"/api/dashboards/{dash_id}/refresh", headers=headers)
    assert refresh_resp.status_code == 200, refresh_resp.text
    snapshot = refresh_resp.json()
    assert snapshot["refreshed_at"] is not None
    widget = snapshot["widgets"][0]
    assert widget["status"] == "ok"
    new_res_id = widget["result_id"]
    # Идентификатор должен быть НОВЫМ
    assert new_res_id != "r_initial_2"
    assert widget["chart"]["result_id"] == new_res_id

    # Проверяем, что результат действительно вычислен и доступен через /results/{id}/data
    data_resp = client.get(f"/api/results/{new_res_id}/data", headers=headers)
    assert data_resp.status_code == 200
    rows = data_resp.json()["rows"]
    assert rows == [[3]]


def test_partial_failure_isolates_errors_and_preserves_good_widgets(dashboard_env):
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    # 1. Хороший результат
    with factory() as db:
        r_good = ToolResult(
            result_id="r_good",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT SUM(amount) AS total FROM sales"},
            data=ToolResultData(columns=["total"], rows=[[350.0]], row_count=1),
        )
        # 2. Результат, чей SQL при повторном выполнении вызовет синтаксическую ошибку
        r_bad = ToolResult(
            result_id="r_bad",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT * FROM non_existent_table_xyz"},
            data=ToolResultData(columns=["x"], rows=[[1]], row_count=1),
        )
        save_tool_result(
            db, r_good, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir
        )
        save_tool_result(
            db, r_bad, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir
        )

    c_good = {"chart_id": "c_good", "result_id": "r_good", "type": "kpi", "title": "Good KPI"}
    c_bad = {"chart_id": "c_bad", "result_id": "r_bad", "type": "kpi", "title": "Bad KPI"}

    # Создаём дашборд с первым виджетом
    resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "r_good", "chart": c_good},
        headers=headers,
    )
    dash_id = resp.json()["dashboard_id"]

    # Добавляем второй (плохой) виджет
    client.post(
        "/api/dashboards",
        json={
            "dataset_id": dataset_id,
            "dashboard_id": dash_id,
            "result_id": "r_bad",
            "chart": c_bad,
        },
        headers=headers,
    )

    # Запускаем refresh: один виджет успешен, второй с ошибкой
    refresh_resp = client.post(f"/api/dashboards/{dash_id}/refresh", headers=headers)
    assert refresh_resp.status_code == 200, refresh_resp.text
    snapshot = refresh_resp.json()
    assert len(snapshot["widgets"]) == 2

    w1 = snapshot["widgets"][0]
    w2 = snapshot["widgets"][1]

    # Первый виджет успешен
    assert w1["status"] == "ok"
    assert w1["result_id"] is not None
    assert w1["error"] is None

    # Второй виджет содержит явную ошибку и result_id=null
    assert w2["status"] == "error"
    assert w2["result_id"] is None
    assert w2["error"] is not None
    assert any(err in w2["error"]["code"] for err in ("error", "violation", "scope"))


def test_owner_isolation_two_users_cannot_access_or_refresh_each_other(dashboard_env):
    client, factory, settings, alice_token, bob_token, dataset_id, results_dir = dashboard_env
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    # Создаём дашборд Алисы
    with factory() as db:
        r = ToolResult(
            result_id="alice_res",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT 1"},
            data=ToolResultData(columns=["x"], rows=[[1]], row_count=1),
        )
        save_tool_result(db, r, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)

    c = {"chart_id": "c_alice", "result_id": "alice_res", "type": "kpi", "title": "Alice KPI"}
    create_resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "alice_res", "chart": c},
        headers=alice_headers,
    )
    assert create_resp.status_code == 200
    dash_id = create_resp.json()["dashboard_id"]

    # 1. Боб пытается прочитать дашборд Алисы -> 404
    assert client.get(f"/api/dashboards/{dash_id}", headers=bob_headers).status_code == 404

    # 2. Боб пытается обновить дашборд Алисы -> 404
    assert client.post(f"/api/dashboards/{dash_id}/refresh", headers=bob_headers).status_code == 404

    # 3. Боб пытается изменить дашборд Алисы -> 404
    patch_bob = client.patch(
        f"/api/dashboards/{dash_id}", json={"title": "Hacked"}, headers=bob_headers
    )
    assert patch_bob.status_code == 404

    # 4. Боб пытается создать дашборд на основе результата Алисы -> 404
    assert (
        client.post(
            "/api/dashboards",
            json={"dataset_id": dataset_id, "result_id": "alice_res", "chart": c},
            headers=bob_headers,
        ).status_code
        == 404
    )

    # 5. Боб запрашивает список дашбордов чужого датасета -> 404
    list_bob = client.get(f"/api/dashboards?dataset_id={dataset_id}", headers=bob_headers)
    assert list_bob.status_code == 404


def test_reopen_and_refresh_after_restart(dashboard_env):
    """Проверка сценария перезапуска приложения и повторного открытия дашборда."""
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    # 1. Создаем зависимую цепочку
    with factory() as db:
        r1 = ToolResult(
            result_id="r_restart_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT id, amount FROM sales WHERE amount >= 100"},
            data=ToolResultData(
                columns=["id", "amount"], rows=[[1, 100.0], [2, 200.0]], row_count=2
            ),
        )
        r2 = ToolResult(
            result_id="r_restart_2",
            source_result_id="r_restart_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT SUM(amount) AS sum_high FROM source_result"},
            data=ToolResultData(columns=["sum_high"], rows=[[300.0]], row_count=1),
        )
        save_tool_result(db, r1, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)
        save_tool_result(db, r2, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)

    c = {"chart_id": "c_restart", "result_id": "r_restart_2", "type": "kpi", "title": "Sum High"}
    create_resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "r_restart_2", "chart": c},
        headers=headers,
    )
    assert create_resp.status_code == 200
    dash_id = create_resp.json()["dashboard_id"]

    # 2. Имитируем перезапуск сервера: создаём новый экземпляр FastAPI приложения
    # поверх того же файла SQLite и хранилища DuckDB/Parquet
    new_app = create_app(settings)
    new_app.state.session_factory = factory

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    new_app.dependency_overrides[get_db] = override_db

    with TestClient(new_app) as restarted_client:
        # Повторное открытие через GET не выполняет пересчёт и не меняет ID
        get_resp = restarted_client.get(f"/api/dashboards/{dash_id}", headers=headers)
        assert get_resp.status_code == 200
        snap = get_resp.json()
        assert snap["widgets"][0]["result_id"] == "r_restart_2"
        assert snap["refreshed_at"] is None

        # Выполняем POST refresh после перезапуска
        refresh_resp = restarted_client.post(
            f"/api/dashboards/{dash_id}/refresh", headers=headers
        )
        assert refresh_resp.status_code == 200
        refreshed_snap = refresh_resp.json()
        assert refreshed_snap["refreshed_at"] is not None

        w = refreshed_snap["widgets"][0]
        assert w["status"] == "ok"
        new_id = w["result_id"]
        assert new_id != "r_restart_2"

        # Проверяем доступность пересчитанных данных
        data_resp = restarted_client.get(f"/api/results/{new_id}/data", headers=headers)
        assert data_resp.status_code == 200
        assert data_resp.json()["rows"] == [[300.0]]


def test_intermediate_step_failure_stops_downstream_and_isolates_widget(dashboard_env):
    """Сбой промежуточного шага рецепта прерывает зависимые шаги и изолирует виджет."""
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    with factory() as db:
        # 1. Хороший независимый результат для второго виджета
        r_other = ToolResult(
            result_id="r_other",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT COUNT(*) AS total_count FROM sales"},
            data=ToolResultData(columns=["total_count"], rows=[[3]], row_count=1),
        )
        # 2. Трехшаговая цепочка, где шаг 2 сломан
        # Шаг 1: корректный
        s1 = ToolResult(
            result_id="s_step_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT id, category FROM sales"},
            data=ToolResultData(
                columns=["id", "category"],
                rows=[[1, "electronics"], [2, "electronics"], [3, "books"]],
                row_count=3,
            ),
        )
        # Шаг 2: SQL с обращением к несуществующей колонке
        s2 = ToolResult(
            result_id="s_step_2",
            source_result_id="s_step_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT bad_column FROM source_result"},
            data=ToolResultData(columns=["bad_column"], rows=[[1]], row_count=1),
        )
        # Шаг 3: зависит от Шага 2
        s3 = ToolResult(
            result_id="s_step_3",
            source_result_id="s_step_2",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT COUNT(*) FROM source_result"},
            data=ToolResultData(columns=["count"], rows=[[1]], row_count=1),
        )
        for r in (r_other, s1, s2, s3):
            save_tool_result(
                db, r, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir
            )

    # Создаём дашборд с зависимым 3-шаговым виджетом
    c3 = {"chart_id": "c3", "result_id": "s_step_3", "type": "kpi", "title": "Chained KPI"}
    resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "s_step_3", "chart": c3},
        headers=headers,
    )
    assert resp.status_code == 200
    dash_id = resp.json()["dashboard_id"]

    # Добавляем второй независимый хороший виджет
    c_other = {"chart_id": "c_other", "result_id": "r_other", "type": "kpi", "title": "Other KPI"}
    client.post(
        "/api/dashboards",
        json={
            "dataset_id": dataset_id,
            "dashboard_id": dash_id,
            "result_id": "r_other",
            "chart": c_other,
        },
        headers=headers,
    )

    # Выполняем refresh
    refresh_resp = client.post(f"/api/dashboards/{dash_id}/refresh", headers=headers)
    assert refresh_resp.status_code == 200
    snap = refresh_resp.json()
    assert len(snap["widgets"]) == 2

    w_chained = snap["widgets"][0]
    w_other = snap["widgets"][1]

    # Первый виджет упал на промежуточном шаге, зависимый шаг 3 не дал ложного успеха
    assert w_chained["status"] == "error"
    assert w_chained["result_id"] is None
    assert w_chained["error"] is not None

    # Второй независимый виджет пересчитан успешно
    assert w_other["status"] == "ok"
    assert w_other["result_id"] is not None
    assert w_other["error"] is None


def test_broken_dependency_chain_rejected_on_widget_creation(dashboard_env):
    """Превышение лимита шагов рецепта (MAX_RECIPE_STEPS) отвергается с кодом 422."""
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    # Создаём цепочку из 6 шагов (превышает MAX_RECIPE_STEPS = 5)
    with factory() as db:
        prev_id = None
        for i in range(1, 7):
            cur_id = f"r_chain_{i}"
            r = ToolResult(
                result_id=cur_id,
                source_result_id=prev_id,
                status="ok",
                tool="run_sql",
                params={"sql": f"SELECT {i} AS step"},
                data=ToolResultData(columns=["step"], rows=[[i]], row_count=1),
            )
            save_tool_result(
                db, r, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir
            )
            prev_id = cur_id

    # Пытаемся добавить виджет на основе 6-го шага -> 422
    chart = {"chart_id": "c_deep", "result_id": "r_chain_6", "type": "kpi", "title": "Too Deep"}
    resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "r_chain_6", "chart": chart},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "лимит" in resp.text.lower()


def test_patch_dashboard_layout_persistence(dashboard_env):
    """PATCH только с layout корректно сохраняется в БД и возвращается при GET (§7.2, #97)."""
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    with factory() as db:
        res = ToolResult(
            result_id="res_layout_test",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT 1"},
            data=ToolResultData(columns=["val"], rows=[[1]], row_count=1),
        )
        save_tool_result(
            db, res, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir
        )

    chart = {"chart_id": "c_lay", "result_id": "res_layout_test", "type": "kpi", "title": "Layout"}
    create_resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "res_layout_test", "chart": chart},
        headers=headers,
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    dash_id = created["dashboard_id"]
    wid = created["widgets"][0]["widget_id"]
    assert created["widgets"][0]["layout"] == {"x": 0, "y": 0, "w": 6, "h": 4}

    # PATCH только с layout
    patch_resp = client.patch(
        f"/api/dashboards/{dash_id}",
        json={"layout": {wid: {"x": 1, "y": 12, "w": 3, "h": 2}}},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["widgets"][0]["layout"] == {"x": 1, "y": 12, "w": 3, "h": 2}

    # Последующее чтение GET дашборда возвращает обновлённый layout из БД
    get_resp = client.get(f"/api/dashboards/{dash_id}", headers=headers)
    assert get_resp.status_code == 200
    reloaded = get_resp.json()
    assert reloaded["widgets"][0]["layout"] == {"x": 1, "y": 12, "w": 3, "h": 2}


def test_save_refreshed_dependent_widget_result_to_another_dashboard(dashboard_env):
    """Результат пересчитанного зависимого виджета сохраняется в другой дашборд (§7.2, #97)."""
    client, factory, settings, alice_token, _, dataset_id, results_dir = dashboard_env
    headers = {"Authorization": f"Bearer {alice_token}"}

    # Создаём 2-шаговую зависимую цепочку
    with factory() as db:
        r1 = ToolResult(
            result_id="r_prov_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT id, category, amount FROM sales"},
            data=ToolResultData(
                columns=["id", "category", "amount"],
                rows=[[1, "electronics", 100.0], [2, "electronics", 200.0]],
                row_count=2,
            ),
        )
        r2 = ToolResult(
            result_id="r_prov_2",
            source_result_id="r_prov_1",
            status="ok",
            tool="run_sql",
            params={"sql": "SELECT SUM(amount) AS total FROM source_result"},
            data=ToolResultData(columns=["total"], rows=[[300.0]], row_count=1),
        )
        save_tool_result(db, r1, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)
        save_tool_result(db, r2, chat_id="chat_alice", owner_id="u_alice", storage_dir=results_dir)

    chart = {"chart_id": "c_prov", "result_id": "r_prov_2", "type": "kpi", "title": "Prov KPI"}
    create_resp = client.post(
        "/api/dashboards",
        json={"dataset_id": dataset_id, "result_id": "r_prov_2", "chart": chart},
        headers=headers,
    )
    assert create_resp.status_code == 200
    dash1_id = create_resp.json()["dashboard_id"]

    # Выполняем refresh первого дашборда
    refresh_resp = client.post(f"/api/dashboards/{dash1_id}/refresh", headers=headers)
    assert refresh_resp.status_code == 200
    refreshed_wid = refresh_resp.json()["widgets"][0]
    assert refreshed_wid["status"] == "ok"
    new_res_id = refreshed_wid["result_id"]
    assert new_res_id != "r_prov_2"

    # Сохраняем свежий результат в НОВЫЙ дашборд через POST /api/dashboards
    c_new = {"chart_id": "c_dash2", "result_id": new_res_id, "type": "kpi", "title": "Dash2 KPI"}
    save_new_resp = client.post(
        "/api/dashboards",
        json={
            "dataset_id": dataset_id,
            "result_id": new_res_id,
            "chart": c_new,
            "title": "Второй дашборд",
        },
        headers=headers,
    )
    assert save_new_resp.status_code == 200, save_new_resp.text
    dash2_data = save_new_resp.json()
    dash2_id = dash2_data["dashboard_id"]
    assert dash2_id != dash1_id
    assert len(dash2_data["widgets"]) == 1
    w2 = dash2_data["widgets"][0]
    assert w2["result_id"] == new_res_id
    assert w2["recipe"] is not None
    assert len(w2["recipe"]["steps"]) == 2

    # Сохраняем свежий результат в СУЩЕСТВУЮЩИЙ дашборд
    c_add = {"chart_id": "c_add", "result_id": new_res_id, "type": "kpi", "title": "Added KPI"}
    add_resp = client.post(
        "/api/dashboards",
        json={
            "dataset_id": dataset_id,
            "dashboard_id": dash1_id,
            "result_id": new_res_id,
            "chart": c_add,
        },
        headers=headers,
    )
    assert add_resp.status_code == 200, add_resp.text
    dash1_reloaded = add_resp.json()
    assert len(dash1_reloaded["widgets"]) == 2

    # Проверяем, что второй дашборд с перенесённым рецептом автономно пересчитывается
    refresh2_resp = client.post(f"/api/dashboards/{dash2_id}/refresh", headers=headers)
    assert refresh2_resp.status_code == 200
    dash2_refreshed = refresh2_resp.json()
    assert dash2_refreshed["widgets"][0]["status"] == "ok"
    assert dash2_refreshed["widgets"][0]["result_id"] is not None
    assert dash2_refreshed["widgets"][0]["result_id"] != new_res_id
