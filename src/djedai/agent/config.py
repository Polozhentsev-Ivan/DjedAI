import os

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, Field, ValidationError

from djedai.agent.client import BIModelClient
from djedai.agent.reasoning import ReasoningConfigurationError
from djedai.config import LimitSettings, ReasoningEffort, get_limits


class ModelConfig(BaseModel):
    base_url: str = Field(min_length=1)
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


def data_sharing_allowed() -> bool:
    """Можно ли отправлять модели значения ячеек: только при ``ALLOW_DATA_SHARING=true``.

    По умолчанию нельзя: ``MODEL_BASE_URL`` может указывать на внешний API. Значение
    берётся из окружения, затем из ``.env``; окружение процесса не меняется.
    """
    value = os.environ.get("ALLOW_DATA_SHARING")
    if value is None:
        value = dotenv_values().get("ALLOW_DATA_SHARING")
    return (value or "false").strip().lower() == "true"


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


def get_agent_client() -> BIModelClient:
    """Загружает конфигурацию из .env и окружения, инициализирует и возвращает клиент."""
    original_environ = dict(os.environ)
    load_dotenv()
    try:
        config = ModelConfig(
            base_url=os.environ.get("MODEL_BASE_URL", ""),
            name=os.environ.get("MODEL_NAME", ""),
            api_key=os.environ.get("MODEL_API_KEY", ""),
        )
        limits = get_limits()
        return BIModelClient(
            api_key=config.api_key,
            base_url=config.base_url,
            model_name=config.name,
            timeout=limits.model_timeout_seconds,
            max_output_tokens=limits.model_max_output_tokens,
            max_retries=limits.model_max_retries,
            retry_delay=limits.model_retry_delay_seconds,
            reasoning=reasoning_profile(limits),
            reasoning_supported=limits.model_reasoning_supported,
        )
    except ValidationError as e:
        if any(
            str(item["loc"][0]).upper().startswith("MODEL_REASONING")
            for item in e.errors()
            if item["loc"]
        ):
            raise ReasoningConfigurationError("invalid_effort") from e
        raise ValueError(f"Отсутствует или неверна конфигурация модели: {e}") from e
    finally:
        # load_dotenv() mutates the process environment globally; restore it so
        # reading agent config does not leak values (e.g. APP_ENV) to other code.
        os.environ.clear()
        os.environ.update(original_environ)
