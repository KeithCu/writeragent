"""Tests for OpenRouter model id suffix resolution."""

from plugin.framework.openrouter_model_id import (
    openrouter_model_ids_equivalent,
    resolve_openrouter_catalog_id,
)


_CATALOG = frozenset(
    {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-120b:free",
        "qwen/qwen-plus-2025-07-28:thinking",
    }
)


def test_nitro_resolves_to_base_when_in_catalog() -> None:
    assert resolve_openrouter_catalog_id("openai/gpt-oss-120b:nitro", _CATALOG) == "openai/gpt-oss-120b"


def test_floor_and_exacto_resolve_to_base() -> None:
    assert resolve_openrouter_catalog_id("openai/gpt-oss-120b:floor", _CATALOG) == "openai/gpt-oss-120b"
    assert resolve_openrouter_catalog_id("openai/gpt-oss-120b:exacto", _CATALOG) == "openai/gpt-oss-120b"


def test_free_stays_exact_when_in_catalog() -> None:
    assert resolve_openrouter_catalog_id("openai/gpt-oss-120b:free", _CATALOG) == "openai/gpt-oss-120b:free"


def test_thinking_stays_exact_when_in_catalog() -> None:
    assert (
        resolve_openrouter_catalog_id("qwen/qwen-plus-2025-07-28:thinking", _CATALOG)
        == "qwen/qwen-plus-2025-07-28:thinking"
    )


def test_unknown_suffix_unchanged() -> None:
    assert resolve_openrouter_catalog_id("foo/bar:custom", _CATALOG) == "foo/bar:custom"


def test_nitro_without_catalog_always_strips() -> None:
    assert resolve_openrouter_catalog_id("some/model:nitro", None) == "some/model"


def test_equivalent_nitro_and_base() -> None:
    assert openrouter_model_ids_equivalent("openai/gpt-oss-120b:nitro", "openai/gpt-oss-120b", _CATALOG)


def test_free_not_equivalent_to_base() -> None:
    assert not openrouter_model_ids_equivalent("openai/gpt-oss-120b:free", "openai/gpt-oss-120b", _CATALOG)


def test_openrouter_equivalent_dropped_from_check_all_fqns() -> None:
    """Deep check-all run 32840960268: Prev 11:29. Split/resolve stay on (~2 min)."""
    from pathlib import Path

    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(Path("plugin/framework/openrouter_model_id.py"), require_deal=True)
    assert not any(f.endswith(".openrouter_model_ids_equivalent") for f in fqns)
    assert any(f.endswith("._split_suffix") for f in fqns)
    assert any(f.endswith(".resolve_openrouter_catalog_id") for f in fqns)
