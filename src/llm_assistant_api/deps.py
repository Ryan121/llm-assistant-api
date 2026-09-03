"""Request-scoped accessors for the objects stored on ``app.state``."""

from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .errors import error_body
from .rate_limiting import rate_limiter

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def _describe(token: str) -> str:
    """A comparable, non-secret fingerprint for the logs."""
    if not token:
        return "no bearer token"
    return f"token {token[:8]}... (length {len(token)})"


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http
    return client


def require_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
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

    # Rate limiting check
    # Using IP address for rate limiting if available, otherwise token
    identifier = _get_client_identifier(credentials, settings, request)
    if not rate_limiter.is_allowed(identifier):
        reset_time = rate_limiter.get_reset_time(identifier)
        log.warning(
            "rate limited request from %s: exceeded limit of %d requests per %d seconds",
            identifier,
            rate_limiter.config.max_requests,
            rate_limiter.config.window_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=error_body(
                "Too many requests. Please slow down.",
                "rate_limit_exceeded",
                "rate_limit_exceeded",
            ),
            headers={
                "Retry-After": str(int(reset_time - time.time())),
                "X-RateLimit-Limit": str(rate_limiter.config.max_requests),
                "X-RateLimit-Remaining": str(rate_limiter.get_remaining_requests(identifier)),
            },
        )

    # compare_digest against every key so the reply time does not leak which
    # prefix matched. It requires ASCII, and a non-ASCII token would otherwise
    # raise TypeError and surface as a 500 instead of a 401.
    if not presented.isascii() or not any(hmac.compare_digest(presented, key) for key in accepted):
        # Logged server-side so the operator can compare the two sides without
        # the client having to paste its key anywhere.
        log.warning(
            "rejected request: presented %s, gateway has %d key(s) configured",
            _describe(presented),
            len(accepted),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_body(
                "Incorrect API key provided. "
                f"This gateway has {len(accepted)} key(s) configured; run "
                "`make vscode-config` on the host to print the expected value, "
                "and `make check-auth` if the editor is already using it.",
                "invalid_request_error",
                "invalid_api_key",
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )


def _get_client_identifier(
    credentials: Optional[HTTPAuthorizationCredentials], settings: Settings, request: Request
) -> str:
    """Get a unique identifier for rate limiting purposes."""
    # Try to use the API key as identifier if available
    if credentials and credentials.credentials:
        return f"key:{credentials.credentials[:16]}"  # Truncate for uniqueness

    # Fall back to IP address
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Take the first IP in the forwarded-for list
        return f"ip:{forwarded_for.split(',')[0].strip()}"

    # Fallback to remote address
    return f"ip:{request.client.host if request.client else 'unknown'}"
