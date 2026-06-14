# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Logic for fetching available models from LLM endpoints.
"""
import urllib.parse
import ipaddress
import logging
from typing import Any

from plugin.framework.constants import ModelCapability
from plugin.framework.default_models import DEFAULT_MODELS, get_provider_defaults, resolve_model_id
from plugin.framework.url_utils import normalize_endpoint_url, get_api_version_suffix
from plugin.framework.client.provider_detection import (
    get_provider_from_endpoint as _central_get_provider_from_endpoint,
    is_local_host,  # noqa: F401 - re-export for backward compat
    is_openrouter_endpoint,  # noqa: F401 - re-export for backward compat
)

# Re-export the canonical versions so existing `from .model_fetcher import ...`
# sites keep working during the transition.
get_provider_from_endpoint = _central_get_provider_from_endpoint  # type: ignore[no-redef]
from plugin.framework.errors import NetworkError
from plugin.framework.config import (
    get_api_key_for_endpoint,
    get_config_bool_safe,
    as_bool,
    get_config,
    get_current_endpoint,
    set_config,
)

log = logging.getLogger(__name__)

# Endpoint presets: local first, then FOSS-friendly / open-model providers, proprietary last. Base URLs only; api.py adds /v1 (or /api for OpenWebUI).
ENDPOINT_PRESETS = [
    ("Local (Ollama)", "http://localhost:11434"),
    ("Local (LM Studio)", "http://localhost:1234"),
    ("OpenRouter", "https://openrouter.ai/api"),
    ("Mistral", "https://api.mistral.ai"),
    ("Together AI", "https://api.together.xyz"),
    ("Groq", "https://api.groq.com/openai"),
    ("DeepSeek", "https://api.deepseek.com"),
    ("Cerebras", "https://api.cerebras.ai/v1"),
    ("Perplexity", "https://api.perplexity.ai"),
    ("X.ai (Grok)", "https://api.x.ai/v1"),
    ("Anthropic", "https://api.anthropic.com/v1"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("Z.ai", "https://api.z.ai/v4"),
]


# GET {base}/v1/models — memoized for the lifetime of this Python process (LibreOffice
# session). Key is normalized URL, or ``url + "\\x1f" + api_key`` when ``ctx`` is passed
# (same host, different keys must not share cache). Value is model id list or None after failure.
_model_fetch_cache: dict[str, list[str] | None] = {}
_model_fetch_image_cache: dict[str, list[str] | None] = {}

# /v1/models response shapes (GET {endpoint}/v1/models):
# - Together (api.together.xyz): top-level JSON array [{id, type, ...}, ...]; image rows use type="image".
# - OpenRouter (openrouter.ai): {data: [...]}; image rows use architecture.output_modalities (not slug names).
# - OpenAI-compatible (Ollama, LM Studio, most hosted chat APIs): {data: [{id}, ...]}; image models
#   are not typed — local discovery uses slug keywords in _filter_fetched_models (flux, sdxl, …).
# Image-output IDs are extracted at fetch time into _model_fetch_image_cache; see
# fetch_available_image_models for which providers trust metadata vs slug fallback.


def _v1_models_entries_from_body(data: Any) -> list[Any] | None:
    """Normalize /v1/models JSON to a list of model row dicts."""
    # Together: bare array (see Together OpenAPI ModelInfoList).
    if isinstance(data, list):
        return data
    # OpenRouter, OpenAI, Ollama, etc.: {"data": [...]}.
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return None


def _image_output_model_ids_from_v1_entries(entries: list[Any]) -> list[str]:
    """Collect model IDs that generate images (not vision-input-only chat models)."""
    out: list[str] = []
    for m in entries:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        arch = m.get("architecture") or {}
        modalities = arch.get("output_modalities")
        # OpenRouter: google/gemini-2.5-flash-image, openai/gpt-5-image, etc.
        if isinstance(modalities, list) and "image" in modalities:
            out.append(str(mid))
        # Together: google/flash-image-2.5, black-forest-labs/FLUX.* — type enum, not architecture.
        elif str(m.get("type") or "").lower() == "image":
            out.append(str(mid))
    return out


def _parse_v1_models_response(data: Any) -> tuple[list[str], list[str]] | None:
    """Return (all_ids, image_output_ids) from a /v1/models JSON body."""
    entries = _v1_models_entries_from_body(data)
    if entries is None:
        return None
    models: list[str] = []
    for m in entries:
        if isinstance(m, dict):
            mid = m.get("id")
            if mid:
                models.append(str(mid))
    image_models = _image_output_model_ids_from_v1_entries(entries)
    return models, image_models


def _store_model_fetch_caches(cache_key: str, models: list[str] | None, image_models: list[str] | None) -> None:
    _model_fetch_cache[cache_key] = models
    _model_fetch_image_cache[cache_key] = image_models if models is not None else None


def _model_fetch_cache_key(url: str, ctx: Any, base: str, api_key_override: str | None = None) -> str:
    if ctx is None:
        return url
    if api_key_override is not None:
        key = str(api_key_override).strip()
    else:
        key = str(get_api_key_for_endpoint(ctx, base) or "")
    return f"{url}\x1f{key}"


def endpoint_url_suitable_for_v1_models_fetch(endpoint: str) -> bool:
    """True if endpoint looks like a complete http(s) URL with a real host (skip mid-typing e.g. 'http:/')."""
    if not endpoint or not isinstance(endpoint, str):
        return False
    try:
        p = urllib.parse.urlparse(endpoint.strip())
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname
    if not host:
        return False
    h = host.lower()
    if h == "localhost":
        return True
    if "." in h:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def fetch_available_models(endpoint, ctx=None, api_key_override: str | None = None):
    """Fetch available models from endpoint/v1/models. Returns list of IDs or None on error.

    When ``ctx`` is set, sends the same auth headers as chat (Bearer / x-api-key per provider)
    using ``get_api_key_for_endpoint(ctx, base)``. Pass ``api_key_override`` (including ``""``)
    to use a key not yet saved to config (e.g. Settings dialog typing order: URL then API key).

    When ``ctx`` is omitted, behavior matches legacy unauthenticated GET (tests / callers
    without context). ``api_key_override`` is ignored if ``ctx`` is ``None``.

    Responses (including failed lookups, stored as None) are cached in `_model_fetch_cache`
    for the process lifetime so repeated Settings/sidebar use does not re-hit the network.
    """
    if not endpoint:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base:
        return None
    if not endpoint_url_suitable_for_v1_models_fetch(base):
        return None
    is_owu = get_config_bool_safe(ctx, "is_openwebui") if ctx else False
    suffix = get_api_version_suffix(base, is_openwebui=is_owu)
    url = f"{base}{suffix}/models"
    cache_key = _model_fetch_cache_key(url, ctx, base, api_key_override)
    if cache_key in _model_fetch_cache:
        return _model_fetch_cache[cache_key]

    req_headers: dict[str, str] = {}
    if ctx is not None:
        from plugin.framework.client.auth import AuthError, build_auth_headers, resolve_auth_for_config

        if api_key_override is not None:
            api_key = str(api_key_override).strip()
        else:
            api_key = str(get_api_key_for_endpoint(ctx, base) or "").strip()
        is_openwebui = as_bool(get_config(ctx, "is_openwebui")) or "open-webui" in base.lower() or "openwebui" in base.lower()
        is_openrouter = "openrouter.ai" in base.lower() or as_bool(get_config(ctx, "is_openrouter"))
        mini = {"endpoint": base, "api_key": api_key, "is_openwebui": is_openwebui, "is_openrouter": is_openrouter}
        try:
            req_headers = build_auth_headers(resolve_auth_for_config(mini))
        except AuthError as e:
            log.debug("fetch_available_models skipping %s: %s", url, e)
            _store_model_fetch_caches(cache_key, None, None)
            return None

    try:
        from plugin.framework.client.requests import sync_request
        data = sync_request(url, parse_json=True, headers=req_headers if req_headers else None)
        parsed = _parse_v1_models_response(data)
        if parsed is not None:
            models, image_models = parsed
            _store_model_fetch_caches(cache_key, models, image_models)
            return models
    except (ValueError, TypeError, IOError) as e:
        log.warning("fetch_available_models network/parse error for %s: %s", url, e)
    except Exception as e:
        if isinstance(e, NetworkError):
            log.warning("fetch_available_models NetworkError for %s: %s", url, e)
        else:
            log.warning("fetch_available_models unexpected error for %s: %s", url, type(e).__name__)
    _store_model_fetch_caches(cache_key, None, None)
    return None


def fetch_available_image_models(endpoint, ctx=None, api_key_override: str | None = None):
    """Image-output model IDs from /v1/models (architecture.output_modalities or type=image).

    Provider policy after shared fetch (see module comment above):
    - openrouter / together: metadata only (_model_fetch_image_cache); no slug guessing.
    - ollama / lm studio / custom: keyword filter on id strings when metadata is empty.
    """
    if not endpoint:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base:
        return None
    if not endpoint_url_suitable_for_v1_models_fetch(base):
        return None
    all_models = fetch_available_models(endpoint, ctx, api_key_override=api_key_override)
    if all_models is None:
        return None
    is_owu = get_config_bool_safe(ctx, "is_openwebui") if ctx else False
    suffix = get_api_version_suffix(base, is_openwebui=is_owu)
    url = f"{base}{suffix}/models"
    cache_key = _model_fetch_cache_key(url, ctx, base, api_key_override)
    arch_ids = _model_fetch_image_cache.get(cache_key) or []
    provider = get_provider_from_endpoint(base)
    # Hosted catalogs declare image models in API metadata; slug heuristics mis-classify
    # (e.g. OpenRouter gemini-*-image names, Together google/flash-image-2.5 without "flux" in id).
    if provider in ("openrouter", "together"):
        return list(arch_ids)
    # Ollama / LM Studio: /v1/models rows lack type/architecture; match flux, sdxl, etc. on id.
    return _filter_fetched_models(all_models, "image")

def _filter_fetched_models(models: list[str], req_cap: str) -> list[str]:
    """Filter raw model IDs from /v1/models based on the requested capability (text/image/audio)."""
    if not models:
        return []

    out = []
    if req_cap == "text":
        # Exclude known non-chat models (mirrors LibreAI C++ logic)
        exclude = {
            "embedding", "embed", "aqa", "attribution", "retrieval", "vision",
            "rerank", "classifier", "moderation", "whisper", "speech", "audio",
            "llava", "stable-diffusion", "sdxl", "dall", "aurora", "imagen",
            "codellama", "codegemma", "starcoder", "deepseek-coder", "coder"
        }
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in exclude):
                continue
            out.append(m)
    elif req_cap == "image":
        # Ollama / local only — hosted OpenRouter/Together use fetch_available_image_models metadata path.
        include = {"flux", "stable-diffusion", "sdxl", "dall-e", "aurora", "imagen", "dreamshaper", "playground", "juggernaut"}
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in include):
                out.append(m)
    else:
        # Audio/STT: name heuristics for local /v1/models (hosted catalogs lack modality).
        include = {"whisper", "voxtral", "parakeet", "transcribe", "speech"}
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in include):
                out.append(m)
    return out


# --- Provider and Endpoint resolution ---


# get_provider_from_endpoint is re-exported from the central implementation in
# provider_detection.py (see the 2026 provider heuristic consolidation).
# The name is bound above via the import alias for backward-compat with any
# internal `from .model_fetcher import get_provider_from_endpoint` sites.


def get_endpoint_presets():
    """Return list of (label, url) for endpoint selector, in display order."""
    return list(ENDPOINT_PRESETS)


# --- Model capability and audio support ---


def get_model_capability(ctx, model_id, endpoint):
    """Check the model catalog for capabilities bitmask."""
    provider = get_provider_from_endpoint(endpoint)
    model_id = str(model_id or "").strip()
    # Check DEFAULT_MODELS for this ID/provider
    for m in DEFAULT_MODELS:
        effective_id = resolve_model_id(m, provider)
        if not effective_id:
            continue
        if provider == "openrouter":
            from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent

            if openrouter_model_ids_equivalent(effective_id, model_id):
                return m.get("capability", ModelCapability.CHAT)
        elif effective_id == model_id:
            return m.get("capability", ModelCapability.CHAT)
    return ModelCapability.NONE


def has_native_audio(ctx, model_id, endpoint):
    """Determine if a model supports native audio input.
    Uses persistent cache first, then catalog/heuristics.
    Returns: True if supported, False if unsupported, None if unknown.
    """
    model_id = str(model_id).lower()
    endpoint = normalize_endpoint_url(endpoint)

    # 1. Persistent Cache Check
    cache = get_config(ctx, "audio_support_map")
    if isinstance(cache, dict):
        key = f"{endpoint}@{model_id}"
        if key in cache:
            return as_bool(cache[key])

    # 2. Catalog check
    caps = get_model_capability(ctx, model_id, endpoint)
    if isinstance(caps, int) and (caps & ModelCapability.AUDIO):
        return True

    # 3. Heuristics (Regex/Keywords) for known audio-native families
    # Gemini (Flash/Pro 1.5+)
    if "gemini" in model_id and "1.5" in model_id:
        return True
    # Explicit audio models
    if "audio-preview" in model_id or "multimodal" in model_id:
        return True

    return None  # Unknown, allow trying native audio


def set_native_audio_support(ctx, model_id, endpoint, supported):
    """Save the audio support status for a model+endpoint pair."""
    model_id = str(model_id).lower()
    endpoint = normalize_endpoint_url(endpoint)
    key = f"{endpoint}@{model_id}"

    cache = get_config(ctx, "audio_support_map")
    if not isinstance(cache, dict):
        cache = {}

    cache[key] = bool(supported)
    set_config(ctx, "audio_support_map", cache)


# --- Resolved model getters (text / STT / grammar / image) ---


def get_text_model(ctx):
    """Return the text/chat model (stored as text_model, fallback to model)."""
    val = str(get_config(ctx, "text_model") or get_config(ctx, "model") or "").strip()
    if val:
        return val
    current_endpoint = get_current_endpoint(ctx)
    provider = get_provider_from_endpoint(current_endpoint)
    defaults = get_provider_defaults(provider)
    return str(defaults.get("text_model", "")).strip()


def get_stt_model(ctx):
    """Return the configured STT model."""
    val = get_config(ctx, "stt_model")
    if val is not None and str(val).strip():
        return str(val).strip()
    current_endpoint = get_current_endpoint(ctx)
    provider = get_provider_from_endpoint(current_endpoint)
    defaults = get_provider_defaults(provider)
    return str(defaults.get("stt_model", "") or "").strip()


def get_grammar_model(ctx):
    """Return the configured grammar model, fallback to chat text model."""
    val = str(get_config(ctx, "doc.grammar_proofreader_model") or "").strip()
    if val:
        return val
    return get_text_model(ctx)


def get_image_model(ctx):
    """Return current image model based on provider."""
    image_provider = get_config(ctx, "image_provider")
    if image_provider == "aihorde":
        return str(get_config(ctx, "aihorde_model") or "").strip()
    val = str(get_config(ctx, "image_model") or "").strip()
    if val:
        return val
    if image_provider == "endpoint":
        current_endpoint = get_current_endpoint(ctx)
        provider = get_provider_from_endpoint(current_endpoint)
    else:
        provider = image_provider
    defaults = get_provider_defaults(provider)
    return str(defaults.get("image_model", "")).strip()


def set_image_model(ctx, val, update_lru=True):
    """Set image model based on provider and notify listeners."""
    if val is None:
        return
    val_str = str(val).strip()
    if not val_str:
        return

    image_provider = get_config(ctx, "image_provider")
    storage_key = "aihorde_model" if image_provider == "aihorde" else "image_model"
    current = str(get_config(ctx, storage_key) or "").strip()
    if val_str == current:
        return

    if image_provider == "aihorde":
        set_config(ctx, "aihorde_model", val_str)
    else:
        set_config(ctx, "image_model", val_str)
        if update_lru:
            from plugin.chatbot.config_ui_helpers import update_lru_history

            update_lru_history(ctx, val_str, "image_model_lru", get_current_endpoint(ctx))

