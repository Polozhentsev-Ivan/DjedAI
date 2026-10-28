"""Настройка модели из интерфейса (#209): модель, а при разрешении — ключ и адрес API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from djedai.backend.auth.dependencies import current_owner_id
from djedai.backend.model_settings import ModelSettingsService, model_settings_for
from djedai.contracts.settings import ModelCheckResult, ModelSettingsRead, ModelSettingsUpdate

router = APIRouter(prefix="/settings/model", tags=["settings"])


def get_model_settings(request: Request) -> ModelSettingsService:
    return model_settings_for(request.app)


ModelSettingsDep = Annotated[ModelSettingsService, Depends(get_model_settings)]
UserDep = Annotated[str, Depends(current_owner_id)]


@router.get("", response_model=ModelSettingsRead)
def read_model_settings(_user_id: UserDep, service: ModelSettingsDep) -> ModelSettingsRead:
    """Действующие настройки; ключ не возвращается, только признак ``api_key_set``."""
    return service.view()


@router.put("", response_model=ModelSettingsRead)
def update_model_settings(
    payload: ModelSettingsUpdate, user_id: UserDep, service: ModelSettingsDep
) -> ModelSettingsRead:
    """Меняет модель, а при ``MODEL_SETTINGS_FROM_UI=true`` ещё ключ и адрес (иначе 403)."""
    return service.update(payload, user_id)


@router.post("/check", response_model=ModelCheckResult)
def check_model_settings(
    payload: ModelSettingsUpdate, _user_id: UserDep, service: ModelSettingsDep
) -> ModelCheckResult:
    """Один короткий запрос к модели с введёнными значениями; ничего не сохраняет."""
    return service.check(payload)
