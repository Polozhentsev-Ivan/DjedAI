import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from djedai.agent.client import BIModelClient
from djedai.config import get_limits


class ModelConfig(BaseModel):
    base_url: str = Field(min_length=1)
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


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
        )
    except ValidationError as e:
        raise ValueError(f"Отсутствует или неверна конфигурация модели: {e}") from e
    finally:
        # load_dotenv() mutates the process environment globally; restore it so
        # reading agent config does not leak values (e.g. APP_ENV) to other code.
        os.environ.clear()
        os.environ.update(original_environ)
