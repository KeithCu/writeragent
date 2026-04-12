# WriterAgent tests — Orca catalog normalization
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from plugin.framework.orca_catalog import (
    aggregate_pricing,
    aggregate_providers,
    capability_mismatch_warning,
    context_length_mismatch_warning,
    format_capability_labels,
    merge_default_entry_from_slim,
    normalize_orca_model,
    orca_slim_to_model_capability,
    slim_catalog_payload,
    slim_record_supports_tool_calling,
)
from plugin.framework.types import ModelCapability


def test_aggregate_pricing_averages_numeric_fields() -> None:
    p = aggregate_pricing(
        [
            {"pricing": {"text_input": "0.01", "text_output": "0.04"}},
            {"pricing": {"text_input": "0.03", "text_output": "0.08"}},
        ]
    )
    assert p is not None
    assert p["text_input"] == 0.02
    assert p["text_output"] == 0.06


def test_aggregate_pricing_omits_tiers() -> None:
    p = aggregate_pricing(
        [
            {
                "pricing": {
                    "text_input": "1",
                    "tiers": [{"text_input": "99", "tokens": 1000}],
                }
            }
        ]
    )
    assert p is not None
    assert "tiers" not in p
    assert p["text_input"] == 1.0


def test_aggregate_providers_union_and_max() -> None:
    agg = aggregate_providers(
        [
            {"context_length": 1000, "supported_parameters": ["tools", "temperature"]},
            {"context_length": 8000, "supported_parameters": ["max_tokens", "tools"]},
        ]
    )
    assert agg["context_length"] == 8000
    assert agg["supported_parameters"] == ["max_tokens", "temperature", "tools"]


def test_normalize_orca_model_no_providers_key() -> None:
    slim = normalize_orca_model(
        {
            "id": "x/y",
            "name": "X",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "providers": [],
        }
    )
    assert slim["id"] == "x/y"
    assert slim["context_length"] is None
    assert slim["supported_parameters"] == []


def test_slim_catalog_payload_drops_non_tool_models() -> None:
    raw = {
        "updated_at": "t",
        "models": [
            {
                "id": "has/tools",
                "name": "A",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "providers": [{"supported_parameters": ["tools", "max_tokens"]}],
            },
            {
                "id": "no/tools",
                "name": "B",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "providers": [{"supported_parameters": ["max_tokens"]}],
            },
        ],
    }
    slim = slim_catalog_payload(raw, source_url="http://test")
    assert len(slim["models"]) == 1
    assert slim["models"][0]["id"] == "has/tools"


def test_slim_record_supports_tool_calling() -> None:
    assert slim_record_supports_tool_calling({"supported_parameters": ["tools"]})
    assert not slim_record_supports_tool_calling({"supported_parameters": ["max_tokens"]})
    assert not slim_record_supports_tool_calling({})


def test_slim_catalog_payload() -> None:
    raw = {
        "updated_at": "t",
        "models": [
            {
                "id": "a/b",
                "name": "AB",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "providers": [
                    {
                        "context_length": 4096,
                        "supported_parameters": ["tools"],
                        "chat_completions": True,
                    }
                ],
            }
        ],
    }
    slim = slim_catalog_payload(raw, source_url="http://test")
    assert slim["_meta"]["source_url"] == "http://test"
    assert slim["models"][0]["context_length"] == 4096
    assert "providers" not in slim["models"][0]


def test_orca_slim_to_model_capability() -> None:
    s = {
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supported_parameters": ["tools"],
    }
    c = orca_slim_to_model_capability(s)
    assert c & ModelCapability.CHAT
    assert c & ModelCapability.VISION
    assert c & ModelCapability.TOOLS


def test_format_capability_labels() -> None:
    s = format_capability_labels(
        int(ModelCapability.CHAT | ModelCapability.TOOLS),
    )
    assert "CHAT" in s and "TOOLS" in s


def test_context_length_mismatch_warning_none_when_equal() -> None:
    assert (
        context_length_mismatch_warning("a/b", "X", 1000, 1000) is None
    )


def test_context_length_mismatch_warning_when_differs() -> None:
    w = context_length_mismatch_warning("a/b", "X", 1000, 2000)
    assert w is not None and "1000" in w and "2000" in w


def test_capability_mismatch_warning_none_when_equal() -> None:
    c = int(ModelCapability.CHAT | ModelCapability.TOOLS)
    assert capability_mismatch_warning("a/b", "X", c, c) is None


def test_capability_mismatch_warning_when_differs() -> None:
    w = capability_mismatch_warning(
        "a/b",
        "X",
        int(ModelCapability.CHAT),
        int(ModelCapability.CHAT | ModelCapability.TOOLS),
    )
    assert w is not None and "only in Orca" in w


def test_merge_default_entry_from_slim_or_capabilities() -> None:
    entry = {
        "display_name": "T",
        "capability": ModelCapability.CHAT,
        "context_length": 100,
        "ids": {"openrouter": "a/b"},
    }
    slim = {
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "supported_parameters": ["tools"],
        "context_length": 500,
    }
    merge_default_entry_from_slim(entry, slim)
    assert entry["context_length"] == 500
    assert entry["capability"] & ModelCapability.TOOLS
