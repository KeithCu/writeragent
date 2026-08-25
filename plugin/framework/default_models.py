# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Default models for various providers.

Flat catalog: each model has ``ids`` (provider-specific IDs). models are
available for providers listed as keys in the ``ids`` dict.
"""

from typing import Any
from plugin.framework.constants import ModelCapability


from plugin.framework.deal_shim import DEAL_MAX_TOKEN, str_bounded, deal


@deal.post(lambda result: result is None or isinstance(result, str))
def resolve_model_id(model: dict[str, Any], provider):
    """Resolve the effective model ID for a given provider.

    Args:
        model: model dict with an ``ids`` field (mapping provider -> ID).
        provider: provider key (e.g. ``"openrouter"``, ``"ollama"``).

    Returns:
        The resolved model ID string, or None if the model is not
        available for this provider.
    """
    # CrossHair TypeError on typing.Literal['a','b','rc'] when proxying ids.get (FV §8.1 D).
    # crosshair: off
    ids = model.get("ids", {})
    if not isinstance(ids, dict):
        return None
    resolved = ids.get(provider)
    # Catalog rows must map provider → str. CrossHair can put () in ids.release.
    return resolved if isinstance(resolved, str) else None


# FIXME, this should be a list, stored with the other endpoint pre-configured params
@deal.pre(lambda provider: not provider or str_bounded(provider, DEAL_MAX_TOKEN))
@deal.post(lambda result: isinstance(result, dict))
def get_provider_defaults(provider):
    """Return default models mapped per provider based on boolean flags in DEFAULT_MODELS."""
    if not provider:
        return {}
    # Local runtimes share bare-name conventions with Ollama; no separate catalog rows yet.
    if provider == "lmstudio":
        provider = "ollama"
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
    {"display_name": "Free Models (Auto)", "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 131072, "ids": {"openrouter": "openrouter/free"}},
    {"display_name": "DeepSeek V3", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 163840, "ids": {"deepseek": "deepseek-chat"}, "default_text": True},
    {"display_name": "DeepSeek V4 Flash", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 163840, "ids": {"together": "deepseek-ai/DeepSeek-V4-Flash-0731"}},
    {"display_name": "MiniMax M3", "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1000000, "ids": {"together": "MiniMaxAI/MiniMax-M3"}, "default_text": True},
    {"display_name": "GPT-OSS 120B", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 131072, "ids": {"together": "openai/gpt-oss-120b", "openrouter": "openai/gpt-oss-120b:nitro", "groq": "openai/gpt-oss-120b"}, "default_text": True},
    {"display_name": "GPT-OSS 20B", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 128000, "ids": {"together": "openai/gpt-oss-20b", "groq": "openai/gpt-oss-20b"}, "default_text": True},
    {"display_name": "Mistral Large 3", "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 262144, "ids": {"openrouter": "mistralai/mistral-large-2512", "mistral": "mistral-large-latest"}},
    {"display_name": "Voxtral Mini Transcribe", "capability": ModelCapability.AUDIO, "ids": {"openrouter": "mistralai/voxtral-mini-transcribe"}, "default_audio": True},
    {"display_name": "Gemini 3.1 Flash Lite Preview", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1048576, "ids": {"google": "gemini-3.1-flash-lite-preview", "openrouter": "google/gemini-3.1-flash-lite-preview"}},
    {"display_name": "Gemini 3.1 Flash Lite", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1048576, "ids": {"google": "gemini-3.1-flash-lite", "openrouter": "google/gemini-3.1-flash-lite"}},
    {"display_name": "Gemini 3.1 Pro", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1048576, "ids": {"google": "gemini-3.1-pro", "openrouter": "google/gemini-3.1-pro"}},
    {"display_name": "Gemini Flash Image 2.5", "capability": ModelCapability.IMAGE, "ids": {"together": "google/flash-image-2.5"}, "default_image": True},
    {"display_name": "Gemini 2.5 Flash Image", "capability": ModelCapability.IMAGE, "ids": {"openrouter": "google/gemini-2.5-flash-image"}, "default_image": True},
    {"display_name": "Nvidia Parakeet TDT 0.6B v3", "capability": ModelCapability.AUDIO, "ids": {"together": "nvidia/parakeet-tdt-0.6b-v3"}, "default_audio": True},
    {"display_name": "GLM 5.2", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 200000, "ids": {"zai": "glm-5.2"}, "default_text": True},
    {"display_name": "GLM ASR 2512", "capability": ModelCapability.AUDIO, "ids": {"zai": "glm-asr-2512"}, "default_audio": True},
]
