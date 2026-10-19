import pytest

from djedai.agent.config import get_agent_client
from djedai.contracts.agent import Understanding


@pytest.mark.integration
@pytest.mark.llm
def test_live_agent_api_call():
    """
    Интеграционный тест: Вызов реальной модели через сконфигурированный клиент.
    Проверяет валидность JSON Schema (Structured Outputs) и соединение с API.
    Для запуска требуется заполненный .env файл с MODEL_BASE_URL, MODEL_NAME, MODEL_API_KEY.
    Команда для ручного запуска:
    uv run pytest -m "integration and llm" tests/integration/agent/test_live_api.py -v -s
    """
    client = get_agent_client()

    assert client.client.api_key, "MODEL_API_KEY не установлен в окружении!"

    response = client.generate_typed_response(
        system_prompt="You are a BI router. Classify the intent into JSON.",
        user_prompt="I want to see a chart showing revenue.",
        response_model=Understanding,
    )

    assert response.status == "success"
    assert response.data is not None
    # Если модель действительно работает, она поймет, что это интент 'visualize' или 'dashboard'
    assert response.data.intent in ["visualize", "data_question", "dashboard"]
