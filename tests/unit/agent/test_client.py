import json
from unittest.mock import MagicMock

import pytest
from openai import APIError, APITimeoutError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from pydantic import RootModel

from djedai.agent.client import BIModelClient
from djedai.contracts.agent import PlannedCall, Understanding


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


def test_client_records_usage_and_applies_output_cap(client):
    raw = create_mock_response('{"intent":"data_question"}').model_dump()
    raw["usage"] = {"prompt_tokens": 123, "completion_tokens": 15, "total_tokens": 138}
    client.client.chat.completions.create.return_value = ChatCompletion.model_validate(raw)
    client.max_output_tokens = 512
    response = client.generate_typed_response("sys", "user", Understanding)
    assert response.status == "success"
    assert response.prompt_tokens == 123
    assert response.completion_tokens == 15
    assert response.finish_reason == "stop"
    assert client.client.chat.completions.create.call_args.kwargs["max_tokens"] == 512


def test_client_does_not_accept_a_token_limited_response(client):
    raw = create_mock_response('{"intent":"data_question"}').model_dump()
    raw["choices"][0]["finish_reason"] = "length"
    client.client.chat.completions.create.return_value = ChatCompletion.model_validate(raw)
    response = client.generate_typed_response("sys", "user", Understanding)
    assert response.status == "error"
    assert response.error_type == "OUTPUT_LIMIT"


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


def test_client_schema_mismatch_with_primitive(client):
    # Verifies that sending primitive JSON (like an int) does not crash the client
    # with a secondary ValidationError for the raw_response field.
    client.client.chat.completions.create.return_value = create_mock_response("42")

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "error"
    assert response.error_type == "SCHEMA_MISMATCH"
    assert response.raw_response == 42


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


'''def test_client_markdown_array_cleanup(client):
    # Simulates Gemma wrapping output in ```json [...] ```
    wrapped_json = '```json\n[{\n  "intent": "dashboard"\n}]\n```'
    client.client.chat.completions.create.return_value = create_mock_response(wrapped_json)

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "success"
    assert response.data.intent == "dashboard"'''


def test_client_list_parsing(client):
    # Verifies that top-level arrays are not destructively stripped
    valid_list_json = json.dumps([{"tool": "overview", "args": {}}])
    client.client.chat.completions.create.return_value = create_mock_response(valid_list_json)

    response = client.generate_typed_response("sys", "user", RootModel[list[PlannedCall]])

    assert response.status == "success"
    assert len(response.data.root) == 1
    assert response.data.root[0].tool == "overview"
