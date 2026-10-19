import pytest

from djedai.agent.config import get_agent_client
from djedai.agent.graph import build_agent_graph
from djedai.contracts.agent import Message


@pytest.mark.integration
@pytest.mark.llm
def test_live_agent_api_call():
    """
    Интеграционный тест: Вызов реальной модели через граф.
    Проверяет валидность JSON Schema и прохождение стейта через узлы.
    Команда: uv run pytest -m "integration and llm" tests/integration/test_live_api.py -v -s
    """
    client = get_agent_client()
    assert client.client.api_key, "MODEL_API_KEY не установлен в .env!"

    graph = build_agent_graph()
    config = {"configurable": {"thread_id": "live_test", "client": client}}

    initial_state = {
        "chat_id": "live_test_chat",
        "dataset_id": "ds_1",
        "messages": [Message(role="user", content="I want to see a chart showing revenue.")],
        "catalog_prompt": "Table: sales, columns: id, revenue, date.",
    }

    result = graph.invoke(initial_state, config=config)

    assert result["understanding"].intent in [
        "visualize",
        "data_question",
        "dashboard",
        "ambiguous",
    ]

    # Убеждаемся, что блоки ответа сформированы (либо ошибка, либо реальный ответ)
    assert len(result["answer_blocks"]) >= 1
