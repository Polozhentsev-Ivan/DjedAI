"""API определений метрик (B2-9): версии, вывод из употребления, владельцы, миграция."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from djedai.backend.app import create_app
from djedai.backend.auth import hash_password
from djedai.backend.config import Settings
from djedai.backend.db.models import MessageModel, UserModel
from djedai.backend.db.repositories import create_chat
from djedai.backend.db.session import create_db_engine
from djedai.backend.local_user import bootstrap_local_user
from djedai.contracts.metrics import MetricDefinition

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "metrics"
PASSWORD = "test-metric-password"

GROSS = {
    "metric_id": "gross_payments",
    "title": "Оплаты без возвратов",
    "kind": "sum",
    "grain": {"table": "payments", "key": "platezh_id"},
    "measure": {"table": "payments", "column": "summa"},
    "additivity": "additive",
    "filters": [{"column": {"table": "payments", "column": "tip"}, "op": "eq", "value": "payment"}],
    "unit": "money",
    "currency_column": {"table": "payments", "column": "valyuta"},
    "time_column": {"table": "payments", "column": "data_platezha"},
}
ORDERS = {
    "metric_id": "orders_count",
    "title": "Заказы",
    "kind": "count",
    "grain": {"table": "orders", "key": "zakaz_id"},
    "additivity": "additive",
    "unit": "count",
}
CUSTOMERS = {
    "metric_id": "customers_count",
    "title": "Клиенты",
    "kind": "count",
    "grain": {"table": "customers", "key": "klient_id"},
    "additivity": "additive",
    "unit": "count",
}
PER_CUSTOMER = {
    "metric_id": "orders_per_customer",
    "title": "Заказов на клиента",
    "kind": "ratio",
    "grain": {"table": "orders", "key": "zakaz_id"},
    "numerator": "orders_count",
    "denominator": "customers_count",
    "additivity": "recompute",
    "unit": "ratio",
}


@pytest.fixture(scope="module")
def account_password_hash() -> str:
    return hash_password(PASSWORD)


@contextmanager
def _client(settings: Settings, username: str = "local") -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        yield client


def _alembic(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    return cfg


@pytest.fixture
def configured(tmp_path: Path, account_password_hash: str):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    command.upgrade(_alembic(url), "head")
    engine = create_db_engine(url)
    factory = sessionmaker(bind=engine)
    for user_id in ("local", "other"):
        bootstrap_local_user(factory, user_id, username=user_id)
    with factory() as db:
        for user_id in ("local", "other"):
            db.get(UserModel, user_id).password_hash = account_password_hash
        db.commit()
    settings = Settings(
        _env_file=None,
        cp1_local_mode=False,
        app_database_url=url,
        datasets_data_dir=str(tmp_path / "datasets"),
    )
    yield settings, factory
    engine.dispose()


def _upload(client: TestClient) -> str:
    files = [
        ("files", (path.name, path.read_bytes(), "text/csv"))
        for path in sorted(FIXTURES.glob("*.csv"))
    ]
    response = client.post("/api/datasets/upload", files=files)
    assert response.status_code == 202, response.text
    dataset_id = response.json()["dataset_id"]
    assert "event: done\n" in client.get(f"/api/datasets/{dataset_id}/events").text
    return dataset_id


def test_metric_versions_are_confirmed_retired_and_owner_scoped(configured) -> None:
    settings, _ = configured
    with _client(settings) as client:
        dataset_id = _upload(client)
        base = f"/api/datasets/{dataset_id}/metrics"
        assert client.get(base).json() == []

        # Клиент не может назначить версию или подтверждение: их ставит сервер.
        created = client.post(base, json={**GROSS, "version": 7})
        assert created.status_code == 201, created.text
        first = MetricDefinition.model_validate(created.json())
        assert (first.version, first.status) == (1, "confirmed")
        assert first.confirmation.source == "api"
        assert first.confirmation.by_user_id == "local"

        net = {**GROSS, "title": "Оплаты с возвратами", "filters": []}
        second = MetricDefinition.model_validate(client.post(base, json=net).json())
        assert second.version == 2
        assert [m["version"] for m in client.get(base).json()] == [2]
        history = client.get(base, params={"history": "true"}).json()
        assert [(m["version"], m["status"]) for m in history] == [(1, "retired"), (2, "confirmed")]

        # Отношение требует подтверждённых составляющих.
        assert client.post(base, json=PER_CUSTOMER).status_code == 422
        assert client.post(base, json=ORDERS).status_code == 201
        assert client.post(base, json=CUSTOMERS).status_code == 201
        assert client.post(base, json=PER_CUSTOMER).status_code == 201
        assert client.delete(f"{base}/customers_count").status_code == 409
        assert client.delete(f"{base}/orders_per_customer").status_code == 204
        assert client.delete(f"{base}/orders_per_customer").status_code == 404
        assert client.delete(f"{base}/customers_count").status_code == 204
        assert {m["metric_id"] for m in client.get(base).json()} == {
            "gross_payments",
            "orders_count",
        }

        # Определение сверяется с каталогом: переименованная колонка не угадывается.
        wrong = {**GROSS, "measure": {"table": "payments", "column": "amount"}}
        response = client.post(base, json=wrong)
        assert response.status_code == 422
        assert "payments.amount" in response.text
        not_unique = {**ORDERS, "grain": {"table": "orders", "key": "klient_id"}}
        assert client.post(base, json=not_unique).status_code == 422
        assert client.post(base, json={**GROSS, "currency_column": None}).status_code == 422

    with _client(settings, "other") as other:
        assert other.get(base).status_code == 404
        assert other.post(base, json=ORDERS).status_code == 404
        assert other.delete(f"{base}/orders_count").status_code == 404

    with _client(settings) as restarted:
        assert len(restarted.get(base).json()) == 2


def test_choosing_a_clarification_option_confirms_its_candidate(configured) -> None:
    settings, factory = configured
    with _client(settings) as client:
        dataset_id = _upload(client)
        service = client.app.state.metric_service
        with factory() as db:
            create_chat(db, "chat", "local", dataset_id)
            db.add(MessageModel(message_id="m_answer", chat_id="chat", role="assistant"))
            service.propose(
                db,
                "local",
                dataset_id,
                "m_answer",
                [("Оплаты без возвратов", MetricDefinition.model_validate(GROSS))],
            )
            db.add(MessageModel(message_id="m_question", chat_id="chat", role="user"))
            db.commit()

        assert service.confirm_choice("local", dataset_id, "chat", "Другое", "m_question") is None
        assert (
            service.confirm_choice(
                "other", dataset_id, "chat", "Оплаты без возвратов", "m_question"
            )
            is None
        )
        chosen = service.confirm_choice(
            "local", dataset_id, "chat", " Оплаты без возвратов ", "m_question"
        )
        assert chosen is not None
        assert chosen.confirmation.source == "clarification"
        assert chosen.confirmation.message_id == "m_answer"
        listed = client.get(f"/api/datasets/{dataset_id}/metrics").json()
        assert [m["metric_id"] for m in listed] == ["gross_payments"]


def test_metric_migration_upgrades_and_downgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = _alembic(url)
    command.upgrade(cfg, "e161chat0001")
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    try:
        tables = inspect(engine).get_table_names()
        assert {"metric_definitions", "metric_proposals"} <= set(tables)
        assert "metric" in {c["name"] for c in inspect(engine).get_columns("results")}
        command.downgrade(cfg, "e161chat0001")
        inspector = inspect(engine)
        assert "metric_definitions" not in inspector.get_table_names()
        assert "metric" not in {c["name"] for c in inspector.get_columns("results")}
        with engine.connect() as db:
            assert db.execute(text("SELECT count(*) FROM results")).scalar() == 0
    finally:
        engine.dispose()
