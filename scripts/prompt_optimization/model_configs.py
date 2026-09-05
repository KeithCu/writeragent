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


# Prices and context_length from OpenRouter GET /api/v1/models (2026-08-31).
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
        openrouter_id="google/gemini-3.5-flash-lite",
        display_name="Google: Gemini 3.5 Flash Lite",
        context_window_tokens=1_048_576,
        input_cost_per_million=0.3,
        output_cost_per_million=2.5,
        notes="Google lite Flash for cheap focused subagent tasks.",
    ),
    ModelConfig(
        openrouter_id="google/gemma-4-31b-it",
        display_name="Google: Gemma 4 31B",
        context_window_tokens=262_144,
        input_cost_per_million=0.09,
        output_cost_per_million=0.34,
        notes="Google DeepMind 30.7B dense multimodal model with native tools and reasoning.",
    ),
    ModelConfig(
        openrouter_id="google/gemma-4-26b-a4b-it",
        display_name="Google: Gemma 4 26B A4B",
        context_window_tokens=262_144,
        input_cost_per_million=0.07,
        output_cost_per_million=0.34,
        notes="Google DeepMind 25.2B/3.8B active MoE; near-31B quality at low compute cost.",
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
        openrouter_id="inception/mercury-2.5-preview",
        display_name="Inception: Mercury 2.5 Preview",
        context_window_tokens=260_000,
        input_cost_per_million=0.25,
        output_cost_per_million=0.75,
        notes="Inception reasoning diffusion LLM v2.5; standard pricing ($0.25/$0.75); replaces mercury-2.",
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
        input_cost_per_million=0.3,
        output_cost_per_million=1.2,
        notes="Meta dense 30B multimodal; distilled Spark for consumer-hardware agents.",
    ),
    ModelConfig(
        openrouter_id="meta/muse-spark-1.3-contributor",
        display_name="Meta: Muse Spark 1.3 Contributor",
        context_window_tokens=1_048_576,
        input_cost_per_million=0.1,
        output_cost_per_million=0.2,
        notes="Meta Spark 1.3 contributor tier; replaces spark-1.2-contributor.",
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
        openrouter_id="poolside/laguna-xs-2.1",
        display_name="Poolside: Laguna XS 2.1",
        context_window_tokens=262_144,
        input_cost_per_million=0.06,
        output_cost_per_million=0.12,
        notes="Poolside compact ~30B coding/agent model; cheap fast subagent tier.",
    ),
    ModelConfig(
        openrouter_id="qwen/qwen3.8-27b",
        display_name="Qwen: Qwen3.8 27B",
        context_window_tokens=1_000_000,
        input_cost_per_million=0.425,
        output_cost_per_million=2.55,
        notes="Qwen 3.8 dense 27B VLM; the default-set Qwen (replaces the 3.5 family).",
    ),
    ModelConfig(
        openrouter_id="qwen/qwen3.8-flash",
        display_name="Qwen: Qwen3.8 Flash",
        context_window_tokens=1_000_000,
        input_cost_per_million=0.15,
        output_cost_per_million=0.47,
        notes="Qwen 3.8 Flash 125B/6B-active MoE with 1M context; replaces 3.7 Flash.",
    ),
    ModelConfig(
        openrouter_id="z-ai/glm-5.3-flash",
        display_name="Z.ai: GLM 5.3 Flash",
        context_window_tokens=1_310_720,
        input_cost_per_million=0.075,
        output_cost_per_million=0.25,
        notes="Z.ai GLM 5.3 Flash fast/cheap tier with 1.31M context and tools.",
    ),
    ModelConfig(
        openrouter_id="upstage/solar-pro4",
        display_name="Upstage: Solar Pro 4",
        context_window_tokens=524_288,
        input_cost_per_million=0.03,
        output_cost_per_million=0.12,
        notes="Upstage Solar Pro 4; ultra-low cost document/agent specialist.",
    ),
    ModelConfig(
        openrouter_id="ibm-granite/granite-4.2-8b",
        display_name="IBM: Granite 4.2 8B",
        context_window_tokens=131_072,
        input_cost_per_million=0.1,
        output_cost_per_million=0.15,
        notes="IBM Granite 4.2 8B compact enterprise agent model with native tools.",
    ),
    ModelConfig(
        openrouter_id="mistralai/mistral-small-2603",
        display_name="Mistral: Mistral Small 4",
        context_window_tokens=262_144,
        input_cost_per_million=0.15,
        output_cost_per_million=0.6,
        notes="Mistral Small 4 release with native tool calling and concise output.",
    ),
    ModelConfig(
        openrouter_id="bytedance-seed/seed-2.0-mini",
        display_name="ByteDance Seed: Seed 2.0 Mini",
        context_window_tokens=262_144,
        input_cost_per_million=0.1,
        output_cost_per_million=0.4,
        notes="ByteDance fast cost-efficient 262k context agent model.",
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
        input_cost_per_million=0.065,
        output_cost_per_million=0.18,
        notes="DeepSeek 284B/13B-active MoE Flash; replaces V3.2.",
    ),
]

# Smoke / optimize / run_eval.py student. :nitro is OpenRouter routing
# (same slug as plugin.framework.default_models); pricing uses gpt-oss-120b.
DEFAULT_EVAL_STUDENT_MODEL = "openai/gpt-oss-120b:nitro"
# Teacher for --generate-golds (catalog model; not Sonnet).
DEFAULT_GOLD_MODEL = "openai/gpt-5.6-luna"

# Model IDs that are only used for gold generation, not in default multi-eval sweep.
GOLD_ONLY_MODEL_IDS: frozenset[str] = frozenset()


MODEL_BY_ID: dict[str, ModelConfig] = {m.openrouter_id: m for m in MODELS}

MODEL_ALIASES: dict[str, str] = {
    "inception/mercury-2.5": "inception/mercury-2.5-preview",
}


def get_default_models() -> Sequence[ModelConfig]:
    """
    Return the default ordered list of models for benchmarking.

    Excludes gold-only models so typical multi_eval runs stay cheap.
    """
    return [m for m in MODELS if m.openrouter_id not in GOLD_ONLY_MODEL_IDS]


