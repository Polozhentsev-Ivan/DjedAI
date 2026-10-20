"""Точка входа и фабрика создания FastAPI-приложения DjedAI."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import TypeAdapter
from pydantic.json_schema import models_json_schema

from djedai.backend.api.router import api_router
from djedai.backend.config import Settings, get_settings
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
    DatasetCatalog,
    DatasetStreamEvent,
    DatasetSummary,
    ErrorResponse,
    HealthCheck,
    MessageBlock,
    SendMessageRequest,
    TokenResponse,
    ToolResult,
    ToolResultData,
    UserCreate,
    UserLogin,
    UserRead,
)

# Корневые модели контрактов для включения в OpenAPI / components.schemas
CONTRACT_ROOT_MODELS = [
    DatasetCatalog,
    ToolResult,
    ToolResultData,
    ChartSpec,
    AssistantMessage,
    Dashboard,
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
    yield


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
