import pytest

from djedai.agent.config import get_agent_client


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.test.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model-70b")
    monkeypatch.setenv("MODEL_API_KEY", "sk-secret-key")

    client = get_agent_client()
    assert client.client.base_url == "https://api.test.com/v1/"
    assert client.model == "test-model-70b"
    assert client.client.api_key == "sk-secret-key"


def test_config_missing_keys_raises_error(monkeypatch):
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    # Prevent the developer's local .env file from repopulating the removed keys.
    monkeypatch.setattr("djedai.agent.config.load_dotenv", lambda *args, **kwargs: None)

    with pytest.raises(ValueError) as exc:
        get_agent_client()

    assert "Отсутствует или неверна конфигурация модели" in str(exc.value)
