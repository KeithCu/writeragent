"""Default models for various providers.

Flat catalog: each model has ``ids`` (provider-specific IDs). models are
available for providers listed as keys in the ``ids`` dict.
"""

from typing import Any
from plugin.framework.types import ModelCapability


def resolve_model_id(model: dict[str, Any], provider):
    """Resolve the effective model ID for a given provider.

    Args:
        model: model dict with an ``ids`` field (mapping provider -> ID).
        provider: provider key (e.g. ``"openrouter"``, ``"ollama"``).

    Returns:
        The resolved model ID string, or None if the model is not
        available for this provider.
    """
    ids = model.get("ids", {})
    return ids.get(provider)


# FIXME, this should be a list, stored with the other endpoint pre-configured params
def get_provider_defaults(provider):
    """Return default models mapped per provider based on boolean flags in DEFAULT_MODELS."""
    if not provider:
        return {}
    defaults = {}
    for model in DEFAULT_MODELS:
        effective_id = resolve_model_id(model, provider)
        if not effective_id:
            continue

        # Capability check using bitmasks
        caps = model.get("capability", ModelCapability.NONE)

        if (caps & ModelCapability.CHAT) and "text_model" not in defaults:
            if model.get("default_text"):
                defaults["text_model"] = effective_id
        if (caps & ModelCapability.IMAGE) and "image_model" not in defaults:
            if model.get("default_image"):
                defaults["image_model"] = effective_id
        if (caps & ModelCapability.AUDIO) and "stt_model" not in defaults:
            if model.get("default_audio"):
                defaults["stt_model"] = effective_id

    # Fallback to first available if no explicit default was flagged
    for model in DEFAULT_MODELS:
        effective_id = resolve_model_id(model, provider)
        if not effective_id:
            continue
        caps = model.get("capability", ModelCapability.NONE)
        if (caps & ModelCapability.CHAT) and "text_model" not in defaults:
            defaults["text_model"] = effective_id
        if (caps & ModelCapability.IMAGE) and "image_model" not in defaults:
            defaults["image_model"] = effective_id
        if (caps & ModelCapability.AUDIO) and "stt_model" not in defaults:
            defaults["stt_model"] = effective_id

    return defaults


DEFAULT_MODELS: list[dict[str, Any]] = [
    {"display_name": "DeepSeek V3", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 163840, "ids": {"deepseek": "deepseek-chat"}, "default_text": True},
    {"display_name": "MiniMax M2.7", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 197000, "ids": {"together": "MiniMaxAI/MiniMax-M2.7"}, "default_text": True},
    {
        "display_name": "GPT-OSS 120B",
        "capability": ModelCapability.CHAT | ModelCapability.TOOLS,
        "context_length": 131072,
        "ids": {"together": "openai/gpt-oss-120b", "openrouter": "openai/gpt-oss-120b"},
        "default_text": True,
    },
    {"display_name": "GPT-OSS 20B", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 128000, "ids": {"together": "openai/gpt-oss-20b"}, "default_text": True},
    {
        "display_name": "Mistral Large 3",
        "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS,
        "context_length": 262144,
        "ids": {"openrouter": "mistralai/mistral-large-2512", "mistral": "mistral-large-latest"},
    },
    {
        "display_name": "Gemini 3.1 Flash Lite Preview",
        "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS,
        "context_length": 1048576,
        "ids": {"google": "gemini-3.1-flash-lite-preview", "openrouter": "google/gemini-3.1-flash-lite-preview"},
        "default_audio": True,
    },
    {"display_name": "Gemini Flash Image 2.5", "capability": ModelCapability.IMAGE, "ids": {"together": "google/flash-image-2.5"}, "default_image": True},
    {"display_name": "Voxtral Mini 3B", "capability": ModelCapability.AUDIO, "ids": {"together": "mistralai/Voxtral-Mini-3B-2507"}, "default_audio": True},
]
