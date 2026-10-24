import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from djedai.agent.diagnostics import (
    DiagnosticHttpClient,
    capture_usage,
    model_call,
    retry_started,
    retry_wait,
)
from djedai.agent.transport import attempt_lease
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
    "INVALID_JSON", "SCHEMA_MISMATCH", "OUTPUT_LIMIT", "TIMEOUT", "SERVER_ERROR", "UNKNOWN_ERROR"
]
MODEL_UNAVAILABLE: tuple[ModelErrorType, ...] = ("TIMEOUT", "SERVER_ERROR")


class ModelResponse[T: BaseModel](BaseModel):
    status: Literal["success", "error"]
    data: T | None = None
    error_type: ModelErrorType | None = None
    message: str | None = None
    raw_response: Any = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


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
    ):
        endpoint = urlsplit(base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError("MODEL_BASE_URL должен быть полным HTTP(S)-адресом")
        if not api_key.strip() or any(character in api_key for character in "\r\n"):
            raise ValueError("MODEL_API_KEY не должен быть пустым или содержать переносы строк")
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            http_client=DiagnosticHttpClient(),
        )
        self.model = model_name
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def _request(self, **kwargs: Any) -> Any:
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
                with attempt_lease(budget, self.timeout):
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
        self, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> ModelResponse[T]:
        with model_call(self.model, response_model.__name__) as call:
            try:
                result = self._generate_typed_response(system_prompt, user_prompt, response_model)
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
        self, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> ModelResponse[T]:
        schema_json = response_model.model_json_schema()

        structured_format = {
            "type": "json_schema",
            "json_schema": {"name": response_model.__name__, "schema": schema_json, "strict": True},
        }

        try:
            response = self._request(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=structured_format,
                temperature=0.1,
                **({"max_tokens": self.max_output_tokens} if self.max_output_tokens else {}),
            )

            capture_usage(response.usage)

            if response.choices[0].finish_reason == "length":
                return ModelResponse(
                    status="error",
                    error_type="OUTPUT_LIMIT",
                    message="Model output token limit reached.",
                )

            raw_content = response.choices[0].message.content.strip()

            # Clean markdown code blocks, add array wrapper (e.g. for Gemma) if needed
            clean_content = raw_content.removeprefix("```json").removesuffix("```").strip()
            # if clean_content.startswith("[") and clean_content.endswith("]"):
            # clean_content = clean_content[1:-1]

            parsed_json = json.loads(clean_content)
            validated_data = response_model.model_validate(parsed_json)

            return ModelResponse(
                status="success",
                data=validated_data,
                prompt_tokens=response.usage.prompt_tokens if response.usage else None,
                completion_tokens=response.usage.completion_tokens if response.usage else None,
                finish_reason=response.choices[0].finish_reason,
            )

        except (TurnTimeout, TurnCancelled):
            raise
        except json.JSONDecodeError as e:
            return ModelResponse(
                status="error",
                error_type="INVALID_JSON",
                message=str(e),
                raw_response=raw_content if "raw_content" in locals() else None,
            )
        except ValidationError as e:
            return ModelResponse(
                status="error",
                error_type="SCHEMA_MISMATCH",
                message=str(e),
                raw_response=parsed_json if "parsed_json" in locals() else None,
            )
        except APITimeoutError:
            return ModelResponse(
                status="error",
                error_type="TIMEOUT",
                message="Модель не ответила за отведённое время.",
            )
        except (APIError, APIConnectionError, RateLimitError) as e:
            return ModelResponse(
                status="error",
                error_type="SERVER_ERROR",
                message=str(e),
            )
        except Exception as e:
            return ModelResponse(
                status="error",
                error_type="UNKNOWN_ERROR",
                message=str(e),
            )
