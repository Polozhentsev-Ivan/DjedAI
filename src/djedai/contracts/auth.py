"""Контракты сущностей пользователей, входа и сессий (AUTH)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class UserBase(BaseModel):
    """Базовые атрибуты пользователя."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(min_length=3, max_length=50, description="Логин пользователя")
    email: str | None = Field(default=None, max_length=255, description="Email пользователя")

    @field_validator("username")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Логин не должен состоять из пробелов")
        return value


class UserCreate(UserBase):
    """Данные для регистрации нового пользователя."""

    password: str = Field(min_length=6, max_length=1024, description="Пароль пользователя")


class UserLogin(BaseModel):
    """Данные для аутентификации (входа)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(description="Логин пользователя")
    password: str = Field(description="Пароль пользователя")


class UserRead(UserBase):
    """Данные пользователя для чтения через API (без хешей паролей)."""

    user_id: str = Field(description="Уникальный идентификатор пользователя")
    is_active: bool = Field(default=True, description="Активен ли аккаунт")
    created_at: AwareDatetime = Field(description="Время регистрации")


class TokenResponse(BaseModel):
    """Ответ API при успешной аутентификации."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str = Field(min_length=1, description="Токен авторизации / идентификатор сессии")
    token_type: Literal["bearer"] = Field(default="bearer", description="Тип токена")
    expires_in: int = Field(gt=0, description="Время жизни токена в секундах")
