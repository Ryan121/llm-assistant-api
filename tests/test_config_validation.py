"""Settings validation.

A bad value here fails at start-up with a named field rather than as a strange
runtime behaviour hours later, which is the whole reason the validators exist.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from .conftest import make_settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", -1),
        ("connect_timeout_seconds", 0),
        ("max_connections", 0),
        ("max_tokens_cap", -1),
        ("autocomplete_timeout_seconds", 0),
        ("embeddings_timeout_seconds", -5),
        ("context_guard_tokens", -1),
        ("context_guard_margin", 0),
        ("context_guard_margin", 1.5),
        ("chars_per_token", 0),
    ],
)
def test_invalid_values_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_context_budget_applies_the_margin() -> None:
    settings = make_settings(context_guard_tokens=100_000, context_guard_margin=0.9)

    assert settings.context_budget == 90_000


def test_context_budget_is_zero_when_the_guard_is_off() -> None:
    assert make_settings().context_budget == 0


def test_a_margin_of_one_is_allowed() -> None:
    """Exactly the engine's window, with no headroom, is a legitimate choice."""
    settings = make_settings(context_guard_tokens=1000, context_guard_margin=1.0)

    assert settings.context_budget == 1000


def test_served_models_lists_only_what_is_enabled() -> None:
    assert len(make_settings().served_models()) == 1

    everything = make_settings(
        autocomplete_enabled=True, embeddings_enabled=True, rerank_enabled=True
    )
    assert len(everything.served_models()) == 4
