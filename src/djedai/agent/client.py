import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from djedai.agent.data_policy import (
    POLICY_RESTRICTED_MESSAGE,
    DataTransferPolicy,
    DataTransferRestricted,
)
from djedai.agent.diagnostics import (
    DiagnosticHttpClient,
    capture_usage,
    model_call,
    retry_started,
    retry_wait,
)
from djedai.agent.reasoning import (
    PROVIDER_DEFAULT,
    REASONING_STAGES,
    ReasoningConfigurationError,
    actual_reasoning_effort,
    normalize_reasoning_profile,
)
from djedai.agent.transport import attempt_lease
from djedai.config import ReasoningEffort
from djedai.execution import (
    ExecutionBudget,
    TurnCancelled,
    TurnTimeout,
    current_budget,
)

# Типы ошибок — общий контракт: по ним потребители решают, повторять ли вызов.
# INVALID_JSON / SCHEMA_MISMATCH / OUTPUT_LIMIT — модель ответила, но ответ не принят;
# TIMEOUT / SERVER_ERROR — модель недоступна; UNKNOWN_ERROR — прочие исключения.
ModelErrorType = Literal[
    "INVALID_JSON",
    "SCHEMA_MISMATCH",
    "OUTPUT_LIMIT",
    "TIMEOUT",
    "SERVER_ERROR",
    "UNKNOWN_ERROR",
    "POLICY_RESTRICTED",
]
MODEL_UNAVAILABLE: tuple[ModelErrorType, ...] = ("TIMEOUT", "SERVER_ERROR")

# Failure messages stay content-free: provider bodies and model output are never
# copied into user-visible errors or diagnostics.
INVALID_JSON_MESSAGE = "Модель вернула ответ не в формате JSON."
SCHEMA_MISMATCH_MESSAGE = "Ответ модели не соответствует ожидаемой схеме."
OUTPUT_LIMIT_MESSAGE = "Модель достигла лимита вывода."
TIMEOUT_MESSAGE = "Модель не ответила за отведённое время."
SERVER_ERROR_MESSAGE = "Модель недоступна или вернула ошибку сервера."
UNKNOWN_ERROR_MESSAGE = "Не удалось получить ответ модели."


def _schema_prompt(system_prompt: str, schema_json: dict[str, Any]) -> str:
    schema_str = json.dumps(schema_json, ensure_ascii=False)
    return (
        f"{system_prompt}\n\nRespond with a valid JSON object matching this schema:\n{schema_str}"
    )


def _is_json_schema_rejection(exc: APIError) -> bool:
    if getattr(exc, "status_code", None) not in {400, 422}:
        return False
    msg = (str(exc) or "").lower()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", {})
        if isinstance(err, dict):
            if err.get("param") in {"response_format", "response_format.type", "json_schema"}:
                return True
            msg += " " + (str(err.get("message") or "")).lower()
    return any(marker in msg for marker in ("json_schema", "response_format", "schema", "strict"))


class ModelResponse[T: BaseModel](BaseModel):
    status: Literal["success", "error"]
    data: T | None = None
    error_type: ModelErrorType | None = None
    message: str | None = None
    raw_response: Any = None
    http_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


def valid_base_url(base_url: str) -> bool:
    endpoint = urlsplit(base_url)
    return endpoint.scheme in {"http", "https"} and bool(endpoint.hostname)


def valid_api_key(api_key: str) -> bool:
    return bool(api_key.strip()) and not any(character in api_key for character in "\r\n")


class BIModelClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout: float = 15.0,
        max_output_tokens: int | None = None,
        max_retries: int = 1,
        retry_delay: float = 0.5,
        reasoning: Mapping[str, ReasoningEffort] | None = None,
        reasoning_supported: bool = False,
        data_policy: DataTransferPolicy | None = None,
    ):
        if not valid_base_url(base_url):
            raise ValueError("MODEL_BASE_URL должен быть полным HTTP(S)-адресом")
        if not valid_api_key(api_key):
            raise ValueError("MODEL_API_KEY не должен быть пустым или содержать переносы строк")
        profile = normalize_reasoning_profile(reasoning)
        if not reasoning_supported:
            enabled = {
                stage: effort for stage, effort in profile.items() if effort != PROVIDER_DEFAULT
            }
            if enabled:
                raise ReasoningConfigurationError(
                    "unsupported",
                    "MODEL_REASONING_SUPPORTED=false: endpoint/модель не поддерживает "
                    "reasoning_effort, но заданы режимы "
                    f"{enabled}. Укажите provider_default для всех стадий или "
                    "поддерживающий reasoning endpoint.",
                )
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            http_client=DiagnosticHttpClient(),
        )
        self.model = model_name
        self.reasoning = profile
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        # When an OpenAI-compatible server rejects json_schema (400/422), fallback
        # to json_object with the schema in the system prompt for this and subsequent calls.
        self._supports_json_schema: bool = True
        # One policy per client. Callers that build structured prompts use the same
        # instance, so retries and repairs cannot bypass the category rules.
        self.data_policy = data_policy or DataTransferPolicy.from_env()

    def reasoning_for(self, stage: str | None) -> ReasoningEffort:
        """Return the configured effort for a stage; an omitted stage keeps the default."""
        if stage is None:
            return PROVIDER_DEFAULT
        if stage not in REASONING_STAGES:
            raise ValueError(
                f"Неизвестная стадия reasoning: {stage!r}; "
                f"допустимые стадии: {', '.join(REASONING_STAGES)}"
            )
        return self.reasoning[stage]

    def _request(self, *, read_error_body: bool = False, **kwargs: Any) -> Any:
        budget = current_budget() or ExecutionBudget(
            self.timeout * (self.max_retries + 1) + self.retry_delay * (2**self.max_retries)
        )
        # Keep one retry policy even when a test/integration supplies its own SDK client.
        self.client.max_retries = 0
        for attempt in range(self.max_retries + 1):
            budget.check()
            if attempt:
                retry_started()
            try:
                with attempt_lease(budget, self.timeout, read_error_body=read_error_body):
                    response = self.client.chat.completions.create(**kwargs)
                budget.check()
                return response
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                budget.check()
                timed_out = isinstance(exc, APITimeoutError)
                status = getattr(exc, "status_code", None)
                transient = (
                    timed_out
                    or isinstance(exc, APIConnectionError)
                    or status in {408, 429, 500, 502, 503, 504}
                )
                if attempt >= self.max_retries or not transient:
                    if timed_out:
                        raise APITimeoutError(request=exc.request) from exc
                    raise
                delay = self.retry_delay * 2**attempt
                headers = getattr(getattr(exc, "response", None), "headers", {})
                retry_after = headers.get("retry-after")
                try:
                    if headers.get("retry-after-ms"):
                        delay = float(headers["retry-after-ms"]) / 1000
                    elif retry_after is not None:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            date = parsedate_to_datetime(retry_after)
                            delay = max(0.0, (date - datetime.now(UTC)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
                if math.isnan(delay) or delay < 0:
                    delay = self.retry_delay * 2**attempt
                if delay >= budget.remaining:
                    # Honour the server's delay by not attempting an early retry.
                    raise
                retry_wait(delay)
                budget.wait(delay)
        raise RuntimeError("Unreachable model retry state")

    def generate_typed_response[T: BaseModel](
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        *,
        stage: str | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelResponse[T]:
        setting = self.reasoning_for(stage)
        effort = actual_reasoning_effort(setting)
        with model_call(
            self.model,
            response_model.__name__,
            stage=stage,
            reasoning_effort=effort,
            reasoning_setting=setting,
        ) as call:
            try:
                self.data_policy.guard(system_prompt, user_prompt, stage=stage)
            except DataTransferRestricted:
                # Rejection happens before _generate_typed_response and therefore
                # before any network attempt or retry.
                result = ModelResponse(
                    status="error",
                    error_type="POLICY_RESTRICTED",
                    message=POLICY_RESTRICTED_MESSAGE,
                )
                if call:
                    call.finish(result.error_type)
                return result
            try:
                result = self._generate_typed_response(
                    system_prompt,
                    user_prompt,
                    response_model,
                    reasoning_effort=effort,
                    max_output_tokens=max_output_tokens,
                )
            except (TurnTimeout, TurnCancelled) as exc:
                if call:
                    call.finish("TIMEOUT" if isinstance(exc, TurnTimeout) else "CANCELLED")
                raise
            except Exception:
                if call:
                    call.finish("UNKNOWN_ERROR")
                raise
            if call:
                call.finish(result.error_type)
            return result

    def _generate_typed_response[T: BaseModel](
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        *,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelResponse[T]:
        schema_json = response_model.model_json_schema()
        use_schema = self._supports_json_schema

        if use_schema:
            structured_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": schema_json,
                    "strict": True,
                },
            }
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        else:
            structured_format = {"type": "json_object"}
            messages = [
                {
                    "role": "system",
                    "content": _schema_prompt(system_prompt, schema_json),
                },
                {"role": "user", "content": user_prompt},
            ]

        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": structured_format,
            "temperature": 0.1,
        }
        output_limit = (
            max_output_tokens if max_output_tokens is not None else self.max_output_tokens
        )
        if output_limit:
            request["max_tokens"] = output_limit
        if reasoning_effort is not None:
            # DeepInfra documents reasoning_effort inside extra_body for the
            # OpenAI-compatible Chat Completions API.
            request["extra_body"] = {"reasoning_effort": reasoning_effort}

        metadata: dict[str, Any] = {}
        try:
            try:
                response = self._request(read_error_body=use_schema, **request)
            except APIError as exc:
                if use_schema and _is_json_schema_rejection(exc):
                    self._supports_json_schema = False
                    fallback_req = dict(request)
                    fallback_req["response_format"] = {"type": "json_object"}
                    fallback_req["messages"] = [
                        {
                            "role": "system",
                            "content": _schema_prompt(system_prompt, schema_json),
                        },
                        {"role": "user", "content": user_prompt},
                    ]
                    response = self._request(**fallback_req)
                else:
                    raise

            capture_usage(response.usage)
            metadata = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else None,
                "completion_tokens": response.usage.completion_tokens if response.usage else None,
                "finish_reason": response.choices[0].finish_reason,
            }

            if response.choices[0].finish_reason == "length":
                return ModelResponse(
                    status="error",
                    error_type="OUTPUT_LIMIT",
                    message=OUTPUT_LIMIT_MESSAGE,
                    raw_response=response.choices[0].message.content,
                    **metadata,
                )

            content_val = response.choices[0].message.content
            raw_content = (content_val or "").strip()

            if not raw_content:
                return ModelResponse(
                    status="error",
                    error_type="INVALID_JSON",
                    message=INVALID_JSON_MESSAGE,
                    raw_response=raw_content,
                    **metadata,
                )

            # Strip <think>...</think> blocks from reasoning models
            clean_content = re.sub(r"(?is)<think>.*?</think>", "", raw_content).strip()

            # Clean markdown code blocks
            if "```" in clean_content:
                match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean_content, re.IGNORECASE)
                if match:
                    clean_content = match.group(1).strip()
                else:
                    clean_content = (
                        clean_content.removeprefix("```json")
                        .removeprefix("```")
                        .removesuffix("```")
                        .strip()
                    )

            try:
                parsed_json = json.loads(clean_content)
            except json.JSONDecodeError:
                start_brace = clean_content.find("{")
                start_bracket = clean_content.find("[")
                if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
                    end_brace = clean_content.rfind("}")
                    if end_brace > start_brace:
                        parsed_json = json.loads(clean_content[start_brace : end_brace + 1])
                    else:
                        raise
                elif start_bracket != -1:
                    end_bracket = clean_content.rfind("]")
                    if end_bracket > start_bracket:
                        parsed_json = json.loads(clean_content[start_bracket : end_bracket + 1])
                    else:
                        raise
                else:
                    raise

            validated_data = response_model.model_validate(parsed_json)

            return ModelResponse(
                status="success",
                data=validated_data,
                **metadata,
            )

        except (TurnTimeout, TurnCancelled):
            raise
        except json.JSONDecodeError:
            return ModelResponse(
                status="error",
                error_type="INVALID_JSON",
                message=INVALID_JSON_MESSAGE,
                raw_response=raw_content if "raw_content" in locals() else None,
                **metadata,
            )
        except ValidationError:
            return ModelResponse(
                status="error",
                error_type="SCHEMA_MISMATCH",
                message=SCHEMA_MISMATCH_MESSAGE,
                raw_response=parsed_json if "parsed_json" in locals() else None,
                **metadata,
            )
        except APITimeoutError:
            return ModelResponse(
                status="error",
                error_type="TIMEOUT",
                message=TIMEOUT_MESSAGE,
            )
        except (APIError, APIConnectionError, RateLimitError) as e:
            return ModelResponse(
                status="error",
                error_type="SERVER_ERROR",
                message=SERVER_ERROR_MESSAGE,
                http_status=getattr(e, "status_code", None),
            )
        except Exception:
            return ModelResponse(
                status="error",
                error_type="UNKNOWN_ERROR",
                message=UNKNOWN_ERROR_MESSAGE,
                **metadata,
            )
