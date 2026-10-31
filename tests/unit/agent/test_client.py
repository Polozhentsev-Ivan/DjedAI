import json
from unittest.mock import MagicMock

import pytest
from openai import APIError, APITimeoutError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from pydantic import RootModel

from djedai.agent.client import SERVER_ERROR_MESSAGE, BIModelClient
from djedai.contracts.agent import ComposeResponse, PlannedCall, Understanding


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


def test_client_omits_reasoning_parameter_for_provider_default(client):
    client.client.chat.completions.create.return_value = create_mock_response(
        '{"intent":"data_question"}'
    )
    client.generate_typed_response("sys", "user", Understanding, stage="plan")
    request = client.client.chat.completions.create.call_args.kwargs
    assert "extra_body" not in request


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high"])
def test_client_sends_stage_reasoning_through_extra_body(mocker, effort):
    mocker.patch("djedai.agent.client.OpenAI")
    client = BIModelClient(
        api_key="test",
        base_url="http://test",
        model_name="test-model",
        reasoning={"understand": effort},
        reasoning_supported=True,
    )
    client.client.chat.completions.create = MagicMock(
        return_value=create_mock_response('{"intent":"data_question"}')
    )

    client.generate_typed_response("sys", "user", Understanding, stage="understand")

    request = client.client.chat.completions.create.call_args.kwargs
    assert request["extra_body"] == {"reasoning_effort": effort}


def test_client_rejects_unknown_reasoning_stage(client):
    with pytest.raises(ValueError, match="Неизвестная стадия reasoning"):
        client.generate_typed_response("sys", "user", Understanding, stage="sql")


def test_client_rejects_non_default_reasoning_by_default(mocker):
    mocker.patch("djedai.agent.client.OpenAI")
    with pytest.raises(ValueError, match="MODEL_REASONING_SUPPORTED=false"):
        BIModelClient(
            api_key="test",
            base_url="http://test",
            model_name="test-model",
            reasoning={"compose": "high"},
        )


def test_client_allows_provider_default_on_unsupported_endpoint(mocker):
    mocker.patch("djedai.agent.client.OpenAI")
    client = BIModelClient(
        api_key="test",
        base_url="http://test",
        model_name="test-model",
        reasoning_supported=False,
    )
    assert client.reasoning == {
        "understand": "provider_default",
        "plan": "provider_default",
        "visualize": "provider_default",
        "compose": "provider_default",
        "verify": "provider_default",
    }


def test_client_does_not_replace_rejected_reasoning_with_default(mocker):
    mocker.patch("djedai.agent.client.OpenAI")
    client = BIModelClient(
        api_key="test",
        base_url="http://test",
        model_name="test-model",
        reasoning={"verify": "high"},
        reasoning_supported=True,
    )
    client.client.chat.completions.create = MagicMock(
        side_effect=APIError(
            message="reasoning_effort is not supported",
            request=MagicMock(),
            body={"error": {"message": "reasoning_effort is not supported"}},
        )
    )

    response = client.generate_typed_response("sys", "user", Understanding, stage="verify")

    assert response.status == "error"
    assert response.error_type == "SERVER_ERROR"
    assert response.message == SERVER_ERROR_MESSAGE
    assert "reasoning_effort" not in response.message
    assert client.client.chat.completions.create.call_count == 1
    assert client.client.chat.completions.create.call_args.kwargs["extra_body"] == {
        "reasoning_effort": "high"
    }


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


def test_client_explicit_output_cap_overrides_the_default(client):
    client.client.chat.completions.create.return_value = create_mock_response(
        '{"intent":"data_question"}'
    )
    client.max_output_tokens = 512

    client.generate_typed_response(
        "sys", "user", Understanding, stage="plan", max_output_tokens=4096
    )

    request = client.client.chat.completions.create.call_args.kwargs
    assert request["max_tokens"] == 4096


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


def test_client_strips_think_tags(client):
    content = (
        "<think>I should classify this as data_overview</think>"
        '{"intent": "data_overview", "entities": {"tables": [], "columns": []}}'
    )
    client.client.chat.completions.create.return_value = create_mock_response(content)

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "success"
    assert response.data.intent == "data_overview"


