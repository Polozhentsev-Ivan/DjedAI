import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, Field, ValidationError

from djedai.agent.client import BIModelClient
from djedai.agent.reasoning import ReasoningConfigurationError
from djedai.config import LimitSettings, ReasoningEffort, get_limits


class ModelConfig(BaseModel):
    base_url: str = Field(min_length=1)
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


@dataclass(frozen=True)
class ModelValues:
    """Адрес API, модель и ключ; ``None`` — значение не задано на этом уровне (#209)."""

    base_url: str | None = None
    name: str | None = None
    api_key: str | None = field(default=None, repr=False)

    def over(self, base: "ModelValues") -> "ModelValues":
        """Эти значения поверх ``base``: интерфейс важнее ``.env``."""
        return replace(
            base,
            **{
                name: value
                for name, value in vars(self).items()
                if value is not None and value != ""
            },
        )

    def same_endpoint(self, other: "ModelValues") -> bool:
        """Одна ли пара «адрес + модель»: к ней привязаны reasoning, цены и data sharing."""
        return ((self.base_url or "").rstrip("/"), self.name or "") == (
            (other.base_url or "").rstrip("/"),
            other.name or "",
        )


@contextmanager
def _dotenv_environ() -> Iterator[os._Environ[str]]:
    original_environ = dict(os.environ)
    load_dotenv()
    try:
        yield os.environ
    finally:
        # load_dotenv() mutates the process environment globally; restore it so
        # reading agent config does not leak values (e.g. APP_ENV) to other code.
        os.environ.clear()
        os.environ.update(original_environ)


def env_model_values() -> ModelValues:
    """Значения ``MODEL_BASE_URL``, ``MODEL_NAME`` и ``MODEL_API_KEY`` из окружения и ``.env``."""
    with _dotenv_environ() as env:
        return ModelValues(
            base_url=env.get("MODEL_BASE_URL", "").strip() or None,
            name=env.get("MODEL_NAME", "").strip() or None,
            api_key=env.get("MODEL_API_KEY") or None,
        )


def data_sharing_allowed(override: ModelValues | None = None) -> bool:
    """Можно ли отправлять модели значения ячеек: только при ``ALLOW_DATA_SHARING=true``.

    По умолчанию нельзя: ``MODEL_BASE_URL`` может указывать на внешний API. Значение
    берётся из окружения, затем из ``.env``; окружение процесса не меняется. Разрешение
    относится к паре «адрес + модель» из ``.env``: для другой пары из ``override`` нельзя.
    """
    value = os.environ.get("ALLOW_DATA_SHARING")
    if value is None:
        value = dotenv_values().get("ALLOW_DATA_SHARING")
    if (value or "false").strip().lower() != "true":
        return False
    if override is None:
        return True
    env = env_model_values()
    return override.over(env).same_endpoint(env)


def reasoning_profile(limits: LimitSettings | None = None) -> dict[str, ReasoningEffort]:
    """Возвращает проверенный режим reasoning для каждой стадии агента."""
    limits = limits if limits is not None else get_limits()
    return {
        "understand": limits.model_reasoning_understand,
        "plan": limits.model_reasoning_plan,
        "visualize": limits.model_reasoning_visualize,
        "compose": limits.model_reasoning_compose,
        "verify": limits.model_reasoning_verify,
    }


def get_agent_client(override: ModelValues | None = None) -> BIModelClient:
    """Загружает конфигурацию из .env и окружения, инициализирует и возвращает клиент.

    ``override`` — значения из интерфейса поверх ``.env`` (#209). ``MODEL_REASONING_*``
    подтверждены только для пары «адрес + модель» из ``.env``; для другой пары
    reasoning выключается, и все стадии остаются в ``provider_default``.
    """
    try:
        env = env_model_values()
        values = override.over(env) if override is not None else env
        config = ModelConfig(
            base_url=values.base_url or "",
            name=values.name or "",
            api_key=values.api_key or "",
        )
        limits = get_limits()
        confirmed = values.same_endpoint(env)
        return BIModelClient(
            api_key=config.api_key,
            base_url=config.base_url,
            model_name=config.name,
            timeout=limits.model_timeout_seconds,
            max_output_tokens=limits.model_max_output_tokens,
            max_retries=limits.model_max_retries,
            retry_delay=limits.model_retry_delay_seconds,
            reasoning=reasoning_profile(limits) if confirmed else None,
            reasoning_supported=limits.model_reasoning_supported and confirmed,
        )
    except ValidationError as e:
        if any(
            str(item["loc"][0]).upper().startswith("MODEL_REASONING")
            for item in e.errors()
            if item["loc"]
        ):
            raise ReasoningConfigurationError("invalid_effort") from e
        raise ValueError(f"Отсутствует или неверна конфигурация модели: {e}") from e
