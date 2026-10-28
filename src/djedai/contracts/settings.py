"""Контракты настройки модели из интерфейса (#209).

Ключ API принимается только на запись: ни один ответ его не содержит, а признак
``api_key_set`` лишь сообщает, что ключ задан.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelSettingsRead(BaseModel):
    """Действующие настройки модели; ключ не возвращается."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str | None = Field(description="Действующий адрес API")
    model_name: str | None = Field(description="Действующая модель")
    allowed_models: list[str] = Field(description="Модели, разрешённые администратором")
    api_key_set: bool = Field(description="Ключ API задан в интерфейсе или в .env")
    api_key_from_ui: bool = Field(description="Ключ задан в интерфейсе, а не в .env")
    editable_connection: bool = Field(
        description="Ключ и адрес API можно менять в интерфейсе (MODEL_SETTINGS_FROM_UI)"
    )
    matches_env: bool = Field(
        description=(
            "Пара «адрес + модель» совпадает с .env. Иначе reasoning, цены и передача "
            "значений ячеек модели отключены: они подтверждены только для пары из .env"
        )
    )


class ModelSettingsUpdate(BaseModel):
    """Изменение настроек; не переданное поле не меняется, пустая строка возвращает .env."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_name: str | None = Field(default=None, max_length=255)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


ModelCheckCode = Literal[
    "ok", "not_configured", "invalid_key", "unreachable", "unknown_model", "model_error"
]


class ModelCheckResult(BaseModel):
    """Итог одного короткого запроса к модели; текст ошибки провайдера не передаётся."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    code: ModelCheckCode
    message: str
