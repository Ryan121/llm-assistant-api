"""Runtime configuration.

Every knob is an environment variable so the same image can front any model
without a rebuild. Comma-separated strings are used instead of ``list[str]``
fields because pydantic-settings would otherwise demand JSON-encoded values in
the environment, which is hostile in a ``.env`` file.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Tuple


def _split_csv(raw: str) -> Tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


class Settings(BaseSettings):
    """Application settings, populated from the environment or a ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # ``model_id`` collides with pydantic's reserved ``model_`` namespace.
        protected_namespaces=(),
    )

    # --- service ----------------------------------------------------------
    app_name: str = "llm-assistant-api"
    log_level: str = "INFO"
    cors_origins: str = ""

    # --- primary upstream (chat / edit / agent) ---------------------------
    upstream_base_url: str = "http://vllm:8999/v1"
    upstream_api_key: str = ""
    model_id: str = "Qwen/Qwen3-Coder-30B-A3B-Instruct"

    # --- optional secondary upstream (fill-in-the-middle autocomplete) ----
    autocomplete_enabled: bool = False
    autocomplete_base_url: str = "http://vllm-autocomplete:8999/v1"
    autocomplete_model_id: str = "Qwen/Qwen2.5-Coder-1.5B"

    # --- client access ----------------------------------------------------
    api_keys: str = ""

    # --- proxy behaviour --------------------------------------------------
    request_timeout_seconds: float = 900.0
    connect_timeout_seconds: float = 10.0
    max_connections: int = 100
    max_tokens_cap: int = 0
    model_alias_fallback: bool = True
    expose_upstream_models: bool = False
    normalize_tool_arguments: bool = True
    normalize_empty_tool_arguments: bool = True

    @property
    def api_key_set(self) -> frozenset[str]:
        """Accepted bearer tokens. Empty means the API is unauthenticated."""
        return frozenset(_split_csv(self.api_keys))

    @property
    def cors_origin_list(self) -> list[str]:
        return list(_split_csv(self.cors_origins))

    @property
    def autocomplete_aliases(self) -> frozenset[str]:
        """Model names that should be routed to the autocomplete upstream."""
        if not self.autocomplete_enabled:
            return frozenset()
        return frozenset({self.autocomplete_model_id, "autocomplete"})

    def served_models(self) -> list[str]:
        models = [self.model_id]
        if self.autocomplete_enabled:
            models.append(self.autocomplete_model_id)
        return models

    # Validation methods - using Pydantic V2 field_validator syntax
    @field_validator("request_timeout_seconds")
    @classmethod
    def validate_request_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        return v

    @field_validator("connect_timeout_seconds")
    @classmethod
    def validate_connect_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        return v

    @field_validator("max_connections")
    @classmethod
    def validate_max_connections(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_connections must be positive")
        return v

    @field_validator("max_tokens_cap")
    @classmethod
    def validate_max_tokens_cap(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_tokens_cap must be non-negative")
        return v
