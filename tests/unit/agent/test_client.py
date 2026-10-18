import json
from unittest.mock import MagicMock

import pytest
from openai import APIError, APITimeoutError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from djedai.agent.client import BIModelClient
from djedai.contracts.agent import Understanding


@pytest.fixture
def client(mocker):
    # Mock OpenAI initialization to prevent real network calls
    mocker.patch("djedai.agent.client.OpenAI")
    client = BIModelClient(api_key="test", base_url="http://test", model_name="test-model")
    client.client.chat.completions.create = MagicMock()
    return client


def create_mock_response(content: str):
    message = ChatCompletionMessage(content=content, role="assistant")
    choice = Choice(finish_reason="stop", index=0, message=message)
    return ChatCompletion(
        id="mock", choices=[choice], created=0, model="test", object="chat.completion"
    )


def test_client_success_valid_json(client):
    valid_json = json.dumps({"intent": "data_overview", "entities": {"tables": [], "columns": []}})
    client.client.chat.completions.create.return_value = create_mock_response(valid_json)

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "success"
    assert response.data.intent == "data_overview"
    assert response.error_type is None


def test_client_invalid_json(client):
    client.client.chat.completions.create.return_value = create_mock_response("{ bad_json: true }")

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "error"
    assert response.error_type == "INVALID_JSON"


def test_client_schema_mismatch(client):
    # Missing required 'intent' field
    missing_intent_json = json.dumps({"entities": {"tables": ["t1"], "columns": []}})
    client.client.chat.completions.create.return_value = create_mock_response(missing_intent_json)

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "error"
    assert response.error_type == "SCHEMA_MISMATCH"
    assert response.raw_response == {"entities": {"tables": ["t1"], "columns": []}}


def test_client_timeout_error(client):
    client.client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "error"
    assert response.error_type == "TIMEOUT"


def test_client_server_error(client):
    client.client.chat.completions.create.side_effect = APIError(
        message="Server down", request=MagicMock(), body=None
    )

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "error"
    assert response.error_type == "SERVER_ERROR"


def test_client_markdown_array_cleanup(client):
    # Simulates Gemma wrapping output in ```json [...] ```
    wrapped_json = '```json\n[{\n  "intent": "dashboard"\n}]\n```'
    client.client.chat.completions.create.return_value = create_mock_response(wrapped_json)

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "success"
    assert response.data.intent == "dashboard"
