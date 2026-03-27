"""Default models for various providers.

Flat catalog: each model has ``ids`` (provider-specific IDs). models are
available for providers listed as keys in the ``ids`` dict.
"""


def resolve_model_id(model, provider):
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


#FIXME, this should be a list, stored with the other endpoint pre-configured params
def get_provider_defaults(provider):
    """Return default models mapped per provider based on boolean flags in DEFAULT_MODELS."""
    if not provider:
        return {}
    defaults = {}
    for model in DEFAULT_MODELS:
        effective_id = resolve_model_id(model, provider)
        if not effective_id:
            continue
        if model.get("default_text") and "text_model" not in defaults:
            defaults["text_model"] = effective_id
        if model.get("default_image") and "image_model" not in defaults:
            defaults["image_model"] = effective_id
        if model.get("default_audio") and "stt_model" not in defaults:
            defaults["stt_model"] = effective_id
    return defaults


DEFAULT_MODELS = [
    # ---- Text models (cross-provider) ------------------------------------

    {
        "display_name": "Mistral Large 3",
        "capability": "text",
        "context_length": 256000,
        "ids": {
            "openrouter": "mistralai/mistral-large-latest",
            "mistral": "mistral-large-latest",
        },
        "default_text": True,
    },
    {
        "display_name": "GPT-OSS 120B",
        "capability": "text",
        "context_length": 128000,
        "ids": {
            "openrouter": "openai/gpt-oss-120b",
            "openai": "gpt-oss-120b",
        },
        "default_text": True,
    },

    # ---- Text models (single provider) -----------------------------------

    {
        "display_name": "Devstral 2",
        "capability": "text",
        "context_length": 128000,
        "ids": {"mistral": "devstral-latest"},
    },

    {
        "display_name": "Gemini 3.1 Flash Lite Preview",
        "capability": "text,image,audio",
        "context_length": 1000000,
        "ids": {"openrouter": "google/gemini-3.1-flash-lite-preview"},
        "default_audio": True,
    },

    # ---- Image-only models -----------------------------------------------

    {
        "display_name": "FLUX.2 Pro",
        "capability": "image",
        "ids": {"together": "black-forest-labs/FLUX.2-pro"},
        "default_image": True,
    },
    {
        "display_name": "Flash Image 2.5",
        "capability": "image",
        "ids": {"together": "google/flash-image-2.5"},
    },
    {
        "display_name": "Qwen Image",
        "capability": "image",
        "ids": {"together": "Qwen/Qwen-Image"},
        "default_image": True,
    },
    {
        "display_name": "Gemini 3 Flash Preview",
        "capability": "image",
        "context_length": 1000000,
        "ids": {"openrouter": "google/gemini-3-flash-preview"},
        "default_text": True,
    },
    {
        "display_name": "Gemini 4.1 Fast",
        "capability": "image",
        "context_length": 1000000,
        "ids": {"openrouter": "x-ai/grok-4.1-fast"},
        "default_text": True,
    },
    {
        "display_name": "Qwen 3.5 27B",
        "capability": "image",
        "context_length": 1000000,
        "ids": {"openrouter": "qwen/qwen3.5-27b"},
        "default_text": True,
    },
    {
        "display_name": "Qwen3.5 397B A17b",
        "capability": "image",
        "context_length": 1000000,
        "ids": {"together": "Qwen/Qwen3.5-397B-A17B"},
        "default_text": True,
    },
    {
        "display_name": "NVIDIA Nemotron 3 Super 120B A12B",
        "capability": "image",
        "context_length": 1000000,
        "ids": {"openrouter": "nvidia/nemotron-3-super-120b-a12b:free"},
        "default_text": True,
    },
    {
        "display_name": "Pixtral Large",
        "capability": "image",
        "context_length": 128000,
        "ids": {
            "openrouter": "mistralai/pixtral-large-latest",
            "mistral": "pixtral-large-latest",
        },
        "default_image": True,
    },
    # ---- Audio/STT models ------------------------------------------------
    {
        "display_name": "Whisper Large v3",
        "capability": "audio",
        "ids": {
            "together": "openai/whisper-large-v3",
        },
        "default_audio": True,
    },
    {
        "display_name": "Voxtral Mini",
        "capability": "audio",
        "ids": {"mistral": "voxtral-mini-2602"},
        "default_audio": True,
    },
]

