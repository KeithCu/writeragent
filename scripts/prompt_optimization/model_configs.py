from __future__ import annotations

"""
Model definitions for multi-model DSPy/OpenRouter benchmarking.

Each model is identified by its **openrouter_id** (e.g. openai/gpt-oss-120b).
Prices are in USD per 1M tokens.
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


MODELS: list[ModelConfig] = [
    ModelConfig(
        openrouter_id="openai/gpt-oss-120b",
        display_name="OpenAI: gpt-oss-120b",
        context_window_tokens=131_000,
        input_cost_per_million=0.039,
        output_cost_per_million=0.19,
        notes=(
            "117B-parameter MoE; 5.1B activated per forward; MXFP4; "
            "high-reasoning, agentic, and general-purpose; native tools."
        ),
    ),
    ModelConfig(
        openrouter_id="openai/gpt-5-nano",
        display_name="OpenAI: GPT-5 Nano",
        context_window_tokens=400_000,
        input_cost_per_million=0.05,
        output_cost_per_million=0.40,
        notes=(
            "Smallest / fastest GPT-5 variant; optimized for dev tools and "
            "ultra-low latency; successor to GPT-4.1-nano."
        ),
    ),
    ModelConfig(
        openrouter_id="google/gemini-3-flash-preview",
        display_name="Google: Gemini 3 Flash",
        context_window_tokens=1_050_000,
        input_cost_per_million=0.10,
        output_cost_per_million=0.40,
        notes=(
            "Latest Gemini Flash; fast multimodal with strong coding and "
            "function-calling."
        ),
    ),
    ModelConfig(
        openrouter_id="anthropic/claude-haiku-4.5",
        display_name="Anthropic: Claude Haiku 4.5",
        context_window_tokens=200_000,
        input_cost_per_million=1.00,
        output_cost_per_million=5.00,
        notes=(
            "Fast / efficient Claude with near-frontier intelligence; strong "
            "coding and tools; extended thinking and controllable reasoning."
        ),
    ),
    ModelConfig(
        openrouter_id="z-ai/glm-4.7",
        display_name="Z.ai: GLM 4.7",
        context_window_tokens=203_000,
        input_cost_per_million=0.30,
        output_cost_per_million=1.40,
        notes=(
            "Latest flagship GLM with enhanced programming capabilities and "
            "more stable multi-step reasoning / agent execution."
        ),
    ),
    ModelConfig(
        openrouter_id="minimax/minimax-m2.1",
        display_name="MiniMax: MiniMax M2.1",
        context_window_tokens=197_000,
        input_cost_per_million=0.27,
        output_cost_per_million=0.95,
        notes=(
            "Lightweight model optimized for coding and agentic workflows; "
            "10B activated parameters; strong multilingual coding benchmarks."
        ),
    ),
    ModelConfig(
        openrouter_id="anthropic/claude-sonnet-4.6",
        display_name="Anthropic: Claude Sonnet 4.6",
        context_window_tokens=200_000,
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
        notes="Sonnet 4.6; used for gold standard generation.",
    ),
    ModelConfig(
        openrouter_id="x-ai/grok-4.1-fast",
        display_name="xAI: Grok 4.1 Fast",
        context_window_tokens=2_000_000,
        input_cost_per_million=0.20,
        output_cost_per_million=0.50,
        notes=(
            "Grok 4.1 Fast; 2M-token context; $0.20/M input, $0.50/M output."
        ),
    ),
    ModelConfig(
        openrouter_id="qwen/qwen3.5-35b-a3b",
        display_name="Qwen: Qwen3.5-35B-A3B",
        context_window_tokens=262_000,
        input_cost_per_million=0.25,
        output_cost_per_million=2.00,
        notes="Vision-language MoE; linear attention; comparable to Qwen3.5-27B.",
    ),
    ModelConfig(
        openrouter_id="qwen/qwen3.5-27b",
        display_name="Qwen: Qwen3.5-27B",
        context_window_tokens=262_000,
        input_cost_per_million=0.30,
        output_cost_per_million=2.40,
        notes="Dense VLM with linear attention; comparable to Qwen3.5-122B-A10B.",
    ),
    ModelConfig(
        openrouter_id="qwen/qwen3.5-122b-a10b",
        display_name="Qwen: Qwen3.5-122B-A10B",
        context_window_tokens=262_000,
        input_cost_per_million=0.40,
        output_cost_per_million=3.20,
        notes="VLM MoE; second to Qwen3.5-397B-A17B; strong text and visual.",
    ),
#    ModelConfig(
#        openrouter_id="qwen/qwen3.5-flash",
#        display_name="Qwen: Qwen3.5-Flash",
#        context_window_tokens=1_000_000,
#        input_cost_per_million=0.10,
#        output_cost_per_million=0.40,
#        notes="Flash VLM; fast, efficient; leap over 3 series for text and multimodal.",
#    ),
#    ModelConfig(
#        openrouter_id="liquid-ai/lfm2-24b-a2b",
#        display_name="LiquidAI: LFM2-24B-A2B",
#        context_window_tokens=33_000,
#        input_cost_per_million=0.03,
#        output_cost_per_million=0.12,
#        notes="24B MoE, 2B active; on-device; 32 GB RAM; high quality, low cost.",
#    ),
    ModelConfig(
        openrouter_id="allenai/olmo-3.1-32b-instruct",
        display_name="AllenAI: Olmo 3.1 32B Instruct",
        context_window_tokens=65_536,
        input_cost_per_million=0.20,
        output_cost_per_million=0.60,
        notes="Released Jan 6, 2026; 65K context.",
    ),
    ModelConfig(
        openrouter_id="nvidia/nemotron-3-nano-30b-a3b",
        display_name="NVIDIA: Nemotron 3 Nano 30B A3B",
        context_window_tokens=262_000,
        input_cost_per_million=0.05,
        output_cost_per_million=0.20,
        notes="Small MoE; open-weights; agentic AI; high compute efficiency.",
    ),
    ModelConfig(
        openrouter_id="nvidia/nemotron-3-super-120b-a12b:free",
        display_name="NVIDIA: Nemotron 3 Super 120B A12B (Free Tier)",
        context_window_tokens=262_000,
        input_cost_per_million=0.20,
        output_cost_per_million=0.80,
        notes=(
            "Large Nemotron 3 Super 120B variant on OpenRouter; "
            "configured here with approximate list pricing so it can "
            "participate in intelligence-per-dollar benchmarking."
        ),
    ),
    ModelConfig(
        openrouter_id="nvidia/llama-3.3-nemotron-super-49b-v1.5",
        display_name="NVIDIA: Llama 3.3 Nemotron Super 49B V1.5",
        context_window_tokens=131_072,
        input_cost_per_million=0.10,
        output_cost_per_million=0.40,
        notes=(
            "49B reasoning/chat model derived from Llama-3.3-70B-Instruct; "
            "128K context; optimized for agentic workflows, RAG, and tool use."
        ),
    ),
    ModelConfig(
        openrouter_id="mistralai/devstral-2512",
        display_name="Mistral: Devstral 2 2512",
        context_window_tokens=262_144,
        input_cost_per_million=0.40,
        output_cost_per_million=2.00,
        notes="Released Dec 9, 2025; 262K context.",
    ),
    ModelConfig(
        openrouter_id="nex-agi/deepseek-v3.1-nex-n1",
        display_name="Nex AGI: DeepSeek V3.1 Nex N1",
        context_window_tokens=131_072,
        input_cost_per_million=0.27,
        output_cost_per_million=1.00,
        notes="Released Dec 8, 2025; 131K context.",
    ),
]


MODEL_BY_ID: dict[str, ModelConfig] = {m.openrouter_id: m for m in MODELS}


def get_default_models() -> Sequence[ModelConfig]:
    """
    Return the default ordered list of models for benchmarking.

    This is where you can adjust which models participate in multi-model
    sweeps without touching other code.
    """

    return MODELS

