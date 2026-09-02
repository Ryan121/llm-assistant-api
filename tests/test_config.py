from __future__ import annotations

from tests.conftest import FIM_MODEL, make_settings


def test_api_keys_parsed_from_comma_separated_string() -> None:
    settings = make_settings(api_keys=" alpha , beta ,,")
    assert settings.api_key_set == frozenset({"alpha", "beta"})


def test_empty_api_keys_means_unauthenticated() -> None:
    assert make_settings(api_keys="").api_key_set == frozenset()


def test_cors_origins_default_to_no_origins() -> None:
    assert make_settings().cors_origin_list == []
    assert make_settings(cors_origins="http://a,http://b").cors_origin_list == [
        "http://a",
        "http://b",
    ]


def test_served_models_excludes_autocomplete_unless_enabled() -> None:
    disabled = make_settings()
    assert disabled.served_models() == [disabled.model_id]
    assert disabled.autocomplete_aliases == frozenset()

    enabled = make_settings(autocomplete_enabled=True, autocomplete_model_id=FIM_MODEL)
    assert enabled.served_models() == [enabled.model_id, FIM_MODEL]
    assert enabled.autocomplete_aliases == frozenset({FIM_MODEL, "autocomplete"})
