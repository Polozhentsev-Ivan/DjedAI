import json
from typing import Any, Literal

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError


class ModelResponse[T: BaseModel](BaseModel):
    status: Literal["success", "error"]
    data: T | None = None
    error_type: str | None = None
    message: str | None = None
    raw_response: Any = None


class BIModelClient:
    def __init__(self, api_key: str, base_url: str, model_name: str, timeout: float = 15.0):
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.model = model_name

    def generate_typed_response[T: BaseModel](
        self, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> ModelResponse[T]:
        schema_json = response_model.model_json_schema()

        structured_format = {
            "type": "json_schema",
            "json_schema": {"name": response_model.__name__, "schema": schema_json, "strict": True},
        }

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=structured_format,
                temperature=0.1,
            )

            raw_content = response.choices[0].message.content.strip()

            # Clean markdown code blocks, add array wrapper (e.g. for Gemma) if needed
            clean_content = raw_content.removeprefix("```json").removesuffix("```").strip()
            # if clean_content.startswith("[") and clean_content.endswith("]"):
            # clean_content = clean_content[1:-1]

            parsed_json = json.loads(clean_content)
            validated_data = response_model.model_validate(parsed_json)

            return ModelResponse(status="success", data=validated_data)

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
                message="Model server timed out.",
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
