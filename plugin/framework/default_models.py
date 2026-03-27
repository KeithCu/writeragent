"""Default models for various providers.

Flat catalog: each model has ``ids`` (provider-specific IDs) and/or ``id``
(fallback). Rules:

- ``ids`` present: model available ONLY for providers listed as keys.
- ``ids`` absent, ``id`` present: model available for ALL providers (transversal).
- ``id`` is also the fallback when ``ids`` doesn't contain the requested provider.
"""


def resolve_model_id(model, provider):
    """Resolve the effective model ID for a given provider.

    Args:
        model: model dict with optional ``ids`` and ``id`` fields.
        provider: provider key (e.g. ``"openrouter"``, ``"ai_ollama"``).

    Returns:
        The resolved model ID string, or None if the model is not
        available for this provider.
    """
    ids = model.get("ids")
    if ids:
        if provider in ids:
            return ids[provider]
        # Model has ids but doesn't list this provider -> not available
        return None
    # No ids -> use fallback id, available for all providers
    return model.get("id")

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
def merge_catalogs(base, extension):
    """Merge extension models into a base catalog (flat list).

    Deduplicates by ``id`` (fallback ID). Models already present in *base*
    are skipped.

    Args:
        base: list of model dicts (flat catalog).
        extension: list of model dicts to merge.

    Returns:
        Updated *base* list (also mutated in-place).
    """
    existing_ids = {m.get("id") for m in base if m.get("id")}
    for model in extension:
        if not isinstance(model, dict):
            continue
        mid = model.get("id")
        if not mid and not model.get("ids"):
            continue
        if mid and mid in existing_ids:
            continue
        base.append(model)
        if mid:
            existing_ids.add(mid)
    return base