def test_client_handles_empty_or_none_content(client):
    client.client.chat.completions.create.return_value = create_mock_response("")
    response = client.generate_typed_response("sys", "user", Understanding)
    assert response.status == "error"
    assert response.error_type == "INVALID_JSON"

    client.client.chat.completions.create.return_value = create_mock_response(None)
    response_none = client.generate_typed_response("sys", "user", Understanding)
    assert response_none.status == "error"
    assert response_none.error_type == "INVALID_JSON"


def test_client_handles_markdown_and_surrounding_prose(client):
    content = (
        "Here is the JSON:\n```json\n"
        '{"intent": "data_question", "entities": {"tables": [], "columns": []}}\n'
        "```\nHope that helps!"
    )
    client.client.chat.completions.create.return_value = create_mock_response(content)

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "success"
    assert response.data.intent == "data_question"


def test_client_preserves_markdown_in_compose_response(client):
    content = json.dumps(
        {
            "answer_blocks": [
                {
                    "type": "text",
                    "markdown": "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```",
                }
            ]
        }
    )
    client.client.chat.completions.create.return_value = create_mock_response(content)

    response = client.generate_typed_response("sys", "user", ComposeResponse)

    assert response.status == "success"
    assert (
        response.data.answer_blocks[0].markdown
        == "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```"
    )


def test_client_preserves_markdown_in_compose_response_wrapped_in_markdown(client):
    inner_json = json.dumps(
        {
            "answer_blocks": [
                {
                    "type": "text",
                    "markdown": "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```",
                }
            ]
        }
    )
    content = f"```json\n{inner_json}\n```"
    client.client.chat.completions.create.return_value = create_mock_response(content)

    response = client.generate_typed_response("sys", "user", ComposeResponse)

    assert response.status == "success"
    assert (
        response.data.answer_blocks[0].markdown
        == "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```"
    )


def test_client_preserves_literal_think_tags_inside_json(client):
    content = json.dumps(
        {
            "answer_blocks": [
                {
                    "type": "text",
                    "markdown": "Explanation of <think>some reasoning</think> tag in docs",
                }
            ]
        }
    )
    client.client.chat.completions.create.return_value = create_mock_response(content)

    response = client.generate_typed_response("sys", "user", ComposeResponse)

    assert response.status == "success"
    assert "<think>some reasoning</think>" in response.data.answer_blocks[0].markdown


def test_client_strips_leading_think_and_preserves_inner_markdown(client):
    inner_json = json.dumps(
        {
            "answer_blocks": [
                {
                    "type": "text",
                    "markdown": "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```",
                }
            ]
        }
    )
    content = f"<think>Formulate SQL for user</think>\n{inner_json}"
    client.client.chat.completions.create.return_value = create_mock_response(content)

    response = client.generate_typed_response("sys", "user", ComposeResponse)

    assert response.status == "success"
    assert (
        response.data.answer_blocks[0].markdown
        == "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```"
    )


def test_client_fallback_from_json_schema_to_json_object(client):
    # Simulate first call with json_schema failing with 400 BadRequestError
    from openai import BadRequestError

    error_400 = BadRequestError(
        message="invalid JSON schema for response_format",
        response=MagicMock(status_code=400, headers={}),
        body={"error": {"message": "invalid JSON schema for response_format"}},
    )
    valid_json = json.dumps({"intent": "data_question", "entities": {"tables": [], "columns": []}})
    mock_success = create_mock_response(valid_json)

    client.client.chat.completions.create.side_effect = [error_400, mock_success]

    response = client.generate_typed_response("sys", "user", Understanding)

    assert response.status == "success"
    assert response.data.intent == "data_question"
    assert client.client.chat.completions.create.call_count == 2
    # Verify fallback used json_object
    fallback_call_kwargs = client.client.chat.completions.create.call_args_list[1].kwargs
    assert fallback_call_kwargs["response_format"] == {"type": "json_object"}

    # Subsequent call on the same client should directly use json_object without failing first
    client.client.chat.completions.create.side_effect = [mock_success]
    res2 = client.generate_typed_response("sys", "user2", Understanding)
    assert res2.status == "success"
    assert client.client.chat.completions.create.call_count == 3
    third_call_kwargs = client.client.chat.completions.create.call_args_list[2].kwargs
    assert third_call_kwargs["response_format"] == {"type": "json_object"}


def test_http_client_strips_think_tags(model_endpoint):
    content = (
        "<think>Reasoning steps here...</think>"
        '{"intent": "data_overview", "entities": {"tables": [], "columns": []}}'
    )

    with model_endpoint(lambda _n, _p: {"status": 200, "body": content}) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        res = client.generate_typed_response("sys", "user", Understanding)
        assert res.status == "success"
        assert res.data.intent == "data_overview"


