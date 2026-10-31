"""Модель, адрес API и ключ из интерфейса поверх .env (#209).

Настройки общие на установку. Модель из ``MODEL_ALLOWED_NAMES`` выбирает любой вошедший
пользователь. Ключ и адрес API меняются только при ``MODEL_SETTINGS_FROM_UI=true``: на
сервере ключ не должен идти через браузер, а смена адреса отправила бы данные на
произвольный хост. Если флаг выключили позже, сохранённые ключ и адрес не применяются.

Значения читаются на каждый ход, поэтому изменение действует со следующего хода без
перезапуска. Ключ хранится зашифрованным ``SOURCE_ENCRYPTION_KEY`` и не попадает
ни в ответы, ни в логи.

Оператор может назначить отдельный ключ в ``user_model_keys``. Он имеет приоритет
для владельца, привязан шифрованием к его ID и применяется только к адресу из .env.
Публичный API не позволяет назначать, получать или удалять такие ключи.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from djedai.agent.client import valid_api_key, valid_base_url
from djedai.agent.config import ModelValues, env_model_values, get_agent_client
from djedai.agent.diagnostics import Tariff
from djedai.agent.transport import attempt_lease
from djedai.backend.config import Settings
from djedai.backend.db.models import ModelSettingsModel
from djedai.backend.source_secrets import SourceSecretError, SourceSecrets
from djedai.backend.user_model_keys import UserModelKeys
from djedai.contracts.settings import (
    ModelCheckCode,
    ModelCheckResult,
    ModelSettingsRead,
    ModelSettingsUpdate,
)
from djedai.execution import ExecutionBudget, TurnTimeout

logger = logging.getLogger(__name__)

SETTINGS_ID = "default"
CHECK_TIMEOUT_SECONDS = 10.0
CHECK_MESSAGES: dict[ModelCheckCode, str] = {
    "ok": "Модель ответила",
    "not_configured": "Задайте адрес API, модель и ключ",
    "invalid_key": "Ключ API не принят. Проверьте ключ",
    "unreachable": "Адрес API недоступен. Проверьте адрес и сеть",
    "unknown_model": "Модель не найдена по этому адресу API",
    "model_error": "Модель вернула ошибку. Попробуйте позже",
}


@dataclass(frozen=True)
class _Stored:
    base_url: str | None = None
    model_name: str | None = None
    api_key_ciphertext: str | None = field(default=None, repr=False)


class ModelSettingsService:
    def __init__(
        self, session_factory: sessionmaker[Session], settings: Settings, user_id: str | None = None
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        key = settings.source_encryption_key
        self.secrets = SourceSecrets(key.get_secret_value() if key else None)
        self.user_id = user_id
        # One immutable credential snapshot per request/job. Never combine a newly
        # assigned operator key with a URL accepted before that assignment.
        self.assigned_ciphertext = UserModelKeys(session_factory, self.secrets).ciphertext(user_id)

    @property
    def editable_connection(self) -> bool:
        return self.settings.model_settings_from_ui and self.assigned_ciphertext is None

    def _stored(self) -> _Stored:
        """Только те сохранённые значения, которые конфигурация разрешает применить."""
        try:
            with self.session_factory() as db:
                row = db.get(ModelSettingsModel, SETTINGS_ID)
                if row is None:
                    return _Stored()
                stored = _Stored(row.base_url, row.model_name, row.api_key_ciphertext)
        except SQLAlchemyError:
            logger.error("Model settings storage unavailable")
            raise HTTPException(503, "Хранилище настроек модели недоступно") from None
        name = stored.model_name
        if not self.editable_connection and name not in self.settings.model_allowed_names:
            name = None  # The administrator removed it from the list: .env applies.
        if not self.editable_connection:
            return _Stored(model_name=name)
        return _Stored(stored.base_url, name, stored.api_key_ciphertext)

    def override(self) -> ModelValues | None:
        """Значения из интерфейса для хода; ``None`` — работает только ``.env``."""
        stored = self._stored()
        api_key = (
            self.secrets.decrypt_model_key(stored.api_key_ciphertext)
            if stored.api_key_ciphertext
            else None
        )
        if self.assigned_ciphertext is not None:
            assert self.user_id is not None
            api_key = self.secrets.decrypt_user_model_key(self.user_id, self.assigned_ciphertext)
            # Operator credentials may only reach the operator's configured endpoint.
            return ModelValues(env_model_values().base_url, stored.model_name, api_key)
        values = ModelValues(stored.base_url, stored.model_name, api_key)
        return None if values == ModelValues() else values

    def tariff(self, override: ModelValues | None) -> Tariff | None:
        """Цены из ``.env`` относятся только к паре «адрес + модель» из ``.env``."""
        cfg = self.settings
        if (
            cfg.model_input_price_usd is None
            or cfg.model_output_price_usd is None
            or cfg.model_price_reference is None
        ):
            return None
        if override is not None:
            env = env_model_values()
            if not override.over(env).same_endpoint(env):
                return None
        return Tariff(
            cfg.model_input_price_usd, cfg.model_output_price_usd, cfg.model_price_reference
        )

    def view(self) -> ModelSettingsRead:
        stored = self._stored()
        env = env_model_values()
        current = ModelValues(stored.base_url, stored.model_name).over(env)
        return ModelSettingsRead(
            base_url=current.base_url,
            model_name=current.name,
            allowed_models=list(self.settings.model_allowed_names),
            api_key_set=bool(self.assigned_ciphertext or stored.api_key_ciphertext or env.api_key),
            api_key_from_ui=bool(stored.api_key_ciphertext),
            editable_connection=self.editable_connection,
            matches_env=current.same_endpoint(env),
        )

    def _validated(self, change: ModelSettingsUpdate) -> ModelValues:
        """Проверенные значения запроса; пустая строка означает «вернуть .env»."""
        if not self.editable_connection and (
            change.base_url is not None or change.api_key is not None
        ):
            raise HTTPException(403, "Ключ и адрес API задаёт администратор в .env")
        name = change.model_name.strip() if change.model_name is not None else None
        if name and not self.editable_connection and name not in self.settings.model_allowed_names:
            raise HTTPException(422, "Модель не входит в список разрешённых администратором")
        base_url = change.base_url.strip() if change.base_url is not None else None
        if base_url and not valid_base_url(base_url):
            raise HTTPException(422, "Адрес API должен быть полным HTTP(S)-адресом")
        api_key = change.api_key.get_secret_value() if change.api_key is not None else None
        if api_key and not valid_api_key(api_key):
            raise HTTPException(422, "Ключ API не должен содержать переносы строк")
        return ModelValues(base_url, name, api_key)

    def update(self, change: ModelSettingsUpdate, user_id: str) -> ModelSettingsRead:
        values = self._validated(change)
        ciphertext: str | None = None
        if values.api_key:
            try:
                ciphertext = self.secrets.encrypt_model_key(values.api_key)
            except SourceSecretError:
                raise HTTPException(
                    409, "Чтобы сохранить ключ, задайте SOURCE_ENCRYPTION_KEY в .env"
                ) from None
        try:
            with self.session_factory() as db:
                row = db.get(ModelSettingsModel, SETTINGS_ID)
                if row is None:
                    row = ModelSettingsModel(id=SETTINGS_ID)
                    db.add(row)
                if values.name is not None:
                    row.model_name = values.name or None
                if values.base_url is not None:
                    row.base_url = values.base_url or None
                if values.api_key is not None:
                    row.api_key_ciphertext = ciphertext
                row.updated_by = user_id
                db.commit()
        except SQLAlchemyError:
            logger.error("Model settings storage unavailable")
            raise HTTPException(503, "Хранилище настроек модели недоступно") from None
        logger.info(
            "Model settings updated (model_changed=%s, base_url_changed=%s, api_key_changed=%s)",
            values.name is not None,
            values.base_url is not None,
            values.api_key is not None,
        )
        return self.view()

    def check(self, change: ModelSettingsUpdate) -> ModelCheckResult:
        """Один короткий запрос к модели с введёнными значениями поверх сохранённых."""
        budget = ExecutionBudget(CHECK_TIMEOUT_SECONDS)
        values = self._validated(change)
        stored = self._stored()
        saved_key = None
        if self.assigned_ciphertext is not None:
            try:
                assert self.user_id is not None
                saved_key = self.secrets.decrypt_user_model_key(
                    self.user_id, self.assigned_ciphertext
                )
            except SourceSecretError:
                return _check_result("not_configured")
        elif values.api_key is None and stored.api_key_ciphertext:
            try:
                saved_key = self.secrets.decrypt_model_key(stored.api_key_ciphertext)
            except SourceSecretError:
                return _check_result("not_configured")
        saved = ModelValues(stored.base_url, stored.model_name, saved_key)
        # An explicit empty value means ".env" here too, not "keep the saved value".
        merged = ModelValues(
            *(
                ("" if new == "" else new if new is not None else old)
                for new, old in (
                    (values.base_url, saved.base_url),
                    (values.name, saved.name),
                    (values.api_key, saved.api_key),
                )
            )
        )
        try:
            client = get_agent_client(merged)
        except ValueError:
            return _check_result("not_configured")
        try:
            # Socket timeouts alone do not stop a peer that keeps dripping bytes.
            # The shared transport cancels the entire in-flight buffered request.
            with budget.activate(), attempt_lease(budget, budget.remaining, read_error_body=True):
                client.client.with_options(
                    timeout=CHECK_TIMEOUT_SECONDS, max_retries=0
                ).chat.completions.create(
                    model=client.model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
        except (AuthenticationError, PermissionDeniedError):
            return _check_result("invalid_key")
        except NotFoundError:
            return _check_result("unknown_model")
        except (APIConnectionError, APITimeoutError, TurnTimeout):
            return _check_result("unreachable")
        except APIStatusError as error:
            # Some providers answer 400/422 for an unknown model; the body is never echoed.
            if error.status_code in {400, 422} and "model" in str(error.body).lower():
                return _check_result("unknown_model")
            return _check_result("model_error")
        except Exception as error:  # The provider text may echo request details.
            logger.warning("Model check failed (error_type=%s)", type(error).__name__)
            return _check_result("model_error")
        finally:
            with suppress(Exception):
                client.client.close()
        return _check_result("ok")


def model_settings_for(app: FastAPI, user_id: str | None = None) -> ModelSettingsService:
    """Сервис поверх текущих хранилища и настроек приложения; создание дешёвое."""
    return ModelSettingsService(app.state.session_factory, app.state.settings, user_id)


def _check_result(code: ModelCheckCode) -> ModelCheckResult:
    return ModelCheckResult(ok=code == "ok", code=code, message=CHECK_MESSAGES[code])
