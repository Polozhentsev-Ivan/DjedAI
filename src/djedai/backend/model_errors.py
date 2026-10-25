"""Safe model configuration errors allowed through the normal 5xx redaction."""

from fastapi import HTTPException

from djedai.agent.reasoning import ReasoningConfigurationError


class ModelConfigurationHTTPError(HTTPException):
    def __init__(self, error: ReasoningConfigurationError) -> None:
        # Only the fixed public explanation is exposed, never raw settings/input.
        super().__init__(503, detail=error.public_message)
