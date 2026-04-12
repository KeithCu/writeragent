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
    {
        "display_name": "DeepSeek V3",
        "capability": ModelCapability.CHAT | ModelCapability.TOOLS,
        "context_length": 163840,
        "ids": {
            "deepseek": "deepseek-chat",
            "openrouter": "deepseek/deepseek-chat"
        },
        "default_text": True
    },
    {
        "display_name": "Llama 3.3 70B",
        "capability": ModelCapability.CHAT | ModelCapability.TOOLS,
        "context_length": 131072,
        "ids": {
            "groq": "llama-3.3-70b-versatile",
            "openrouter": "meta-llama/llama-3.3-70b-instruct",
            "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo"
        },
        "default_text": True
    },
    {
        "display_name": "Mistral Large 3",
        "capability": ModelCapability.CHAT | ModelCapability.TOOLS,
        "context_length": 128000,
        "ids": {
            "openrouter": "mistralai/mistral-large-latest",
            "mistral": "mistral-large-latest"
        }
    },
    {
        "display_name": "Gemini 3.1 Flash Preview",
        "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS,
        "context_length": 1048576,
        "ids": {
            "google": "gemini-3.1-flash-preview",
            "openrouter": "google/gemini-3.1-flash-lite-preview"
        },
        "default_audio": True
    },
    {
        "display_name": "Claude 3.5 Sonnet",
        "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS,
        "context_length": 200000,
        "ids": {
            "anthropic": "claude-3-5-sonnet-20241022",
            "openrouter": "anthropic/claude-3.5-sonnet"
        }
    },
    {
        "display_name": "FLUX.2 Pro",
        "capability": ModelCapability.IMAGE,
        "ids": {
            "together": "black-forest-labs/FLUX.2-pro"
        },
        "default_image": True
    },
    {
        "display_name": "Pixtral Large",
        "capability": ModelCapability.CHAT | ModelCapability.IMAGE | ModelCapability.VISION,
        "context_length": 128000,
        "ids": {
            "openrouter": "mistralai/pixtral-large-latest",
            "mistral": "pixtral-large-latest"
        }
    },
    {
        "display_name": "Whisper Large v3",
        "capability": ModelCapability.AUDIO,
        "ids": {
            "together": "openai/whisper-large-v3",
            "groq": "whisper-large-v3"
        },
        "default_audio": True
    }
]
