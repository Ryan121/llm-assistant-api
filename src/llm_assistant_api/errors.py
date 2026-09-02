"""Error helpers that keep responses shaped like the OpenAI API.

VS Code extensions surface ``error.message`` verbatim, so the shape matters
more than the status code.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def error_body(message: str, err_type: str, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": err_type, "param": None, "code": code}}


def error_response(
    status_code: int, message: str, err_type: str, code: str | None = None
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(message, err_type, code))


class UpstreamError(Exception):
    """The vLLM backend could not be reached or did not answer in time."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ModelNotFoundError(Exception):
    """The requested model is not served by this gateway."""

    def __init__(self, model: str, available: list[str]) -> None:
        super().__init__(model)
        self.model = model
        self.available = available
