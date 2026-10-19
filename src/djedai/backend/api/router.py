"""Сводный роутер API DjedAI."""

from __future__ import annotations

from fastapi import APIRouter

from djedai.backend.api.routes import auth, chats, dashboards, datasets, health, results

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(datasets.router)
api_router.include_router(chats.router)
api_router.include_router(results.router)
api_router.include_router(dashboards.router)
api_router.include_router(auth.router)
