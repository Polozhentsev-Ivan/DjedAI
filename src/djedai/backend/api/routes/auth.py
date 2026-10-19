"""Роутер авторизации и профиля (AUTH-2)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from djedai.contracts.auth import TokenResponse, UserCreate, UserLogin, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=201)
def register(user_in: UserCreate) -> UserRead:
    """Регистрирует нового пользователя (заглушка для AUTH-2)."""
    return UserRead(
        user_id="u_stub",
        username=user_in.username,
        email=user_in.email,
        is_active=True,
        created_at=datetime.now(UTC),
    )


@router.post("/login", response_model=TokenResponse)
def login(credentials: UserLogin) -> TokenResponse:
    """Выполняет аутентификацию пользователя и возвращает токен сессии (заглушка для AUTH-2)."""
    return TokenResponse(
        access_token="stub_token",
        token_type="bearer",
        expires_in=86400,
    )


@router.get("/me", response_model=UserRead)
def get_current_user_stub() -> UserRead:
    """Возвращает информацию о текущем пользователе (заглушка для AUTH-2)."""
    return UserRead(
        user_id="anon",
        username="anonymous",
        email=None,
        is_active=True,
        created_at=datetime.now(UTC),
    )
