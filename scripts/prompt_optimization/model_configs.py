from __future__ import annotations

"""
Model definitions for multi-model DSPy/OpenRouter benchmarking.

Each model is identified by its **openrouter_id** (e.g. openai/gpt-oss-120b).
Prices are in USD per 1M tokens (OpenRouter ``pricing.prompt`` /
``pricing.completion`` × 1e6). Context windows are ``context_length``.
"""

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class ModelConfig:
    """
    Static metadata for an LLM model used in benchmarking.

    - openrouter_id: OpenRouter model slug (the single identifier for API and CLI).
    - display_name: human-readable label for logs / tables.
    - context_window_tokens: advertised maximum context window (tokens).
    - input_cost_per_million: price for 1M input tokens (USD).
    - output_cost_per_million: price for 1M output tokens (USD).
    - notes: optional description.
    """

    openrouter_id: str
    display_name: str
    context_window_tokens: Optional[int]
    input_cost_per_million: float
    output_cost_per_million: float
    notes: Optional[str] = None


# Prices and context_length from OpenRouter GET /api/v1/models (2026-08-24).
# Default sweep is US/small-leaning plus China pack; gold-only is excluded
# from get_default_models() but stays in MODELS for --gold-model.
MODELS: list[ModelConfig] = [
    ModelConfig(
        openrouter_id="openai/gpt-oss-120b",
        display_name="OpenAI: gpt-oss-120b",
        context_window_tokens=131_072,
        input_cost_per_million=0.037,
        output_cost_per_million=0.17,
        notes="OpenAI open-weight 117B MoE (5.1B active); high-reasoning agentic default.",
    ),
    ModelConfig(
        openrouter_id="openai/gpt-oss-20b",
        display_name="OpenAI: gpt-oss-20b",
        context_window_tokens=131_072,
        input_cost_per_million=0.03,
        output_cost_per_million=0.13,
        notes="OpenAI open-weight 21B MoE (3.6B active); cheap small sibling of 120B.",
    ),
    ModelConfig(
        openrouter_id="openai/gpt-5.6-luna",
        display_name="OpenAI: GPT-5.6 Luna",
        context_window_tokens=1_050_000,
        input_cost_per_million=0.2,
        output_cost_per_million=1.2,
        notes="OpenAI GPT-5.6 fast/cheap tier for latency-sensitive agent work.",
    ),
    ModelConfig(
        openrouter_id="google/gemini-3.7-flash",
        display_name="Google: Gemini 3.7 Flash",
        context_window_tokens=1_048_576,
        input_cost_per_million=0.375,
        output_cost_per_million=1.875,
        notes="Google Flash for fast agentic/coding work; replaces Gemini 3 Flash Preview.",
    ),
    ModelConfig(
        openrouter_id="google/gemini-3.5-flash-lite",
        display_name="Google: Gemini 3.5 Flash Lite",
        context_window_tokens=1_048_576,
        input_cost_per_million=0.3,
        output_cost_per_million=2.5,
        notes="Google lite Flash for cheap focused subagent tasks.",
    ),
    ModelConfig(
        openrouter_id="nvidia/nemotron-3-nano-30b-a3b",
        display_name="NVIDIA: Nemotron 3 Nano 30B A3B",
        context_window_tokens=262_144,
        input_cost_per_million=0.05,
        output_cost_per_million=0.2,
        notes="NVIDIA small MoE; paid (not :free) high-efficiency agentic.",
    ),
    ModelConfig(
        openrouter_id="nvidia/nemotron-3.5-lightning",
        display_name="NVIDIA: Nemotron 3.5 Lightning",
        context_window_tokens=262_144,
        input_cost_per_million=0.08,
        output_cost_per_million=0.2,
        notes="NVIDIA 30B/3B-active MoE; paid replacement for the Super :free slot.",
    ),
    ModelConfig(
        openrouter_id="inception/mercury-2",
        display_name="Inception: Mercury 2",
        context_window_tokens=128_000,
        input_cost_per_million=0.25,
        output_cost_per_million=0.75,
        notes="Inception reasoning diffusion LLM; native tools, very high throughput.",
    ),
    ModelConfig(
        openrouter_id="x-ai/grok-4.6",
        display_name="SpaceXAI: Grok 4.6",
        context_window_tokens=500_000,
        input_cost_per_million=2.0,
        output_cost_per_million=6.0,
        notes="SpaceXAI Grok 4.6; replaces retired grok-4.1-fast (4.6 only).",
    ),
    ModelConfig(
        openrouter_id="meta/muse-glimmer-30b",
        display_name="Meta: Muse Glimmer 30B",
        context_window_tokens=131_072,
        input_cost_per_million=0.35,
        output_cost_per_million=1.5,
        notes="Meta dense 30B multimodal; distilled Spark for consumer-hardware agents.",
    ),
    ModelConfig(
        openrouter_id="meta/muse-spark-1.2-contributor",
        display_name="Meta: Muse Spark 1.2 Contributor",
        context_window_tokens=1_048_576,
        input_cost_per_million=0.1,
        output_cost_per_million=0.2,
        notes="Meta Spark 1.2 contributor tier; cheap 1M-context reasoning.",
    ),
    ModelConfig(
        openrouter_id="poolside/laguna-s-2.1",
        display_name="Poolside: Laguna S 2.1",
        context_window_tokens=1_048_576,
        input_cost_per_million=0.09,
        output_cost_per_million=0.18,
        notes="Poolside 118B/8B-active coding agent; paid (not :free).",
    ),
    ModelConfig(
        openrouter_id="qwen/qwen3.8-27b",
        display_name="Qwen: Qwen3.8 27B",
        context_window_tokens=1_000_000,
        input_cost_per_million=0.4,
        output_cost_per_million=3.0,
        notes="Qwen 3.8 dense 27B VLM; the default-set Qwen (replaces the 3.5 family).",
    ),
    ModelConfig(
        openrouter_id="z-ai/glm-5.3",
        display_name="Z.ai: GLM 5.3",
        context_window_tokens=1_048_576,
        input_cost_per_million=1.4,
        output_cost_per_million=4.4,
        notes="Z.ai flagship GLM for long-horizon software/agent work; replaces 5.1.",
    ),
    ModelConfig(
        openrouter_id="minimax/minimax-m3",
        display_name="MiniMax: MiniMax M3",
        context_window_tokens=1_048_576,
        input_cost_per_million=0.3,
        output_cost_per_million=1.2,
        notes="MiniMax multimodal 1M-context agent/coding model; replaces M2.7.",
    ),
    ModelConfig(
        openrouter_id="deepseek/deepseek-v4-flash-0731",
        display_name="DeepSeek: DeepSeek V4 Flash 0731",
        context_window_tokens=1_310_720,
        input_cost_per_million=0.14,
        output_cost_per_million=0.28,
        notes="DeepSeek 284B/13B-active MoE Flash; replaces V3.2.",
    ),
    ModelConfig(
        openrouter_id="qwen/qwen3.7-flash",
        display_name="Qwen: Qwen3.7 Flash",
        context_window_tokens=1_000_000,
        input_cost_per_million=0.03,
        output_cost_per_million=0.13,
        notes="Qwen cheap Flash VLM; extra small China Qwen besides 27B.",
    ),
    ModelConfig(
        openrouter_id="anthropic/claude-sonnet-4.6",
        display_name="Anthropic: Claude Sonnet 4.6",
        context_window_tokens=1_000_000,
        input_cost_per_million=3.0,
        output_cost_per_million=15.0,
        notes=(
            "Gold-only: use for --gold-model when generating gold standards; "
            "excluded from default multi-model eval (too expensive for repeated runs)."
        ),
    ),
]

# Model IDs that are only used for gold generation, not in default multi-eval sweep.
GOLD_ONLY_MODEL_IDS: frozenset[str] = frozenset({"anthropic/claude-sonnet-4.6"})


MODEL_BY_ID: dict[str, ModelConfig] = {m.openrouter_id: m for m in MODELS}


def get_default_models() -> Sequence[ModelConfig]:
    """
    Return the default ordered list of models for benchmarking.

    Excludes gold-only models (e.g. Claude Sonnet) so typical multi_eval runs
    stay cheap. Use --gold-model anthropic/claude-sonnet-4.6 when generating
    golds; that model is in MODEL_BY_ID but not in this list.
    """
    return [m for m in MODELS if m.openrouter_id not in GOLD_ONLY_MODEL_IDS]
