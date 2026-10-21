"""Тесты API эндпоинтов, CORS-политики и фабрики FastAPI приложения."""

from __future__ import annotations

from fastapi.testclient import TestClient

from djedai.backend.app import create_app
from djedai.backend.config import Settings

REQUIRED_SCHEMAS = [
    "AssistantMessage",
    "MessageBlock",
    "TextBlock",
    "ChartBlock",
    "TableBlock",
    "ClarificationBlock",
    "DashboardLinkBlock",
    "SuggestionsBlock",
    "ToolResult",
    "ToolResultData",
    "ToolResultColumn",
    "ChartSpec",
    "Dashboard",
    "DashboardWidget",
    "DatasetCatalog",
    "Table",
    "Column",
    "UserRead",
    "UserCreate",
    "UserLogin",
    "TokenResponse",
    "AgentStreamEvent",
    "DatasetStreamEvent",
    "DatasetSummary",
    "HealthCheck",
    "ChatCreate",
    "ChatCreated",
    "ChatSummary",
    "ChatMessage",
    "ChatDetail",
    "SendMessageRequest",
]


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


def test_cors_preflight_and_origins() -> None:
    """Проверка разрешенных dev origins и блокировки недоверенных источников."""
    app = create_app()
    client = TestClient(app)

    allowed_origin = "http://localhost:5173"
    disallowed_origin = "https://untrusted.invalid"

    # 1. Корректный preflight для разрешенного dev origin
    headers_allowed = {
        "Origin": allowed_origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    res_preflight_ok = client.options("/api/health", headers=headers_allowed)
    assert res_preflight_ok.status_code == 200
    assert res_preflight_ok.headers.get("access-control-allow-origin") == allowed_origin
    assert res_preflight_ok.headers.get("access-control-allow-credentials") == "true"

    # 2. Preflight для запрещенного origin отклоняется
    headers_disallowed = {
        "Origin": disallowed_origin,
        "Access-Control-Request-Method": "GET",
    }
    res_preflight_fail = client.options("/api/health", headers=headers_disallowed)
    assert (
        res_preflight_fail.status_code != 200
        or res_preflight_fail.headers.get("access-control-allow-origin") != disallowed_origin
    )
    assert res_preflight_fail.headers.get("access-control-allow-origin") != disallowed_origin

    # 3. Обычный GET-запрос с разрешенным origin
    res_get_ok = client.get("/api/health", headers={"Origin": allowed_origin})
    assert res_get_ok.status_code == 200
    assert res_get_ok.headers.get("access-control-allow-origin") == allowed_origin

    # 4. Обычный GET-запрос с запрещенным origin: ответ возвращается,
    # но CORS-заголовок не выставляется
    res_get_disallowed = client.get("/api/health", headers={"Origin": disallowed_origin})
    assert res_get_disallowed.status_code == 200
    assert "access-control-allow-origin" not in res_get_disallowed.headers


def test_cors_blank_disables_all() -> None:
    """При пустом BACKEND_CORS_ORIGINS (blank enables nothing) любые origins отклоняются."""
    settings = Settings(cors_origins=[], _env_file=None)
    app = create_app(settings)
    client = TestClient(app)

    res_opt = client.options(
        "/api/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in res_opt.headers or res_opt.status_code != 200

    res_get = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in res_get.headers


def test_openapi_and_schemas_export() -> None:
    """Проверка наличия всех согласованных моделей и SSE-схем в openapi.json."""
    app = create_app()
    client = TestClient(app)

    # 1. Корневой /openapi.json
    res_openapi = client.get("/openapi.json")
    assert res_openapi.status_code == 200
    schema = res_openapi.json()
    components = schema.get("components", {}).get("schemas", {})

    missing = [name for name in REQUIRED_SCHEMAS if name not in components]
    assert not missing, f"В openapi.json отсутствуют контракты: {missing}"

    for model_name in REQUIRED_SCHEMAS:
        assert model_name in components

    # Проверка целостности графа $ref (отсутствие битых ссылок)
    import re

    refs = set(re.findall(r"#/components/schemas/([A-Za-z0-9_]+)", res_openapi.text))
    missing_refs = [r for r in refs if r not in components]
    assert not missing_refs, f"Обнаружены висячие $ref в openapi.json: {missing_refs}"

    # 2. Экспорт по префиксу /api/openapi.json
    res_api_openapi = client.get("/api/openapi.json")
    assert res_api_openapi.status_code == 200
    assert res_api_openapi.json()["components"]["schemas"].keys() == components.keys()

    # 3. Эндпоинт реестра схем /api/schemas
    res_schemas = client.get("/api/schemas")
    assert res_schemas.status_code == 200
    schemas_dict = res_schemas.json()
    assert "AssistantMessage" in schemas_dict
    assert "ToolResult" in schemas_dict
    assert "AgentStreamEvent" in schemas_dict
    assert "MessageBlock" in schemas_dict


def test_api_subroutes_stubs() -> None:
    app = create_app()
    client = TestClient(app)

    # CP1 routes require an explicitly enabled local-user adapter.
    res_ds = client.get("/api/datasets")
    assert res_ds.status_code == 503

    res_chats = client.get("/api/chats")
    assert res_chats.status_code == 503

    # Results
    res_res = client.get("/api/results/r_test")
    assert res_res.status_code == 503

    # Dashboards
    assert client.get("/api/dashboards").status_code == 422
    res_dash = client.get("/api/dashboards", params={"dataset_id": "ds_test"})
    assert res_dash.status_code == 200
    assert res_dash.json() == []

    # Auth
    res_auth = client.get("/api/auth/me")
    assert res_auth.status_code == 401
    assert res_auth.json()["error"]["code"] == "unauthorized"
