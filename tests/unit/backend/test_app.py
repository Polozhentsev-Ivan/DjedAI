"""Тесты API эндпоинтов и фабрики FastAPI приложения."""

from __future__ import annotations

from fastapi.testclient import TestClient

from djedai.backend.app import create_app
from djedai.backend.config import Settings


def test_health_endpoints() -> None:
    settings = Settings(app_env="test", _env_file=None)
    app = create_app(settings)
    client = TestClient(app)

    # Корневой /health
    res_root = client.get("/health")
    assert res_root.status_code == 200
    data_root = res_root.json()
    assert data_root["status"] == "ok"
    assert data_root["app_env"] == "test"
    assert "version" in data_root

    # API /api/health
    res_api = client.get("/api/health")
    assert res_api.status_code == 200
    assert res_api.json()["status"] == "ok"


def test_api_subroutes_stubs() -> None:
    app = create_app()
    client = TestClient(app)

    # Datasets
    res_ds = client.get("/api/datasets")
    assert res_ds.status_code == 200
    assert res_ds.json() == []

    # Chats
    res_chats = client.get("/api/chats")
    assert res_chats.status_code == 200
    assert res_chats.json() == []

    # Results
    res_res = client.get("/api/results/r_test")
    assert res_res.status_code == 200
    assert res_res.json()["result_id"] == "r_test"

    # Dashboards
    res_dash = client.get("/api/dashboards")
    assert res_dash.status_code == 200
    assert res_dash.json() == []

    # Auth
    res_auth = client.get("/api/auth/me")
    assert res_auth.status_code == 200
    assert "username" in res_auth.json()
