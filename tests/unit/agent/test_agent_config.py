"""Проверки конфигурации API-клиента агента."""

import pytest

from djedai.agent.config import get_agent_client, reasoning_profile
from djedai.config import get_limits


@pytest.fixture(autouse=True)
def clear_limits_cache(monkeypatch):
    defaults = {
        "MODEL_REASONING_UNDERSTAND": "provider_default",
        "MODEL_REASONING_PLAN": "provider_default",
        "MODEL_REASONING_VISUALIZE": "provider_default",
        "MODEL_REASONING_COMPOSE": "provider_default",
        "MODEL_REASONING_VERIFY": "provider_default",
        "MODEL_REASONING_SUPPORTED": "false",
    }
    for name, value in defaults.items():
        monkeypatch.setenv(name, value)
    get_limits.cache_clear()
    yield
    get_limits.cache_clear()


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.test.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model-70b")
    monkeypatch.setenv("MODEL_API_KEY", "sk-secret-key")

    client = get_agent_client()
    assert client.client.base_url == "https://api.test.com/v1/"
    assert client.model == "test-model-70b"
    assert client.client.api_key == "sk-secret-key"
    assert client.reasoning == {
        "understand": "provider_default",
        "plan": "provider_default",
        "visualize": "provider_default",
        "compose": "provider_default",
        "verify": "provider_default",
    }


def test_reasoning_is_loaded_per_stage(monkeypatch):
    get_limits.cache_clear()
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.test.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model-70b")
    monkeypatch.setenv("MODEL_API_KEY", "sk-secret-key")
    monkeypatch.setenv("MODEL_REASONING_UNDERSTAND", "none")
    monkeypatch.setenv("MODEL_REASONING_PLAN", "low")
    monkeypatch.setenv("MODEL_REASONING_COMPOSE", "high")
    monkeypatch.setenv("MODEL_REASONING_SUPPORTED", "true")
    try:
        assert reasoning_profile() == {
            "understand": "none",
            "plan": "low",
            "visualize": "provider_default",
            "compose": "high",
            "verify": "provider_default",
        }
        assert get_agent_client().reasoning_for("compose") == "high"
    finally:
        get_limits.cache_clear()


def test_unsupported_reasoning_fails_instead_of_being_ignored(monkeypatch):
    get_limits.cache_clear()
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.test.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model-70b")
    monkeypatch.setenv("MODEL_API_KEY", "sk-secret-key")
    monkeypatch.setenv("MODEL_REASONING_PLAN", "high")
    monkeypatch.setenv("MODEL_REASONING_SUPPORTED", "false")
    try:
        with pytest.raises(ValueError, match="MODEL_REASONING_SUPPORTED=false"):
            get_agent_client()
    finally:
        get_limits.cache_clear()


def test_non_default_reasoning_requires_explicit_support(monkeypatch):
    get_limits.cache_clear()
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.test.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model-70b")
    monkeypatch.setenv("MODEL_API_KEY", "sk-secret-key")
    monkeypatch.setenv("MODEL_REASONING_UNDERSTAND", "none")
    monkeypatch.setenv("MODEL_REASONING_SUPPORTED", "false")
    try:
        with pytest.raises(ValueError, match="MODEL_REASONING_SUPPORTED=false"):
            get_agent_client()
    finally:
        get_limits.cache_clear()


def test_config_missing_keys_raises_error(monkeypatch):
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    # Prevent the developer's local .env file from repopulating the removed keys.
    monkeypatch.setattr("djedai.agent.config.load_dotenv", lambda *args, **kwargs: None)

    with pytest.raises(ValueError) as exc:
        get_agent_client()

    assert "Отсутствует или неверна конфигурация модели" in str(exc.value)
