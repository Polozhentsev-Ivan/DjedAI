"""Точка входа и фабрика создания FastAPI-приложения DjedAI."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from djedai.backend.api.router import api_router
from djedai.backend.config import Settings, get_settings
from djedai.contracts.common import HealthCheck


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Управление жизненным циклом приложения (запуск и завершение)."""
    yield


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

    # Настройка CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Подключение всех API маршрутов с префиксом /api
    app.include_router(api_router, prefix=cfg.api_prefix)

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
