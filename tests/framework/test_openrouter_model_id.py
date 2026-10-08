"""Tests for OpenRouter model id suffix resolution."""

from plugin.framework.openrouter_model_id import (
    openrouter_model_ids_equivalent,
    resolve_openrouter_catalog_id,
)
import pytest


_CATALOG = frozenset(
    {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-120b:free",
        "qwen/qwen-plus-2025-07-28:thinking",
    }
)


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("openai/gpt-oss-120b:nitro", "openai/gpt-oss-120b", id="test_nitro_resolves_to_base_when_in_catalog"),
        pytest.param("openai/gpt-oss-120b:free", "openai/gpt-oss-120b:free", id="test_free_stays_exact_when_in_catalog"),
        pytest.param("qwen/qwen-plus-2025-07-28:thinking", "qwen/qwen-plus-2025-07-28:thinking", id="test_thinking_stays_exact_when_in_catalog"),
        pytest.param("foo/bar:custom", "foo/bar:custom", id="test_unknown_suffix_unchanged"),
    ],
)
def test_nitro_resolves_to_base_when_in_catalog(value, expected) -> None:
    assert resolve_openrouter_catalog_id(value, _CATALOG) == expected


def test_floor_and_exacto_resolve_to_base() -> None:
    assert resolve_openrouter_catalog_id("openai/gpt-oss-120b:floor", _CATALOG) == "openai/gpt-oss-120b"
    assert resolve_openrouter_catalog_id("openai/gpt-oss-120b:exacto", _CATALOG) == "openai/gpt-oss-120b"

def test_nitro_without_catalog_always_strips() -> None:
    assert resolve_openrouter_catalog_id("some/model:nitro", None) == "some/model"


def test_equivalent_nitro_and_base() -> None:
    assert openrouter_model_ids_equivalent("openai/gpt-oss-120b:nitro", "openai/gpt-oss-120b", _CATALOG)


def test_free_not_equivalent_to_base() -> None:
    assert not openrouter_model_ids_equivalent("openai/gpt-oss-120b:free", "openai/gpt-oss-120b", _CATALOG)


def test_openrouter_equivalent_dropped_from_check_all_fqns() -> None:
    """Deep check-all run 32840960268: Prev 11:29. Split/resolve stay on (~2 min)."""
    from pathlib import Path

    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(Path("plugin/framework/openrouter_model_id.py"), require_deal=True)
    assert not any(f.endswith(".openrouter_model_ids_equivalent") for f in fqns)
    assert any(f.endswith("._split_suffix") for f in fqns)
    assert any(f.endswith(".resolve_openrouter_catalog_id") for f in fqns)


def test_catalog_larger_than_shape_dim_resolves() -> None:
    from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN
    from plugin.framework.openrouter_model_id import resolve_openrouter_catalog_id

    model = "vendor/" + ("m" * (DEAL_MAX_TOKEN + 1))
    catalog = {f"id-{i}" for i in range(DEAL_MAX_SHAPE_DIM + 1)}
    catalog.add(model)
    assert resolve_openrouter_catalog_id(model, catalog) == model
