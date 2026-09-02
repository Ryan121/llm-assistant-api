"""Request-scoped accessors for the objects stored on ``app.state``."""

from __future__ import annotations

import hmac

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .errors import error_body

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http
    return client


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Validate the bearer token.

    With ``API_KEYS`` unset the gateway is deliberately open — that is the
    single-workstation case. As soon as one key is configured, a matching
    token is mandatory.
    """
    accepted = settings.api_key_set
    if not accepted:
        return

    presented = credentials.credentials if credentials else ""
    # compare_digest against every key so the reply time does not leak which
    # prefix matched.
    if not any(hmac.compare_digest(presented, key) for key in accepted):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_body(
                "Incorrect API key provided. Set the key configured in API_KEYS.",
                "invalid_request_error",
                "invalid_api_key",
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
