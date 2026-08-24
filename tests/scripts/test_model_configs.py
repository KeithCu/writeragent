# WriterAgent tests for scripts/prompt_optimization/model_configs.py
from __future__ import annotations

import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from model_configs import (  # noqa: E402
    GOLD_ONLY_MODEL_IDS,
    MODEL_BY_ID,
    MODELS,
    get_default_models,
)


# Default sweep + China pack; gold-only stays in MODELS only.
EXPECTED_DEFAULT_IDS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "openai/gpt-5.6-luna",
    "google/gemini-3.7-flash",
    "google/gemini-3.5-flash-lite",
    "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/nemotron-3.5-lightning",
    "inception/mercury-2",
    "x-ai/grok-4.6",
    "meta/muse-glimmer-30b",
    "meta/muse-spark-1.2-contributor",
    "poolside/laguna-s-2.1",
    "qwen/qwen3.8-27b",
    "z-ai/glm-5.3",
    "minimax/minimax-m3",
    "deepseek/deepseek-v4-flash-0731",
    "qwen/qwen3.7-flash",
]

DROPPED_SLUGS = [
    "x-ai/grok-4.1-fast",
    "allenai/olmo-3.1-32b-instruct",
    "mistralai/devstral-2512",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemini-3-flash-preview",
    "z-ai/glm-5.1",
    "minimax/minimax-m2.7",
    "deepseek/deepseek-v3.2",
    "qwen/qwen3.5-9b",
    "qwen/qwen3.5-27b",
    "qwen/qwen3.5-35b-a3b",
    "qwen/qwen3.5-122b-a10b",
]


def test_default_models_excludes_gold_only() -> None:
    default_ids = [m.openrouter_id for m in get_default_models()]
    assert default_ids == EXPECTED_DEFAULT_IDS
    assert "anthropic/claude-sonnet-4.6" not in default_ids
    assert GOLD_ONLY_MODEL_IDS == frozenset({"anthropic/claude-sonnet-4.6"})
    assert "anthropic/claude-sonnet-4.6" in MODEL_BY_ID


def test_models_catalog_matches_defaults_plus_gold() -> None:
    catalog_ids = [m.openrouter_id for m in MODELS]
    assert catalog_ids == EXPECTED_DEFAULT_IDS + ["anthropic/claude-sonnet-4.6"]
    for slug in DROPPED_SLUGS:
        assert slug not in MODEL_BY_ID


def test_model_config_fields_are_populated() -> None:
    for model in MODELS:
        assert model.openrouter_id
        assert model.display_name
        assert model.context_window_tokens is not None
        assert model.context_window_tokens > 0
        assert model.input_cost_per_million >= 0
        assert model.output_cost_per_million >= 0
        assert model.notes
        assert ":" not in model.openrouter_id.split("/", 1)[-1]
