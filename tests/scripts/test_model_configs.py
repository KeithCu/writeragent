# WriterAgent tests for scripts/prompt_optimization/model_configs.py
from __future__ import annotations

import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from model_configs import (  # noqa: E402
    DEFAULT_EVAL_STUDENT_MODEL,
    DEFAULT_GOLD_MODEL,
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
    "google/gemini-3.5-flash-lite",
    "google/gemma-4-31b-it",
    "google/gemma-4-26b-a4b-it",
    "nvidia/nemotron-3.5-lightning",
    "inception/mercury-2.5-preview",
    "x-ai/grok-4.6",
    "meta/muse-glimmer-30b",
    "meta/muse-spark-1.3-contributor",
    "poolside/laguna-s-2.1",
    "poolside/laguna-xs-2.1",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.8-flash",
    "z-ai/glm-5.3-flash",
    "upstage/solar-pro4",
    "ibm-granite/granite-4.2-8b",
    "mistralai/mistral-small-2603",
    "bytedance-seed/seed-2.0-mini",
    "minimax/minimax-m3",
    "deepseek/deepseek-v4-flash-0731",
]

EXPECTED_GOLD_ONLY_IDS: list[str] = []

DROPPED_SLUGS = [
    "inception/mercury-2",
    "meta/muse-spark-1.2-contributor",
    "x-ai/grok-4.1-fast",
    "allenai/olmo-3.1-32b-instruct",
    "mistralai/devstral-2512",
    "openai/gpt-5-nano",
    "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemini-3-flash-preview",
    "google/gemini-3.7-flash",
    "z-ai/glm-5.1",
    "z-ai/glm-5.3",
    "minimax/minimax-m2.7",
    "deepseek/deepseek-v3.2",
    "qwen/qwen3.5-9b",
    "qwen/qwen3.5-27b",
    "qwen/qwen3.5-35b-a3b",
    "qwen/qwen3.5-122b-a10b",
    "qwen/qwen3.7-flash",
    "qwen/qwen3-30b-a3b-instruct-2507",
    "qwen/qwen3-coder-next",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-sonnet-5",
]


def test_default_models_excludes_gold_only() -> None:
    default_ids = [m.openrouter_id for m in get_default_models()]
    assert default_ids == EXPECTED_DEFAULT_IDS
    assert GOLD_ONLY_MODEL_IDS == frozenset(EXPECTED_GOLD_ONLY_IDS)


def test_models_catalog_matches_defaults_plus_gold() -> None:
    catalog_ids = [m.openrouter_id for m in MODELS]
    assert catalog_ids == EXPECTED_DEFAULT_IDS + EXPECTED_GOLD_ONLY_IDS
    for slug in DROPPED_SLUGS:
        assert slug not in MODEL_BY_ID



def test_default_eval_student_model_is_in_catalog() -> None:
    from plugin.framework.openrouter_model_id import resolve_openrouter_catalog_id

    assert DEFAULT_EVAL_STUDENT_MODEL == "openai/gpt-oss-120b:nitro"
    resolved = resolve_openrouter_catalog_id(DEFAULT_EVAL_STUDENT_MODEL, set(MODEL_BY_ID))
    assert resolved in MODEL_BY_ID
    assert resolved not in GOLD_ONLY_MODEL_IDS
    assert DEFAULT_GOLD_MODEL in MODEL_BY_ID
    assert DEFAULT_GOLD_MODEL == "openai/gpt-5.6-luna"


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