def test_http_client_handles_empty_or_whitespace_content(model_endpoint):
    with model_endpoint(lambda _n, _p: {"status": 200, "body": ""}) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        res = client.generate_typed_response("sys", "user", Understanding)
        assert res.status == "error"
        assert res.error_type == "INVALID_JSON"

    with model_endpoint(lambda _n, _p: {"status": 200, "body": "   \n\t  "}) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        res = client.generate_typed_response("sys", "user", Understanding)
        assert res.status == "error"
        assert res.error_type == "INVALID_JSON"


def test_http_client_fallback_from_json_schema_to_json_object(model_endpoint):
    def reply(n, payload):
        if n == 1:
            assert payload.get("response_format", {}).get("type") == "json_schema"
            return {
                "status": 400,
                "error_body": {
                    "error": {"message": "Invalid response_format: json_schema unsupported"}
                },
            }
        assert payload.get("response_format", {}).get("type") == "json_object"
        assert (
            "Respond with a valid JSON object matching this schema"
            in payload["messages"][0]["content"]
        )
        return {
            "status": 200,
            "body": json.dumps(
                {"intent": "data_question", "entities": {"tables": [], "columns": []}}
            ),
        }

    with model_endpoint(reply) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        res = client.generate_typed_response("sys", "user", Understanding)
        assert res.status == "success"
        assert res.data.intent == "data_question"
        assert len(endpoint.calls) == 2

        # Subsequent call on the same client should NOT make a failing json_schema request
        res2 = client.generate_typed_response("sys", "user2", Understanding)
        assert res2.status == "success"
        assert len(endpoint.calls) == 3
        assert endpoint.calls[2].get("response_format", {}).get("type") == "json_object"


def test_http_client_fallback_shares_execution_budget(model_endpoint):
    from djedai.execution import ExecutionBudget, TurnTimeout

    def reply(n, payload):
        # Exhaust the shared budget only after the first request reached the server.
        # A tiny wall-clock deadline can expire before any HTTP request on a busy host.
        budget.deadline = 0.0
        return {
            "status": 400,
            "error_body": {
                "error": {"message": "Invalid response_format: json_schema unsupported"}
            },
        }

    with model_endpoint(reply) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        budget = ExecutionBudget(5)
        with pytest.raises(TurnTimeout), budget.activate():
            client.generate_typed_response("sys", "user", Understanding)

        # Budget was exhausted during first call/attempt, so fallback was not executed
        assert len(endpoint.calls) == 1


def test_http_client_unrelated_400_with_strict_does_not_trigger_fallback(model_endpoint):
    def reply(n, payload):
        return {
            "status": 400,
            "error_body": {
                "error": {
                    "message": "Parameter strict_filtering cannot be enabled for this dataset",
                    "param": "strict_filtering",
                }
            },
        }

    with model_endpoint(reply) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        res = client.generate_typed_response("sys", "user", Understanding)
        assert res.status == "error"
        assert res.error_type == "SERVER_ERROR"
        assert len(endpoint.calls) == 1
        assert client._supports_json_schema is True


def test_http_client_schema_mismatch_not_published(model_endpoint):
    # Returns valid JSON that does not match Understanding schema
    invalid_payload = {"wrong_field": 12345}

    with model_endpoint(
        lambda _n, _p: {"status": 200, "body": json.dumps(invalid_payload)}
    ) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        res = client.generate_typed_response("sys", "user", Understanding)
        assert res.status == "error"
        assert res.error_type == "SCHEMA_MISMATCH"
        assert res.data is None


def test_http_client_preserves_markdown_code_block_in_compose_response(model_endpoint):
    inner_json = json.dumps(
        {
            "answer_blocks": [
                {
                    "type": "text",
                    "markdown": "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```",
                }
            ]
        }
    )
    with model_endpoint(lambda _n, _p: {"status": 200, "body": inner_json}) as endpoint:
        client = BIModelClient(api_key="k", base_url=endpoint.base_url, model_name="qwen")
        res = client.generate_typed_response("sys", "user", ComposeResponse)
        assert res.status == "success"
        assert (
            res.data.answer_blocks[0].markdown
            == "SQL для подсчёта: ```sql\nSELECT COUNT(*) FROM orders\n```"
        )


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
