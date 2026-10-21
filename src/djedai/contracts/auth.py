"""Контракты сущностей пользователей, входа и сессий (AUTH)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator


def _utf8(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Поле должно содержать корректный текст UTF-8") from None
    return value


Utf8Text = Annotated[str, AfterValidator(_utf8)]


class UserBase(BaseModel):
    """Базовые атрибуты пользователя."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: Utf8Text = Field(min_length=3, max_length=50, description="Логин пользователя")
    email: Utf8Text | None = Field(default=None, max_length=255, description="Email пользователя")

    @field_validator("username")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Логин не должен состоять из пробелов")
        return value


class UserCreate(UserBase):
    """Данные для регистрации нового пользователя."""

    password: Utf8Text = Field(
        min_length=6,
        max_length=1024,
        repr=False,
        json_schema_extra={"writeOnly": True},
        description="Пароль пользователя",
    )


class UserLogin(BaseModel):
    """Данные для аутентификации (входа)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: Utf8Text = Field(min_length=3, max_length=50, description="Логин пользователя")
    password: Utf8Text = Field(
        min_length=6,
        max_length=1024,
        repr=False,
        json_schema_extra={"writeOnly": True},
        description="Пароль пользователя",
    )

    @field_validator("username")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Логин не должен состоять из пробелов")
        return value


class UserRead(UserBase):
    """Данные пользователя для чтения через API (без хешей паролей)."""

    user_id: str = Field(description="Уникальный идентификатор пользователя")
    is_active: bool = Field(default=True, description="Активен ли аккаунт")
    created_at: AwareDatetime = Field(description="Время регистрации")


class TokenResponse(BaseModel):
    """Ответ API при успешной аутентификации."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str = Field(
        min_length=1, repr=False, description="Токен авторизации / идентификатор сессии"
    )
    token_type: Literal["bearer"] = Field(default="bearer", description="Тип токена")
    expires_in: int = Field(gt=0, description="Время жизни токена в секундах")
