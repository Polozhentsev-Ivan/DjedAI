from unittest.mock import MagicMock

from djedai.agent.client import ModelResponse
from djedai.agent.graph import build_agent_graph
from djedai.backend.chat_service import _block_to_message_block
from djedai.contracts.agent import Clarification, Entities, Message, Understanding
from djedai.contracts.message import ClarificationBlock


def run_understand_scenario(
    intent: str, user_query: str, clarification: Clarification | None = None
) -> dict:
    graph = build_agent_graph()
    mock_client = MagicMock()

    # Mock the LLM correctly parsing the negative/ambiguous intent
    mock_client.generate_typed_response.return_value = ModelResponse(
        status="success",
        data=Understanding(intent=intent, entities=Entities(), clarification=clarification),
    )

    config = {"configurable": {"thread_id": f"test_{intent}", "client": mock_client}}
    initial_state = {
        "chat_id": "c1",
        "dataset_id": "ds1",
        "messages": [Message(role="user", content=user_query)],
    }

    return graph.invoke(initial_state, config=config)


def test_clarify_ambiguous_with_options():
    clarification = Clarification(
        question="Какую метрику считать продажами?",
        options=["Сумма заказа (amount)", "Количество заказов"],
    )
    result = run_understand_scenario("ambiguous", "Покажи продажи", clarification)

    assert result["understanding"].intent == "ambiguous"
    assert len(result["answer_blocks"]) == 1

    block = result["answer_blocks"][0]
    assert block.type == "clarification"
    assert "Какую метрику считать продажами?" in block.markdown
    assert block.options == ["Сумма заказа (amount)", "Количество заказов"]
    message_block = _block_to_message_block(block)
    assert message_block == ClarificationBlock(
        question="Какую метрику считать продажами?",
        options=["Сумма заказа (amount)", "Количество заказов"],
    )


def test_clarify_invalid_field():
    result = run_understand_scenario("invalid", "Покажи температуру на марсе")

    assert result["understanding"].intent == "invalid"
    assert len(result["answer_blocks"]) == 1

    block = result["answer_blocks"][0]
    assert block.type == "text"
    assert "не найдены в данных" in block.markdown


def test_clarify_invalid_field_with_options():
    clarification = Clarification(
        question="Поле 'температура' не найдено. Возможно, вы имели в виду:",
        options=["Погода (weather)", "Регион (region)"],
    )
    result = run_understand_scenario("invalid", "Покажи температуру", clarification)

    assert result["understanding"].intent == "invalid"
    assert len(result["answer_blocks"]) == 1

    block = result["answer_blocks"][0]
    assert block.type == "clarification"
    assert "не найдено. Возможно, вы имели в виду:" in block.markdown
    assert block.options == ["Погода (weather)", "Регион (region)"]


def test_clarify_off_topic_capabilities():
    result = run_understand_scenario("off_topic", "Напиши стих")

    assert result["understanding"].intent == "off_topic"
    assert len(result["answer_blocks"]) == 1

    block = result["answer_blocks"][0]
    assert block.type == "text"
    assert "Я — аналитический ИИ-агент" in block.markdown


def test_clarify_empty_query():
    # Empty query shouldn't even call the LLM, it routes directly
    graph = build_agent_graph()
    mock_client = MagicMock()
    config = {"configurable": {"thread_id": "test_empty", "client": mock_client}}

    initial_state = {
        "chat_id": "c1",
        "messages": [Message(role="user", content="   ")],
    }

    result = graph.invoke(initial_state, config=config)

    mock_client.generate_typed_response.assert_not_called()
    assert result["understanding"].intent == "off_topic"
    assert "Я — аналитический ИИ-агент" in result["answer_blocks"][0].markdown
