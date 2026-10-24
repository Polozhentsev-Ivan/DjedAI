"""Точка входа и фабрика создания FastAPI-приложения DjedAI."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from pydantic.json_schema import models_json_schema
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException

from djedai.agent.config import get_agent_client
from djedai.backend.api.router import api_router
from djedai.backend.auth.policy import AUTH_CACHE_HEADERS, AuthPolicyMiddleware, AuthRateLimiter
from djedai.backend.config import Settings, get_settings
from djedai.backend.db.session import create_db_engine
from djedai.backend.diagnostics import DiagnosticJournal
from djedai.contracts import (
    AgentStreamEvent,
    AssistantMessage,
    ChartSpec,
    ChatCreate,
    ChatCreated,
    ChatDetail,
    ChatMessage,
    ChatSummary,
    Dashboard,
    DashboardSnapshot,
    DatasetCatalog,
    DatasetDetail,
    DatasetStreamEvent,
    DatasetSummary,
    ErrorResponse,
    HealthCheck,
    MessageBlock,
    SendMessageRequest,
    TokenResponse,
    ToolError,
    ToolResult,
    ToolResultData,
    UserCreate,
    UserLogin,
    UserRead,
)

# Корневые модели контрактов для включения в OpenAPI / components.schemas
CONTRACT_ROOT_MODELS = [
    DatasetCatalog,
    DatasetDetail,
    ToolResult,
    ToolResultData,
    ChartSpec,
    AssistantMessage,
    Dashboard,
    DashboardSnapshot,
    UserCreate,
    UserLogin,
    UserRead,
    TokenResponse,
    DatasetSummary,
    ErrorResponse,
    HealthCheck,
    ChatCreate,
    ChatCreated,
    ChatSummary,
    ChatMessage,
    ChatDetail,
    SendMessageRequest,
]

# Дискриминированные объединения (unions) для схемы
CONTRACT_UNION_SCHEMAS: dict[str, Any] = {
    "MessageBlock": MessageBlock,
    "AgentStreamEvent": AgentStreamEvent,
    "DatasetStreamEvent": DatasetStreamEvent,
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Управление жизненным циклом приложения (запуск и завершение)."""
    from djedai.backend.dataset_service import DatasetService

    cfg = app.state.settings
    app.state.dataset_service = DatasetService(
        app.state.session_factory,
        cfg.datasets_data_dir,
        source_encryption_key=cfg.source_encryption_key.get_secret_value()
        if cfg.source_encryption_key
        else None,
        description_client=get_agent_client if cfg.catalog_descriptions else None,
    )
    try:
        await app.state.dataset_service.recover_interrupted()
        yield
    finally:
        await app.state.dataset_service.close()
        app.state.engine.dispose()


def get_custom_openapi(app: FastAPI) -> dict[str, Any]:
    """Генерирует полную схему OpenAPI, гарантируя экспорт всех общих контрактов и SSE-схем."""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )

    schemas = openapi_schema.setdefault("components", {}).setdefault("schemas", {})

    # Генерация JSON Schema для всех корневых моделей и связанных подмоделей
    _, top_defs = models_json_schema(
        [(m, "serialization") for m in CONTRACT_ROOT_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    for name, schema in top_defs.get("$defs", {}).items():
        schemas[name] = schema

    # Генерация схем для дискриминированных объединений
    # (MessageBlock, AgentStreamEvent, DatasetStreamEvent)
    for name, union_type in CONTRACT_UNION_SCHEMAS.items():
        ta = TypeAdapter(union_type)
        u_schema = ta.json_schema(ref_template="#/components/schemas/{model}")
        u_defs = u_schema.pop("$defs", {})
        for def_name, def_schema in u_defs.items():
            if def_name not in schemas:
                schemas[def_name] = def_schema
        schemas[name] = u_schema

    error_response = {
        "description": "Ошибка запроса",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }
    for path in openapi_schema.get("paths", {}).values():
        for method, operation in path.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            responses = operation.setdefault("responses", {})
            responses.setdefault("default", error_response)
            if "422" in responses:
                responses["422"] = error_response

    app.openapi_schema = openapi_schema
    return app.openapi_schema


def create_app(settings: Settings | None = None) -> FastAPI:
    """Создает и настраивает экземпляр FastAPI-приложения."""
    cfg = settings or get_settings()

    app = FastAPI(
        title="DjedAI API",
        description="Локальная аналитическая платформа: вопросы к данным на русском языке",
        version=cfg.app_version,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.diagnostics = DiagnosticJournal(
        cfg.diagnostics_dir, cfg.diagnostics_max_bytes, cfg.diagnostics_backups
    )
    app.state.engine = create_db_engine(cfg.app_database_url)
    app.state.session_factory = sessionmaker(
        bind=app.state.engine, autocommit=False, autoflush=False
    )

    app.state.auth_limiter = AuthRateLimiter(
        cfg.auth_rate_limit_requests, cfg.auth_rate_limit_window_seconds
    )
    app.add_middleware(AuthPolicyMiddleware, prefix=cfg.api_prefix, limiter=app.state.auth_limiter)

    # Настройка CORS с явным dev allowlist
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Подключение всех API маршрутов с префиксом /api
    app.include_router(api_router, prefix=cfg.api_prefix)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        codes = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            429: "rate_limit",
            503: "unavailable",
        }
        payload = ErrorResponse(
            error=ToolError(
                code=codes.get(error.status_code, f"http_{error.status_code}"),
                message=str(error.detail)
                if error.status_code < 500
                else "Сервис временно недоступен",
            )
        )
        return JSONResponse(
            payload.model_dump(mode="json"), status_code=error.status_code, headers=error.headers
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        # ValidationError.errors() includes input values, including passwords and DSNs.
        payload = ErrorResponse(
            error=ToolError(
                code="invalid_request",
                message="Проверьте поля запроса",
                suggestion="Параметры запроса должны соответствовать схеме API",
            )
        )
        return JSONResponse(payload.model_dump(mode="json"), status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, _error: Exception) -> JSONResponse:
        payload = ErrorResponse(
            error=ToolError(
                code="internal_error",
                message="Внутренняя ошибка сервера",
            )
        )
        return JSONResponse(
            payload.model_dump(mode="json"),
            status_code=500,
            headers=AUTH_CACHE_HEADERS
            if _request.url.path.startswith(cfg.api_prefix + "/")
            else None,
        )

    # Кастомный генератор схемы OpenAPI с полным набором общих контрактов
    app.openapi = lambda: get_custom_openapi(app)

    # Экспорт схемы OpenAPI по префиксу API (/api/openapi.json)
    @app.get(f"{cfg.api_prefix}/openapi.json", include_in_schema=False)
    def api_openapi() -> dict[str, Any]:
        """Возвращает схему OpenAPI по префиксу API для frontend/клиентов."""
        return app.openapi()

    # Экспорт реестра общих схем в формате JSON Schema (/api/schemas)
    @app.get(f"{cfg.api_prefix}/schemas", include_in_schema=False)
    def api_schemas() -> dict[str, Any]:
        """Возвращает словарь всех зарегистрированных JSON-схем контрактов."""
        return app.openapi().get("components", {}).get("schemas", {})

    # Корневой health-check для Docker / load-balancer без префикса
    @app.get("/health", response_model=HealthCheck, include_in_schema=False)
    def root_health() -> HealthCheck:
        return HealthCheck(
            status="ok",
            app_env=cfg.app_env,
            version=cfg.app_version,
        )

    return app


# Глобальный экземпляр для uvicorn
app = create_app()
