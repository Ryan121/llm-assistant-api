"""Runtime configuration.

Every knob is an environment variable so the same image can front any model
without a rebuild. Comma-separated strings are used instead of ``list[str]``
fields because pydantic-settings would otherwise demand JSON-encoded values in
the environment, which is hostile in a ``.env`` file.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: str) -> tuple[str, ...]:
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

    # --- optional embedding upstream (codebase retrieval) -----------------
    # An agent that can retrieve from the repository answers far better than
    # one working from open files alone. Small enough to share a card with the
    # primary model.
    embeddings_enabled: bool = False
    embeddings_base_url: str = "http://vllm-embeddings:8999/v1"
    embeddings_model_id: str = "Qwen/Qwen3-Embedding-0.6B"

    # --- optional reranker upstream ---------------------------------------
    rerank_enabled: bool = False
    rerank_base_url: str = "http://vllm-rerank:8999/v1"
    rerank_model_id: str = "BAAI/bge-reranker-v2-m3"

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

    # --- per-route timeouts -----------------------------------------------
    # ``request_timeout_seconds`` is sized for a long agent turn. Applying it
    # to autocomplete means an abandoned keystroke can hold a KV-cache slot for
    # fifteen minutes, so those routes get their own, much shorter, budgets.
    autocomplete_timeout_seconds: float = 5.0
    embeddings_timeout_seconds: float = 60.0

    # --- scheduling -------------------------------------------------------
    # Requires vLLM to be started with ``--scheduling-policy priority``.
    # In vLLM a *lower* number is served earlier, so autocomplete - which is
    # worthless if it arrives late - sits in front of a long agent run.
    priority_routing_enabled: bool = False
    autocomplete_priority: int = -1
    chat_priority: int = 0

    # --- context guard ----------------------------------------------------
    # Set to the engine's ``--max-model-len`` to reject an over-long request
    # here, with an actionable message, instead of letting vLLM answer with an
    # opaque 400 halfway through an agent run. 0 disables the check.
    #
    # Deliberately *not* named ``max_model_len``: that would bind to the
    # ``MAX_MODEL_LEN`` every existing .env already sets for the engine, and
    # switch this on for deployments that never asked for it. The estimate is
    # a heuristic, so a silent upgrade into rejecting valid requests is exactly
    # the surprise to avoid.
    context_guard_tokens: int = 0
    context_guard_margin: float = 0.95
    #: Rough bytes-per-token for the estimator. Deliberately a heuristic: an
    #: exact count needs the tokeniser, which would put a ~400 MB dependency in
    #: a 60 MB image for a guard that only has to catch gross overflow.
    chars_per_token: float = 3.5

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
        if self.embeddings_enabled:
            models.append(self.embeddings_model_id)
        if self.rerank_enabled:
            models.append(self.rerank_model_id)
        return models

    @property
    def context_budget(self) -> int:
        """Tokens a request may occupy before the guard rejects it. 0 = off."""
        if self.context_guard_tokens <= 0:
            return 0
        return int(self.context_guard_tokens * self.context_guard_margin)

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

    @field_validator("autocomplete_timeout_seconds", "embeddings_timeout_seconds")
    @classmethod
    def validate_route_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("route timeouts must be positive")
        return v

    @field_validator("context_guard_tokens")
    @classmethod
    def validate_context_guard_tokens(cls, v: int) -> int:
        if v < 0:
            raise ValueError("context_guard_tokens must be non-negative")
        return v

    @field_validator("context_guard_margin")
    @classmethod
    def validate_context_guard_margin(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("context_guard_margin must be in (0, 1]")
        return v

    @field_validator("chars_per_token")
    @classmethod
    def validate_chars_per_token(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("chars_per_token must be positive")
        return v
