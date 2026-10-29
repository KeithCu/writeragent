# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Default models for various providers.

Flat catalog: each model has ``ids`` (provider-specific IDs). models are
available for providers listed as keys in the ``ids`` dict.
"""

from __future__ import annotations

from typing import Any
from plugin.framework.constants import ModelCapability
from plugin.framework.client.provider_detection import get_provider_from_endpoint
from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent


from plugin.framework.deal_shim import DEAL_MAX_TOKEN, str_bounded, deal


@deal.post(lambda result: result is None or isinstance(result, str))
def resolve_model_id(model: dict[str, Any], provider: str | None) -> str | None:
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
def get_provider_defaults(provider: str | None) -> dict[str, str]:
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
        if (caps & ModelCapability.AUDIO) and "tts_model" not in defaults:
            if model.get("default_tts"):
                defaults["tts_model"] = effective_id

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
        # CHAT|AUDIO rows with neither stt nor tts flag (Gemini 3.1 Flash Lite
        # Preview, writeragent-mock) used to become both speech defaults: any
        # AUDIO row that was not explicitly TTS was treated as STT, and any
        # that was not explicitly STT was treated as TTS. Skip CHAT rows in
        # this fallback only. The first loop's default_audio / default_tts
        # flags are unchanged.
        if not (caps & ModelCapability.CHAT):
            if (caps & ModelCapability.AUDIO) and "stt_model" not in defaults:
                # TTS rows (default_tts / tts) are not speech-to-text fallbacks.
                if not model.get("default_tts") and not model.get("tts"):
                    defaults["stt_model"] = effective_id
            if (caps & ModelCapability.AUDIO) and "tts_model" not in defaults:
                # STT rows (default_audio / stt) are not text-to-speech fallbacks.
                if not model.get("default_audio") and not model.get("stt"):
                    defaults["tts_model"] = effective_id

    return defaults


def merge_provider_default_models(to_show: list[str], provider: str, req_cap: str) -> None:
    """Append curated default model ids for this provider/capability."""
    mask = _COMBO_CAP.get(req_cap)
    if mask is None:
        return
    for m in DEFAULT_MODELS:
        capability = m.get("capability", ModelCapability.CHAT)
        if not (capability & mask):
            continue
        effective_id = resolve_model_id(m, provider)
        if not effective_id:
            continue
        # stt marks non-default speech-to-text rows (Together Whisper / Nemotron).
        # AUDIO|CHAT models such as Gemini are not STT entries.
        is_default = (
            (req_cap == "text" and (m.get("default_text") or effective_id == "openrouter/free"))
            or (req_cap == "image" and m.get("default_image"))
            or (req_cap == "audio" and _row_is_stt(m))
            or (req_cap == "tts" and _row_is_tts(m))
        )
        if not is_default:
            continue
        # OpenRouter's speech list returns hexgrad/kokoro-82m; the catalog id
        # is hexgrad/Kokoro-82M. Suffix variants (:nitro) are not the same id.
        if provider == "openrouter" and any(
            mid.casefold() == effective_id.casefold() for mid in to_show
        ):
            continue
        if effective_id not in to_show:
            to_show.append(effective_id)


def get_model_capability(model_id: Any, endpoint: Any) -> int:
    """Check the model catalog for capabilities bitmask."""
    provider = get_provider_from_endpoint(endpoint)
    model_id = str(model_id or "").strip()
    # Check DEFAULT_MODELS for this ID/provider
    for m in DEFAULT_MODELS:
        effective_id = resolve_model_id(m, provider)
        if not effective_id:
            continue
        if provider == "openrouter":
            if openrouter_model_ids_equivalent(effective_id, model_id):
                return m.get("capability", ModelCapability.CHAT)
        elif effective_id == model_id:
            return m.get("capability", ModelCapability.CHAT)
    return ModelCapability.NONE


# Together serverless audio families. GET /v1/models has no audio type, so a row
# is only picked up when its id starts with one of these (docs catalog is the
# source of truth; a new sonic-* or nemotron ASR id can still appear).
_TOGETHER_TTS_PREFIXES: tuple[str, ...] = ("canopylabs/orpheus", "hexgrad/kokoro", "cartesia/sonic")
_TOGETHER_STT_PREFIXES: tuple[str, ...] = ("openai/whisper", "nvidia/parakeet", "nvidia/nemotron")
_COMBO_CAP = {
    "text": ModelCapability.CHAT,
    "image": ModelCapability.IMAGE,
    "audio": ModelCapability.AUDIO,
    "tts": ModelCapability.AUDIO,
}


def _row_is_tts(model: dict[str, Any]) -> bool:
    return bool(model.get("default_tts") or model.get("tts"))


def _row_is_stt(model: dict[str, Any]) -> bool:
    return bool(model.get("default_audio") or model.get("stt"))


def catalog_speech_ids(provider: str, kind: str) -> list[str]:
    """Every TTS or STT catalog id for ``provider``, not only the default."""
    pred = _row_is_tts if kind == "tts" else _row_is_stt
    out: list[str] = []
    for model in DEFAULT_MODELS:
        if not pred(model):
            continue
        effective_id = resolve_model_id(model, provider)
        if effective_id and effective_id not in out:
            out.append(effective_id)
    return out


def together_speech_ids(kind: str, remote_models: list[str] | None = None) -> list[str]:
    """Together Speech-tab ids: full serverless catalog, plus prefix matches.

    ``remote_models`` may be the raw ``GET /v1/models`` array. Chat rows are
    dropped. There is no Together ``output_modalities`` query.
    """
    curated = catalog_speech_ids("together", kind)
    prefixes = _TOGETHER_TTS_PREFIXES if kind == "tts" else _TOGETHER_STT_PREFIXES
    seen = {mid.casefold() for mid in curated}
    extras: list[str] = []
    for raw in remote_models or []:
        mid = str(raw).strip()
        if not mid:
            continue
        folded = mid.casefold()
        if folded in seen:
            continue
        # nvidia/nemotron also matches chat ids (nemotron-nano). Those are
        # not ASR unless the id contains "asr". Whisper, parakeet, and the
        # TTS prefixes do not overlap this string.
        if folded.startswith(prefixes) and not (
            folded.startswith("nvidia/nemotron") and "asr" not in folded
        ):
            extras.append(mid)
            seen.add(folded)
    return curated + extras


DEFAULT_MODELS: list[dict[str, Any]] = [
    {"display_name": "Free Models (Auto)", "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 200000, "ids": {"openrouter": "openrouter/free"}},
    {"display_name": "DeepSeek V3", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 163840, "ids": {"deepseek": "deepseek-chat"}, "default_text": True},
    {"display_name": "DeepSeek V4 Flash", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 163840, "ids": {"together": "deepseek-ai/DeepSeek-V4-Flash-0731"}},
    {"display_name": "MiniMax M3", "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1000000, "ids": {"together": "MiniMaxAI/MiniMax-M3"}, "default_text": True},
    {"display_name": "GPT-OSS 120B", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 131072, "ids": {"together": "openai/gpt-oss-120b", "openrouter": "openai/gpt-oss-120b:nitro", "groq": "openai/gpt-oss-120b"}, "default_text": True},
    {"display_name": "GPT-OSS 20B", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 131072, "ids": {"together": "openai/gpt-oss-20b", "openrouter": "openai/gpt-oss-20b", "groq": "openai/gpt-oss-20b"}},
    {"display_name": "Mistral Large 3", "capability": ModelCapability.CHAT | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 262144, "ids": {"openrouter": "mistralai/mistral-large-2512", "mistral": "mistral-large-latest"}},
    {"display_name": "Voxtral Mini Transcribe", "capability": ModelCapability.AUDIO, "ids": {"openrouter": "mistralai/voxtral-mini-transcribe"}, "default_audio": True},
    {"display_name": "Gemini 3.1 Flash Lite Preview", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1048576, "ids": {"google": "gemini-3.1-flash-lite-preview", "openrouter": "google/gemini-3.1-flash-lite-preview"}},
    {"display_name": "Gemini 3.1 Flash Lite", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1048576, "ids": {"google": "gemini-3.1-flash-lite", "openrouter": "google/gemini-3.1-flash-lite"}},
    {"display_name": "Gemini 3.1 Pro", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.VISION | ModelCapability.TOOLS, "context_length": 1048576, "ids": {"google": "gemini-3.1-pro", "openrouter": "google/gemini-3.1-pro"}},
    {"display_name": "FLUX.2 [dev]", "capability": ModelCapability.IMAGE, "ids": {"together": "black-forest-labs/FLUX.2-dev"}, "default_image": True},
    {"display_name": "Gemini Flash Image 2.5", "capability": ModelCapability.IMAGE, "ids": {"together": "google/flash-image-2.5"}},
    {"display_name": "Gemini 3.1 Flash Lite Image", "capability": ModelCapability.IMAGE, "ids": {"openrouter": "google/gemini-3.1-flash-lite-image"}, "default_image": True},
    {"display_name": "Nvidia Parakeet TDT 0.6B v3", "capability": ModelCapability.AUDIO, "ids": {"together": "nvidia/parakeet-tdt-0.6b-v3"}, "default_audio": True},
    {"display_name": "Whisper Large v3", "capability": ModelCapability.AUDIO, "ids": {"together": "openai/whisper-large-v3"}, "stt": True},
    {"display_name": "Nemotron 3 ASR Streaming 0.6B", "capability": ModelCapability.AUDIO, "ids": {"together": "nvidia/nemotron-3-asr-streaming-0.6b"}, "stt": True},
    {"display_name": "Nemotron 3.5 ASR Streaming 0.6B", "capability": ModelCapability.AUDIO, "ids": {"together": "nvidia/nemotron-3.5-asr-streaming-0.6b"}, "stt": True},
    {"display_name": "Kokoro 82M", "capability": ModelCapability.AUDIO, "ids": {"together": "hexgrad/Kokoro-82M", "openrouter": "hexgrad/Kokoro-82M"}, "default_tts": True},
    {"display_name": "Cartesia Sonic", "capability": ModelCapability.AUDIO, "ids": {"together": "cartesia/sonic"}, "tts": True},
    {"display_name": "Cartesia Sonic 2", "capability": ModelCapability.AUDIO, "ids": {"together": "cartesia/sonic-2"}, "tts": True},
    {"display_name": "Cartesia Sonic 3", "capability": ModelCapability.AUDIO, "ids": {"together": "cartesia/sonic-3"}, "tts": True},
    {"display_name": "Orpheus 3B", "capability": ModelCapability.AUDIO, "ids": {"together": "canopylabs/orpheus-3b-0.1-ft"}, "tts": True},
    {"display_name": "GPT Audio Mini", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.TOOLS, "context_length": 128000, "ids": {"openrouter": "openai/gpt-audio-mini"}},
    {"display_name": "OpenAI TTS-1", "capability": ModelCapability.AUDIO, "ids": {"openai": "tts-1"}, "default_tts": True},
    {"display_name": "GLM 5.2", "capability": ModelCapability.CHAT | ModelCapability.TOOLS, "context_length": 200000, "ids": {"zai": "glm-5.2"}, "default_text": True},
    {"display_name": "GLM ASR 2512", "capability": ModelCapability.AUDIO, "ids": {"zai": "glm-asr-2512"}, "default_audio": True},
    {"display_name": "WriterAgent Mock", "capability": ModelCapability.CHAT | ModelCapability.AUDIO | ModelCapability.TOOLS, "context_length": 32768, "ids": {"mock": "writeragent-mock"}},
]