DEFAULT_MODELS = [
    # ---- Text models (cross-provider) ------------------------------------

    {
        "display_name": "Llama 3.3 70B Instruct",
        "capability": "text",
        "context_length": 128000,
        "priority": 9,
        "notes": "Strong generalist, good tool calling, open weights (Meta)",
        "ids": {
            "openrouter": "meta-llama/llama-3.3-70b-instruct",
            "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "ollama": "llama3.3:70b-instruct",
        },
        "id": "meta-llama/llama-3.3-70b-instruct",
        "default_text": True,
    },
    {
        "display_name": "Mistral Large 3",
        "capability": "text",
        "context_length": 256000,
        "priority": 10,
        "notes": "Top open-weight multimodal, agentic & tool strong (Mistral AI, French)",
        "ids": {
            "openrouter": "mistralai/mistral-large-latest",
            "mistral": "mistral-large-latest",
        },
        "id": "mistral-large-latest",
        "default_text": True,
    },
    {
        "display_name": "GPT-OSS 120B",
        "capability": "text",
        "context_length": 128000,
        "priority": 8,
        "notes": "Open-weight OpenAI model, solid tool use & reasoning",
        "ids": {
            "openrouter": "openai/gpt-oss-120b",
            "openai": "gpt-oss-120b",
        },
        "id": "gpt-oss-120b",
        "default_text": True,
    },
    {
        "display_name": "Mistral 7B Instruct v0.3",
        "capability": "text",
        "context_length": 32768,
        "priority": 8,
        "notes": "Speed demon, reliable function calling (Mistral)",
        "ids": {
            "together": "mistralai/Mistral-7B-Instruct-v0.3",
            "ollama": "mistral:7b-instruct-v0.3",
        },
        "id": "mistralai/Mistral-7B-Instruct-v0.3",
    },

    # ---- Text models (single provider) -----------------------------------

    {
        "display_name": "Gemma 3 27B Instruct",
        "capability": "text,image",
        "context_length": 131072,
        "priority": 8,
        "notes": "Fast, efficient, excellent instruction following (Google)",
        "ids": {"openrouter": "google/gemma-3-27b-it"},
        "id": "google/gemma-3-27b-it",
    },
    {
        "display_name": "Granite 4.0 8B Instruct",
        "capability": "text",
        "context_length": 128000,
        "priority": 7,
        "notes": "Enterprise-tuned, improved tool calling & reasoning (IBM)",
        "ids": {
            "openrouter": "ibm/granite-4.0-8b-instruct"
        },
        "id": "ibm/granite-4.0-8b-instruct",
    },
    {
        "display_name": "Nemotron Super 49B",
        "capability": "text",
        "context_length": 131072,
        "priority": 7,
        "notes": "Tool-augmented specialist, strong RAG/agent (NVIDIA)",
        "ids": {"openrouter": "nvidia/llama-3.3-nemotron-super-49b-v1.5"},
        "id": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    },
    {
        "display_name": "Granite 3.1 8B Instruct",
        "capability": "text",
        "context_length": 128000,
        "priority": 7,
        "notes": "Enterprise reasoning & tool tuned (IBM)",
        "ids": {"together": "ibm/granite-3.1-8b-instruct"},
        "id": "ibm/granite-3.1-8b-instruct",
    },
    {
        "display_name": "Qwen 2.5 32B (local)",
        "capability": "text",
        "context_length": 32768,
        "priority": 9,
        "notes": "Excellent tool calling, multilingual, fits 24GB VRAM",
        "ids": {"ollama": "qwen2.5:32b"},
        "id": "qwen2.5:32b",
    },
    {
        "display_name": "Granite 3.2 8B (local)",
        "capability": "text",
        "context_length": 128000,
        "priority": 7,
        "notes": "IBM enterprise-tuned, tool improvements",
        "ids": {"ollama": "granite3.2:8b"},
        "id": "granite3.2:8b",
    },
    {
        "display_name": "Gemma 2 27B Instruct (local)",
        "capability": "text",
        "context_length": 8192,
        "priority": 7,
        "notes": "Efficient Google model, strong instruction",
        "ids": {"ollama": "gemma2:27b-instruct"},
        "id": "gemma2:27b-instruct",
    },
    {
        "display_name": "Devstral 2",
        "capability": "text",
        "context_length": 128000,
        "priority": 9,
        "notes": "Coding/agent specialist",
        "ids": {"mistral": "devstral-latest"},
        "id": "devstral-latest",
    },

    # ---- Text + Image models ---------------------------------------------

    {
        "display_name": "Gemini 3.1 Flash Lite Preview",
        "capability": "text,image,audio",
        "context_length": 1000000,
        "priority": 10,
        "notes": "Latest Google model, optimized for speed and low cost",
        "ids": {"openrouter": "google/gemini-3.1-flash-lite-preview"},
        "id": "google/gemini-3.1-flash-lite-preview",
        "default_audio": True,
    },

    # ---- Image-only models -----------------------------------------------

    {
        "display_name": "FLUX.2 Pro",
        "capability": "image",
        "priority": 9,
        "notes": "Black Forest Labs image generation (Together AI)",
        "ids": {"together": "black-forest-labs/FLUX.2-pro"},
        "id": "black-forest-labs/FLUX.2-pro",
        "default_image": True,
    },
    {
        "display_name": "Flash Image 2.5",
        "capability": "image",
        "priority": 9,
        "notes": "Google image generation (Together AI)",
        "ids": {"together": "google/flash-image-2.5"},
        "id": "google/flash-image-2.5",
    },
    {
        "display_name": "Qwen Image",
        "capability": "image",
        "priority": 8,
        "notes": "Qwen image generation (Together AI)",
        "ids": {"together": "Qwen/Qwen-Image"},
        "id": "Qwen/Qwen-Image",
    },
    {
        "display_name": "Gemini 3.1 Pro Preview",
        "capability": "image",
        "context_length": 1000000,
        "priority": 9,
        "notes": "Excellent vision + reasoning, 1M context (Google)",
        "ids": {"openrouter": "google/gemini-3.1-pro-preview"},
        "id": "google/gemini-3.1-pro-preview",
        "default_image": True,
    },
    {
        "display_name": "Pixtral Large",
        "capability": "image",
        "context_length": 128000,
        "priority": 8,
        "notes": "Multimodal flagship, strong image understanding (Mistral)",
        "ids": {
            "openrouter": "mistralai/pixtral-large-latest",
            "mistral": "pixtral-large-latest",
        },
        "id": "pixtral-large-latest",
        "default_image": True,
    },
    {
        "display_name": "LLaVA (vision local)",
        "capability": "image",
        "context_length": 4096,
        "priority": 8,
        "notes": "Classic local multimodal",
        "ids": {"ollama": "llava"},
        "id": "llava",
        "default_image": True,
    },
    # ---- Audio/STT models ------------------------------------------------
    {
        "display_name": "Whisper-1",
        "capability": "audio",
        "priority": 10,
        "notes": "Standard OpenAI transcription model",
        "ids": {"openai": "whisper-1"},
        "id": "whisper-1",
        "default_audio": True,
    },
    {
        "display_name": "Whisper Large v3",
        "capability": "audio",
        "priority": 9,
        "notes": "High quality open-weight transcription (Together/Groq)",
        "ids": {
            "together": "openai/whisper-large-v3",
        },
        "id": "openai/whisper-large-v3",
        "default_audio": True,
    },
    {
        "display_name": "Voxtral Mini",
        "capability": "audio",
        "priority": 10,
        "notes": "Native audio model for Mistral",
        "ids": {"mistral": "voxtral-mini-2602"},
        "id": "voxtral-mini-2602",
        "default_audio": True,
    },
]
