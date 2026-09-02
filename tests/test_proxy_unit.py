"""Unit tests for the pure routing/normalisation helpers."""

from __future__ import annotations

import pytest

from llm_assistant_api.errors import ModelNotFoundError
from llm_assistant_api.proxy import Target, prepare_payload, resolve_target
from tests.conftest import (
    AUTOCOMPLETE_UPSTREAM,
    FIM_MODEL,
    PRIMARY_MODEL,
    UPSTREAM,
    make_settings,
)


def test_target_builds_urls_without_double_slashes() -> None:
    target = Target("http://vllm:8000/v1/", PRIMARY_MODEL, "")

    assert target.url("/chat/completions") == "http://vllm:8000/v1/chat/completions"
    assert target.url("models") == "http://vllm:8000/v1/models"


def test_target_omits_authorization_when_no_upstream_key() -> None:
    assert "authorization" not in Target(UPSTREAM, PRIMARY_MODEL, "").headers()
    assert Target(UPSTREAM, PRIMARY_MODEL, "k").headers()["authorization"] == "Bearer k"


@pytest.mark.parametrize("requested", [None, PRIMARY_MODEL, "gpt-4o", "claude-3-5-sonnet"])
def test_unknown_model_names_fall_back_to_the_served_model(requested: str | None) -> None:
    target = resolve_target(make_settings(), requested)

    assert target.base_url == UPSTREAM
    assert target.model_id == PRIMARY_MODEL


def test_fallback_can_be_turned_off_for_strict_clients() -> None:
    settings = make_settings(model_alias_fallback=False)

    assert resolve_target(settings, PRIMARY_MODEL).model_id == PRIMARY_MODEL
    with pytest.raises(ModelNotFoundError) as raised:
        resolve_target(settings, "gpt-4o")
    assert raised.value.available == [PRIMARY_MODEL]


@pytest.mark.parametrize("alias", [FIM_MODEL, "autocomplete"])
def test_autocomplete_aliases_route_to_the_fim_upstream(alias: str) -> None:
    target = resolve_target(make_settings(autocomplete_enabled=True), alias)

    assert target.base_url == AUTOCOMPLETE_UPSTREAM
    assert target.model_id == FIM_MODEL


def test_autocomplete_alias_falls_back_when_the_service_is_disabled() -> None:
    target = resolve_target(make_settings(), "autocomplete")

    assert target.base_url == UPSTREAM
    assert target.model_id == PRIMARY_MODEL


def test_payload_model_is_rewritten_and_other_fields_survive() -> None:
    settings = make_settings()
    payload = {"model": "gpt-4o", "temperature": 0.2, "tools": [{"type": "function"}]}

    prepared = prepare_payload(payload, resolve_target(settings, "gpt-4o"), settings)

    assert prepared["model"] == PRIMARY_MODEL
    assert prepared["temperature"] == 0.2
    assert prepared["tools"] == [{"type": "function"}]
    assert payload["model"] == "gpt-4o", "the caller's dict must not be mutated"


def test_no_cap_leaves_max_tokens_untouched() -> None:
    settings = make_settings(max_tokens_cap=0)
    target = resolve_target(settings, None)

    assert "max_tokens" not in prepare_payload({}, target, settings)
    assert prepare_payload({"max_tokens": 99999}, target, settings)["max_tokens"] == 99999


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, {"max_tokens": 4096}),
        ({"max_tokens": 100}, {"max_tokens": 100}),
        ({"max_tokens": 999999}, {"max_tokens": 4096}),
        ({"max_completion_tokens": 999999}, {"max_completion_tokens": 4096}),
    ],
)
def test_cap_clamps_and_bounds_unbounded_requests(
    payload: dict[str, object], expected: dict[str, int]
) -> None:
    settings = make_settings(max_tokens_cap=4096)

    prepared = prepare_payload(payload, resolve_target(settings, None), settings)

    for field, value in expected.items():
        assert prepared[field] == value
