# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Logic for fetching available models from LLM endpoints.

Concurrency: GET ``/v1/models`` results are memoized in module-level dicts
for the LibreOffice process (sidebar combobox, settings probes, etc.).
Those dicts have **no lock**. If two background jobs miss the cache at
once, both may HTTP; whichever finishes last overwrites the list. That is
harmless (same endpoint, same models). Do not add a mutex just to
serialize identical fetches. When a UNO ``ctx`` is passed, the cache key
includes a hash of the API key so two keys on the same host do not share a
list and a dump of the dict does not contain the key.

Settings treats a successful OpenRouter or Together catalog as once per
process. ``settings_catalog_is_warm`` is that check. **Test Connection**
clears the memo with ``clear_settings_catalog_cache``.
"""

from __future__ import annotations

import hashlib
import urllib.parse
import json
import ipaddress
import logging
import re
from typing import Any

from plugin.framework.constants import ModelCapability
from plugin.framework.default_models import DEFAULT_MODELS, get_provider_defaults, resolve_model_id
from plugin.framework.url_utils import normalize_endpoint_url, get_api_version_suffix
from plugin.framework.client.provider_detection import get_provider_from_endpoint, is_openrouter_endpoint, is_openwebui_endpoint
from plugin.framework.errors import NetworkError
from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent
from plugin.framework.config import get_api_key_for_endpoint, get_config_bool_safe, get_config, get_current_endpoint, set_config
from plugin.framework.config_schema import as_bool

log = logging.getLogger(__name__)

# Catalog / show probes are short and unrelated to LLM generate time.
# Written at each sync_request call site (timeout is required, no default).
_MODEL_FETCH_TIMEOUT = 10

# Endpoint presets: local first, then FOSS-friendly / open-model providers, proprietary last. Base URLs only; get_api_version_suffix adds /v1, /api (OpenWebUI), or /api/paas/v4 (Z.ai).
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
    ("NVIDIA NIM", "https://integrate.api.nvidia.com/v1"),
    ("Z.ai", "https://api.z.ai/api/paas/v4"),
]


# GET {base}/v1/models — memoized for the lifetime of this Python process (LibreOffice
# session). Key is normalized URL plus a hash of the API key (same host, different
# keys must not share cache; the raw key is not stored). Value is model id list or None after failure.
_model_fetch_cache: dict[str, list[str] | None] = {}
_model_fetch_image_cache: dict[str, list[str] | None] = {}
# Successful /v1/models vision memo. A dict is id -> accepts image, and only
# ids whose row listed input_modalities are present. A list is positive ids
# only (tests). None is not stored: a failed GET must be retried.
_model_fetch_vision_cache: dict[str, list[str] | dict[str, bool] | None] = {}
# OpenRouter GET /v1/models?output_modalities=speech|transcription. Separate from
# the unfiltered catalog: that list does not mark TTS, and output_modalities=audio
# is music / gpt-audio, not the Speech-tab TTS combo.
_model_fetch_tts_cache: dict[str, list[str] | None] = {}
_model_fetch_stt_cache: dict[str, list[str] | None] = {}
# Speech voice tokens, keyed by the API model id (process lifetime).
# OpenRouter fills this from supported_voices on the speech model list.
# Together fills it from GET /v1/voices. Same map — not a second voice cache.
_tts_supported_voices: dict[str, list[str]] = {}
# response_format that worked for that model (mp3 / pcm / wav). Same lifetime
# as the voice list: learned while speaking, not written to writeragent.json.
# Gemini-class speech models reject mp3; remembering pcm skips that 400 next time.
# Together uses this too: _download_endpoint_speech learns the format per model id.
_tts_response_format: dict[str, str] = {}
_TTS_RESPONSE_FORMATS = ("mp3", "pcm", "wav")
# GET /v1/voices response memo (URL+key -> model->tokens, or None after failure).
# The Voice combo reads _tts_supported_voices, not this dict. It only stops a
# dialog refresh from repeating the same GET.
_together_voices_fetch_cache: dict[str, dict[str, list[str]] | None] = {}
# Same key as _model_fetch_cache. Per-id context tokens harvested from /v1/models
# (context_length or context_window only). None after a failed fetch. Lookup
# never HTTP — compact reads this; Settings/sidebar populate it.
_model_context_cache: dict[str, dict[str, int] | None] = {}
# POST /api/show, once per process. Value is {capabilities: list[str], num_ctx: int|None}.
# Vision probes and the #570 crash sentence share this so the first probe pays for both.
_ollama_show_cache: dict[str, dict[str, Any]] = {}
# Older tests clear this name; keep it as an alias of the same dict.
_ollama_capabilities_cache = _ollama_show_cache

# Runtime PARAMETER / Modelfile line. Do not use this on model_info context_length.
_OLLAMA_NUM_CTX_LINE = re.compile(r"(?im)^\s*(?:PARAMETER\s+)?num_ctx\s+(\d+)\s*$")

# /v1/models response shapes (GET {endpoint}/v1/models):
# - Together (api.together.xyz): top-level JSON array [{id, type, ...}, ...]; image rows use type="image".
# - OpenRouter (openrouter.ai): {data: [...]}; image rows use architecture.output_modalities (not slug names).
#   TTS is GET /v1/models?output_modalities=speech (not audio). STT is output_modalities=transcription.
# - OpenAI-compatible (Ollama, LM Studio, most hosted chat APIs): {data: [{id}, ...]}; image models
#   are not typed — the image memo for those hosts is the slug keyword filter
#   (flux, sdxl, …), the same list fetch_available_image_models returns.
# Image-output IDs are stored at fetch time in _model_fetch_image_cache. Settings
# reads that memo (once per startup). Test Connection clears it.


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


def _vision_input_model_ids_from_v1_entries(entries: list[Any]) -> list[str]:
    """Collect model IDs that accept image input (vision-capable models)."""
    out: list[str] = []
    for m in entries:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        arch = m.get("architecture") or {}
        input_mods = m.get("input_modalities") or arch.get("input_modalities") or []
        if isinstance(input_mods, list) and "image" in input_mods:
            out.append(str(mid))
    return out


def _stated_input_modalities(row: dict[str, Any]) -> list[Any] | None:
    """input_modalities when this row states a list. None when the field is absent.

    An empty list is a statement (no image). A missing field is not.
    ``top or nested`` matches ``_vision_input_model_ids_from_v1_entries``.
    A non-dict architecture still raises on ``.get``.
    """
    arch = row.get("architecture") or {}
    top = row.get("input_modalities") if "input_modalities" in row else None
    # Non-dict architecture: ``in`` is not enough (a string can contain the
    # name). Call ``.get`` so the parse still aborts, same as the id collector.
    if not isinstance(arch, dict) or "input_modalities" in arch:
        nested = arch.get("input_modalities")
    else:
        nested = None
    chosen = top if top else nested
    if isinstance(chosen, list):
        return chosen
    if isinstance(top, list):
        return top
    return None


def _vision_declarations_from_v1_entries(entries: list[Any]) -> dict[str, bool]:
    """id -> image input, only for rows that listed input_modalities.

    Omitted input_modalities is not an entry. Callers must not treat a missing
    id as an explicit no.
    """
    out: dict[str, bool] = {}
    for row in entries:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not mid:
            continue
        stated = _stated_input_modalities(row)
        if stated is None:
            continue
        out[str(mid)] = "image" in stated
    return out


def _parse_positive_ctx(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _context_tokens_from_v1_entries(entries: list[Any]) -> dict[str, int]:
    """Harvest advertised windows. Prefer context_length (OR/Together) over context_window (Groq).

    Do not read max_context_length — that is a trained max (LM Studio / Ollama-class
    #570), not the live load window.
    """
    out: dict[str, int] = {}
    for m in entries:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        tokens = _parse_positive_ctx(m.get("context_length"))
        if tokens is None:
            tokens = _parse_positive_ctx(m.get("context_window"))
        if tokens is not None:
            out[str(mid)] = tokens
    return out


def _parse_v1_models_response(data: Any) -> tuple[list[str], list[str], dict[str, bool]] | None:
    """Return (all_ids, image_output_ids, vision_declarations) from a /v1/models body.

    Vision declarations are id -> accepts image, only when the row listed
    input_modalities. The positive-id walk still runs so a non-dict
    architecture aborts this parse the same way it did before.
    """
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
    # The positive-id walk is what aborts on a non-dict architecture. Keep it
    # even though the memo now stores declarations, not that id list.
    _vision_input_model_ids_from_v1_entries(entries)
    return models, image_models, _vision_declarations_from_v1_entries(entries)


def _store_model_fetch_caches(cache_key: str, models: list[str] | None, image_models: list[str] | None, vision_models: list[str] | dict[str, bool] | None = None, context_tokens: dict[str, int] | None = None) -> None:
    # A failed fetch must not stick for the process lifetime. The next caller retries.
    if models is None:
        return
    _model_fetch_cache[cache_key] = models
    _model_fetch_image_cache[cache_key] = image_models
    if isinstance(vision_models, dict):
        _model_fetch_vision_cache[cache_key] = dict(vision_models)
    else:
        _model_fetch_vision_cache[cache_key] = vision_models
    _model_context_cache[cache_key] = dict(context_tokens) if context_tokens else {}


def _model_fetch_cache_key(url: str, base: str, api_key_override: str | None = None) -> str:
    if api_key_override is not None:
        key = str(api_key_override).strip()
    else:
        key = str(get_api_key_for_endpoint(base) or "")
    # Hash, not the raw key: these dicts live for the process and get logged
    # in diagnostics. Two keys on one URL must still miss each other.
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{url}\x1f{digest}"


def _api_suffix(base: str) -> str:
    return get_api_version_suffix(base, is_openwebui=get_config_bool_safe("is_openwebui"))


def _models_list_url(base: str) -> str:
    """Same URL ``fetch_available_models`` caches under."""
    return f"{base}{_api_suffix(base)}/models"


def _openrouter_image_models_url(base: str) -> str:
    """OpenRouter image catalog. Not the architecture slice of ``/v1/models``."""
    return f"{base}{_api_suffix(base)}/images/models"


def _openrouter_modality_models_url(base: str, modality: str) -> str:
    query = urllib.parse.urlencode({"output_modalities": modality})
    return f"{_models_list_url(base)}?{query}"


def _together_voices_url(base: str, model_id: str = "") -> str:
    root = f"{base}{_api_suffix(base)}/voices"
    requested = str(model_id or "").strip()
    if not requested:
        return root
    return f"{root}?{urllib.parse.urlencode({'model': requested})}"


def _catalog_base(endpoint: str) -> str:
    if not endpoint:
        return ""
    base = normalize_endpoint_url(endpoint)
    if not base or not endpoint_url_suitable_for_v1_models_fetch(base):
        return ""
    return base


def _cached_id_list(cache: dict[str, list[str] | None], key: str) -> list[str] | None:
    """A stored list is success, including empty. Missing or a failed ``None`` is a miss."""
    found = cache.get(key)
    if isinstance(found, list):
        return list(found)
    return None


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


def _model_fetch_auth_headers(base: str, api_key_override: str | None, url: str, *, is_openwebui: bool, is_openrouter: bool, log_label: str) -> dict[str, str] | None:
    """Headers for a model-list GET.

    None: a key was present and auth setup failed; the caller skips the request.
    {}: fetch without credentials.
    """
    from plugin.framework.client.auth import AuthError, build_auth_headers, resolve_auth_for_config

    if api_key_override is not None:
        api_key = str(api_key_override).strip()
    else:
        api_key = str(get_api_key_for_endpoint(base) or "").strip()
    mini = {"endpoint": base, "api_key": api_key, "is_openwebui": is_openwebui, "is_openrouter": is_openrouter}
    try:
        return build_auth_headers(resolve_auth_for_config(mini))
    except AuthError as e:
        if api_key:
            log.debug("%s skipping %s: %s", log_label, url, e)
            return None
        log.debug("%s unauthenticated for %s: %s", log_label, url, e)
        return {}


def fetch_available_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Fetch available models from endpoint/v1/models. Returns list of IDs or None on error.

    Sends the same auth headers as chat (Bearer / x-api-key per provider) using
    ``get_api_key_for_endpoint(base)``. Pass ``api_key_override`` (including ``""``)
    to use a key not yet saved to config (e.g. Settings dialog typing order: URL then API key).

    Successful responses are cached in ``_model_fetch_cache`` for the process
    lifetime. Failed lookups are not cached, so the next Settings open retries.
    Settings skips the request when ``settings_catalog_is_warm`` is already true.
    """
    if not endpoint:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base:
        return None
    if not endpoint_url_suitable_for_v1_models_fetch(base):
        return None
    url = _models_list_url(base)
    cache_key = _model_fetch_cache_key(url, base, api_key_override)
    if cache_key in _model_fetch_cache:
        return _model_fetch_cache[cache_key]

    is_openwebui = is_openwebui_endpoint(base, explicit_is_openwebui=as_bool(get_config("is_openwebui")))
    # Hostname equality, same rule as get_provider_from_endpoint. A path that
    # merely contains "openrouter.ai" is not this provider.
    is_openrouter = is_openrouter_endpoint(base, explicit_is_openrouter=as_bool(get_config("is_openrouter")))
    req_headers = _model_fetch_auth_headers(base, api_key_override, url, is_openwebui=is_openwebui, is_openrouter=is_openrouter, log_label="fetch_available_models")
    if req_headers is None:
        return None

    try:
        from plugin.framework.client.requests import sync_request

        data = sync_request(url, parse_json=True, headers=req_headers, timeout=_MODEL_FETCH_TIMEOUT)
        parsed = _parse_v1_models_response(data)
        if parsed is not None:
            models, image_models, vision_declared = parsed
            entries = _v1_models_entries_from_body(data) or []
            provider = get_provider_from_endpoint(base)
            # Ollama, LM Studio, and other non-Together hosts usually omit
            # architecture/type image ids, so a memo of only those ids is []
            # while keyword matching finds flux/sdxl/…. Settings reads the
            # memo, and the startup worker does not do a second image GET for
            # those hosts. Store the keyword list the image fetch returns.
            # Together keeps type=image. OpenRouter Settings uses
            # /v1/images/models, not this slice.
            if provider not in ("together", "openrouter"):
                image_models = _filter_fetched_models(models, "image")
            _store_model_fetch_caches(cache_key, models, image_models, vision_declared, _context_tokens_from_v1_entries(entries))
            if provider == "zai":
                preview = models[:5] if models else []
                log.debug("fetch_available_models z.ai ok url=%s count=%s preview=%r", url, len(models), preview)
            return models
    except (ValueError, TypeError, IOError) as e:
        log.warning("fetch_available_models network/parse error for %s: %s", url, e)
    except Exception as e:
        if isinstance(e, NetworkError):
            log.warning("fetch_available_models NetworkError for %s: %s", url, e)
        else:
            log.exception("fetch_available_models unexpected error for %s", url)
    if get_provider_from_endpoint(base) == "zai":
        log.debug("fetch_available_models z.ai failed url=%s", url)
    return None


def fetch_available_image_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Image-output model IDs. Same list ``cached_image_models`` returns. No extra GET.

    Provider policy (see module comment above):
    - openrouter: queries /v1/images/models.
    - together: metadata only (type=image) from the standard /v1/models memo.
    - ollama / lm studio / custom: keyword filter (flux, sdxl, …) stored with
      that same /v1/models memo. The catalog is not fetched again here.
    """
    if not endpoint:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base:
        return None
    if not endpoint_url_suitable_for_v1_models_fetch(base):
        return None

    provider = get_provider_from_endpoint(base)
    is_owu = get_config_bool_safe("is_openwebui")

    if provider == "openrouter":
        url = _openrouter_image_models_url(base)
        cache_key = _model_fetch_cache_key(url, base, api_key_override)
        if cache_key in _model_fetch_image_cache:
            return _model_fetch_image_cache[cache_key]

        req_headers = _model_fetch_auth_headers(base, api_key_override, url, is_openwebui=is_owu, is_openrouter=True, log_label="fetch_available_image_models openrouter")
        if req_headers is None:
            return None

        try:
            from plugin.framework.client.requests import sync_request

            data = sync_request(url, parse_json=True, headers=req_headers, timeout=_MODEL_FETCH_TIMEOUT)
            entries = _v1_models_entries_from_body(data)
            if entries is not None:
                image_models = []
                for m in entries:
                    if isinstance(m, dict) and m.get("id"):
                        image_models.append(str(m["id"]))
                _model_fetch_image_cache[cache_key] = image_models
                return image_models
        except Exception as e:
            log.warning("fetch_available_image_models openrouter failed for %s: %s", url, e)
        return None

    all_models = fetch_available_models(endpoint, api_key_override=api_key_override)
    if all_models is None:
        return None
    url = _models_list_url(base)
    cache_key = _model_fetch_cache_key(url, base, api_key_override)
    stored = _model_fetch_image_cache.get(cache_key)
    # Together declares image models with type=image. A slug heuristic would
    # mis-classify (google/flash-image-2.5 has no "flux"; a chat FLUX row is not
    # an image model). Other hosts' memo is the keyword list written above.
    if isinstance(stored, list):
        return list(stored)
    if provider == "together":
        return []
    return _filter_fetched_models(all_models, "image")


def _supported_voices_from_row(row: dict[str, Any]) -> list[str]:
    """Voice ids from an OpenRouter speech-model row, when the API sends them."""
    raw = row.get("supported_voices")
    if not isinstance(raw, list):
        return []
    voices: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            voices.append(item.strip())
        elif isinstance(item, dict):
            vid = item.get("id") or item.get("name") or item.get("voice")
            if isinstance(vid, str) and vid.strip():
                voices.append(vid.strip())
    return voices


def _modality_ids_from_entries(entries: list[Any], modality: str) -> tuple[list[str], dict[str, list[str]]]:
    """Ids from a modality-filtered /v1/models body.

    Rows that omit output_modalities are kept: the query string is the filter.
    Rows that advertise modalities and lack ``modality`` are dropped so a proxy
    that ignored ``output_modalities=speech`` cannot fill the TTS combo with
    chat models. ``audio`` is not speech.
    """
    ids: list[str] = []
    voices: dict[str, list[str]] = {}
    for row in entries:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not mid:
            continue
        arch = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
        mods = row.get("output_modalities")
        if mods is None:
            mods = arch.get("output_modalities") if isinstance(arch, dict) else None
        if isinstance(mods, list) and modality not in mods:
            continue
        mid_s = str(mid)
        ids.append(mid_s)
        if modality == "speech":
            parsed = _supported_voices_from_row(row)
            if parsed:
                voices[mid_s] = parsed
    return ids, voices


def _fetch_openrouter_modality_models(endpoint: str, modality: str, cache: dict[str, list[str] | None], api_key_override: str | None) -> list[str] | None:
    """OpenRouter ``GET /v1/models?output_modalities=`` list, memoized like other fetches.

    Together's ``/v1/models`` type enum has no speech or transcription value, so
    this returns None there and the Speech tab keeps curated catalog rows.
    A failed lookup is not cached, same as ``fetch_available_models``: storing
    None used to stick for the process and leave the Speech combo empty.
    """
    if modality not in ("speech", "transcription"):
        return None
    if not endpoint:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base or not endpoint_url_suitable_for_v1_models_fetch(base):
        return None
    if get_provider_from_endpoint(base) != "openrouter":
        return None

    is_owu = get_config_bool_safe("is_openwebui")
    url = _openrouter_modality_models_url(base, modality)
    cache_key = _model_fetch_cache_key(url, base, api_key_override)
    if cache_key in cache:
        return cache[cache_key]

    req_headers = _model_fetch_auth_headers(base, api_key_override, url, is_openwebui=is_owu, is_openrouter=True, log_label=f"fetch openrouter {modality} models")
    if req_headers is None:
        return None

    try:
        from plugin.framework.client.requests import sync_request

        data = sync_request(url, parse_json=True, headers=req_headers, timeout=_MODEL_FETCH_TIMEOUT)
        entries = _v1_models_entries_from_body(data)
        if entries is not None:
            model_ids, voices = _modality_ids_from_entries(entries, modality)
            if modality == "speech" and voices:
                for speech_id, speech_voices in voices.items():
                    remember_tts_supported_voices(speech_id, speech_voices)
            cache[cache_key] = model_ids
            return model_ids
    except Exception as e:
        log.warning("fetch openrouter %s models failed for %s: %s", modality, url, e)
    return None


def fetch_available_tts_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """OpenRouter TTS model ids: ``GET /v1/models?output_modalities=speech``.

    Do not use ``output_modalities=audio`` — that is Lyria / gpt-audio, not this combo.
    """
    return _fetch_openrouter_modality_models(endpoint, "speech", _model_fetch_tts_cache, api_key_override)


def fetch_available_stt_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """OpenRouter STT model ids: ``GET /v1/models?output_modalities=transcription``."""
    return _fetch_openrouter_modality_models(endpoint, "transcription", _model_fetch_stt_cache, api_key_override)


def cached_text_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized ``/v1/models`` ids, or None. Does not HTTP."""
    base = _catalog_base(endpoint)
    if not base:
        return None
    key = _model_fetch_cache_key(_models_list_url(base), base, api_key_override)
    return _cached_id_list(_model_fetch_cache, key)


def cached_image_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized image-output ids, or None. Does not HTTP.

    OpenRouter is ``GET /v1/images/models``. Together is the ``type=image`` slice
    stored with ``/v1/models``. Other hosts are the keyword filter (flux, sdxl, …)
    stored with that same response — the list ``fetch_available_image_models``
    returns. Settings reads this. It does not fetch the catalog again.
    """
    base = _catalog_base(endpoint)
    if not base:
        return None
    if get_provider_from_endpoint(base) == "openrouter":
        url = _openrouter_image_models_url(base)
    else:
        url = _models_list_url(base)
    return _cached_id_list(_model_fetch_image_cache, _model_fetch_cache_key(url, base, api_key_override))


def cached_tts_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized OpenRouter speech ids, or None. Does not HTTP."""
    base = _catalog_base(endpoint)
    if not base or get_provider_from_endpoint(base) != "openrouter":
        return None
    key = _model_fetch_cache_key(_openrouter_modality_models_url(base, "speech"), base, api_key_override)
    return _cached_id_list(_model_fetch_tts_cache, key)


def cached_stt_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized OpenRouter transcription ids, or None. Does not HTTP."""
    base = _catalog_base(endpoint)
    if not base or get_provider_from_endpoint(base) != "openrouter":
        return None
    key = _model_fetch_cache_key(_openrouter_modality_models_url(base, "transcription"), base, api_key_override)
    return _cached_id_list(_model_fetch_stt_cache, key)


def _together_voices_cache_keys(base: str, api_key_override: str | None) -> list[str]:
    """List-all and ``?model=`` memo keys for this endpoint+key."""
    root = _together_voices_url(base)
    sample = _model_fetch_cache_key(root, base, api_key_override)
    digest = sample.split("\x1f", 1)[1]
    found: list[str] = []
    for key in _together_voices_fetch_cache:
        url, sep, key_digest = key.partition("\x1f")
        if sep and key_digest == digest and (url == root or url.startswith(root + "?")):
            found.append(key)
    return found


def _together_voices_list_cached(endpoint: str, api_key_override: str | None) -> bool:
    """True after a successful ``GET /v1/voices`` for this endpoint+key.

    List-all and ``?model=`` are different URLs. Either memo means the Speech
    tab has something to read. A stored dict is success, including empty.
    A failure is not stored.
    """
    base = _catalog_base(endpoint)
    if not base:
        return False
    for key in _together_voices_cache_keys(base, api_key_override):
        if isinstance(_together_voices_fetch_cache.get(key), dict):
            return True
    return False


def settings_catalog_is_warm(endpoint: str, api_key_override: str | None = None) -> bool:
    """True when this process already has the catalogs Settings would show.

    OpenRouter needs text, ``/v1/images/models``, speech, and transcription.
    Together has no modality model list; it needs the text list, the image ids
    harvested from that body, and a successful ``/v1/voices`` memo (list-all
    or ``?model=``). The Speech tab only reads ``cached_tts_supported_voices``.
    Other hosts need the text list and the image ids stored with that body.
    A missing or failed entry is cold. Empty lists count as success.
    """
    base = _catalog_base(endpoint)
    if not base:
        return False
    provider = get_provider_from_endpoint(base)
    if cached_text_models(endpoint, api_key_override) is None:
        return False
    if cached_image_models(endpoint, api_key_override) is None:
        return False
    if provider == "openrouter":
        if cached_tts_models(endpoint, api_key_override) is None:
            return False
        return cached_stt_models(endpoint, api_key_override) is not None
    if provider == "together":
        return _together_voices_list_cached(endpoint, api_key_override)
    # Ollama, Groq, and custom hosts keep text ids and the keyword image ids
    # (flux, sdxl, …) in the one /v1/models memo checked above. That memo is
    # the catalog Settings shows. A warm memo must not look cold, or opening
    # Settings refetches.
    return True


def clear_settings_catalog_cache(endpoint: str, api_key_override: str | None = None) -> None:
    """Drop this endpoint+key's memo so the next fetch hits the network.

    Test Connection uses this. Other endpoints and keys stay cached.
    """
    base = _catalog_base(endpoint)
    if not base:
        return
    text_key = _model_fetch_cache_key(_models_list_url(base), base, api_key_override)
    for cache in (
        _model_fetch_cache,
        _model_fetch_image_cache,
        _model_fetch_vision_cache,
        _model_context_cache,
    ):
        cache.pop(text_key, None)
    image_key = _model_fetch_cache_key(_openrouter_image_models_url(base), base, api_key_override)
    _model_fetch_image_cache.pop(image_key, None)
    tts_key = _model_fetch_cache_key(_openrouter_modality_models_url(base, "speech"), base, api_key_override)
    stt_key = _model_fetch_cache_key(_openrouter_modality_models_url(base, "transcription"), base, api_key_override)
    _model_fetch_tts_cache.pop(tts_key, None)
    _model_fetch_stt_cache.pop(stt_key, None)
    # ?model= is a different memo from list-all. Test Connection must drop both
    # or the next Speech fetch returns the stale scoped body without HTTP.
    for voices_key in _together_voices_cache_keys(base, api_key_override):
        _together_voices_fetch_cache.pop(voices_key, None)


def cached_tts_supported_voices(model_id: str) -> list[str]:
    """Voice tokens harvested for this speech model, if any.

    OpenRouter speech rows and Together ``/v1/voices`` share this map.
    ``hexgrad/Kokoro-82M`` and ``hexgrad/kokoro-82m`` are one entry.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return []
    direct = _tts_supported_voices.get(mid)
    if direct:
        return list(direct)
    folded = mid.casefold()
    for key, voices in _tts_supported_voices.items():
        if key.casefold() == folded and voices:
            return list(voices)
    return []


def _tts_meta_key(model_id: str, table: dict[str, str]) -> str | None:
    """Exact or case-insensitive key already stored for ``model_id``."""
    mid = str(model_id or "").strip()
    if not mid:
        return None
    if mid in table:
        return mid
    folded = mid.casefold()
    for key in table:
        if key.casefold() == folded:
            return key
    return None


def cached_tts_response_format(model_id: str) -> str | None:
    """``response_format`` learned for this speech model, if any.

    Absent means the caller should try ``mp3``. Lookup matches the voice cache:
    ``hexgrad/Kokoro-82M`` and ``hexgrad/kokoro-82m`` share one entry.
    """
    key = _tts_meta_key(model_id, _tts_response_format)
    if key is None:
        return None
    fmt = _tts_response_format.get(key) or ""
    if fmt in _TTS_RESPONSE_FORMATS:
        return fmt
    return None


def remember_tts_response_format(model_id: str, fmt: str) -> None:
    """Remember a speech ``response_format`` until this process exits."""
    mid = str(model_id or "").strip()
    clean = str(fmt or "").strip().lower()
    if not mid or clean not in _TTS_RESPONSE_FORMATS:
        return
    key = _tts_meta_key(mid, _tts_response_format) or mid
    _tts_response_format[key] = clean


def openrouter_speech_list_loaded() -> bool:
    """True after a successful OpenRouter speech-list fetch in this process."""
    return any(isinstance(ids, list) for ids in _model_fetch_tts_cache.values())


def openrouter_speech_list_has_model(model_id: str) -> bool:
    """True when ``model_id`` appeared on a fetched OpenRouter speech list."""
    mid = str(model_id or "").strip().casefold()
    if not mid:
        return False
    for ids in _model_fetch_tts_cache.values():
        if not ids:
            continue
        if any(str(api_id).casefold() == mid for api_id in ids):
            return True
    return False


def remember_tts_supported_voices(model_id: str, voices: list[str]) -> None:
    """Store speech voice tokens for ``model_id`` until this process exits.

    Empty input does not erase a previous list. A later successful harvest
    replaces the row. Case-insensitive ids share one key so a Together
    ``Kokoro-82M`` row does not hide an earlier ``kokoro-82m`` entry.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return
    clean: list[str] = []
    for voice in voices:
        token = voice.strip() if isinstance(voice, str) else ""
        if token and token not in clean:
            clean.append(token)
    if not clean:
        return
    key = mid
    folded = mid.casefold()
    for existing in _tts_supported_voices:
        if existing.casefold() == folded:
            key = existing
            break
    _tts_supported_voices[key] = clean


def _together_voice_token(model_id: str, row: Any) -> str:
    """Token to send as ``voice`` on Together ``/audio/speech``.

    Orpheus and Kokoro use the voice name. Cartesia docs say pass the voice
    id, not the display name, when the row includes ``id``.
    https://docs.together.ai/docs/inference/text-to-speech/overview
    """
    if isinstance(row, str):
        return row.strip()
    if not isinstance(row, dict):
        return ""
    name = row.get("name")
    name_s = name.strip() if isinstance(name, str) else ""
    raw_id = row.get("id")
    id_s = raw_id.strip() if isinstance(raw_id, str) else ""
    if "cartesia" in model_id.casefold() and id_s:
        return id_s
    return name_s or id_s


def _together_voice_groups(data: Any, requested_model: str | None) -> list[tuple[str, list[Any]]]:
    """Normalize list-all ``{data:[{model, voices}]}`` and filtered ``{model, voices}``."""
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        groups: list[tuple[str, list[Any]]] = []
        for item in data["data"]:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("model") or requested_model or "").strip()
            raw_voices = item.get("voices")
            if mid and isinstance(raw_voices, list):
                groups.append((mid, raw_voices))
        return groups
    if isinstance(data, dict) and isinstance(data.get("voices"), list):
        mid = str(data.get("model") or requested_model or "").strip()
        if mid:
            return [(mid, data["voices"])]
    return []


def _voices_from_together_body(data: Any, requested_model: str | None) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for mid, raw_voices in _together_voice_groups(data, requested_model):
        tokens: list[str] = []
        for row in raw_voices:
            token = _together_voice_token(mid, row)
            if token and token not in tokens:
                tokens.append(token)
        if tokens:
            found[mid] = tokens
    return found


def fetch_together_tts_voices(endpoint: str, model_id: str | None = None, api_key_override: str | None = None) -> dict[str, list[str]] | None:
    """GET Together ``/v1/voices`` and copy tokens into ``_tts_supported_voices``.

    ``model_id`` set: ``GET /v1/voices?model=``. Omitted: list every model.
    Unauthenticated listing often works; a saved or dialog key is still sent.
    None when the host is not Together or the request fails. Memoized like
    the other model fetches. The combo reads ``cached_tts_supported_voices``.
    """
    if not endpoint:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base or not endpoint_url_suitable_for_v1_models_fetch(base):
        return None
    if get_provider_from_endpoint(base) != "together":
        return None

    requested = str(model_id or "").strip()
    is_owu = get_config_bool_safe("is_openwebui")
    url = _together_voices_url(base, requested)
    cache_key = _model_fetch_cache_key(url, base, api_key_override)
    if cache_key in _together_voices_fetch_cache:
        return _together_voices_fetch_cache[cache_key]

    req_headers = _model_fetch_auth_headers(base, api_key_override, url, is_openwebui=is_owu, is_openrouter=False, log_label="fetch together voices")
    if req_headers is None:
        return None

    try:
        from plugin.framework.client.requests import sync_request

        data = sync_request(url, parse_json=True, headers=req_headers, timeout=_MODEL_FETCH_TIMEOUT)
        found = _voices_from_together_body(data, requested or None)
        for speech_id, speech_voices in found.items():
            remember_tts_supported_voices(speech_id, speech_voices)
        _together_voices_fetch_cache[cache_key] = found
        return found
    except Exception as e:
        log.warning("fetch together voices failed for %s: %s", url, e)
    return None


def preferred_openrouter_tts_model_id(model_id: str) -> str:
    """Speech-list spelling of ``model_id`` when the cache has one.

    The catalog default is ``hexgrad/Kokoro-82M``; the speech list returns
    ``hexgrad/kokoro-82m``. When that list is cached, speak sends the API id.
    With a cold cache the saved id is unchanged, so either casing still speaks.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return mid
    folded = mid.casefold()
    for ids in _model_fetch_tts_cache.values():
        if not ids:
            continue
        for api_id in ids:
            if api_id.casefold() == folded:
                return api_id
    return mid


def _filter_fetched_models(models: list[str], req_cap: str) -> list[str]:
    """Filter raw model IDs from /v1/models based on the requested capability (text/image/audio)."""
    if not models:
        return []

    out = []
    if req_cap == "text":
        # Exclude known non-chat models (mirrors LibreAI C++ logic)
        # vision / llava / coder used to hide chat models (Gemini vision, Qwen coder).
        # Specific non-chat names stay: codellama, whisper, dall-e, and the rest.
        exclude = {"embedding", "embed", "aqa", "attribution", "retrieval", "rerank", "classifier", "moderation", "whisper", "speech", "audio", "stable-diffusion", "sdxl", "dall", "aurora", "imagen", "codellama", "codegemma", "starcoder", "deepseek-coder"}
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in exclude):
                continue
            out.append(m)
    elif req_cap == "image":
        # Ollama / local / Google models matching image keywords
        include = {"flux", "stable-diffusion", "sdxl", "dall-e", "aurora", "imagen", "dreamshaper", "playground", "juggernaut", "-image"}
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in include):
                out.append(m)
    elif req_cap == "tts":
        include = {"tts", "kokoro", "sonic", "speech"}
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in include):
                out.append(m)
    else:
        # Audio/STT name heuristics for local /v1/models. OpenRouter STT uses
        # fetch_available_stt_models (output_modalities=transcription). Together's
        # /v1/models type enum has no transcription value.
        include = {"whisper", "voxtral", "parakeet", "transcribe", "speech", "asr"}
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in include):
                out.append(m)
    return out


# --- Provider and Endpoint resolution ---


def get_endpoint_presets() -> list[tuple[str, str]]:
    """Return list of (label, url) for endpoint selector, in display order."""
    return list(ENDPOINT_PRESETS)


# --- Model capability and audio support ---


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
            from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent

            if openrouter_model_ids_equivalent(effective_id, model_id):
                return m.get("capability", ModelCapability.CHAT)
        elif effective_id == model_id:
            return m.get("capability", ModelCapability.CHAT)
    return ModelCapability.NONE


def has_native_audio(model_id: Any, endpoint: Any) -> bool | None:
    """True if the model accepts input_audio on POST /v1/chat/completions.

    This is not the same as "can transcribe": STT-only models (Voxtral, Whisper)
    transcribe via POST /v1/audio/transcriptions instead. See docs/chat/audio-architecture.md.

    Uses persistent cache first, then catalog/heuristics.
    Returns: True if supported, False if unsupported, None if unknown.
    """
    model_id = str(model_id).lower()
    endpoint = normalize_endpoint_url(endpoint)

    # 1. Persistent Cache Check
    cache = get_config("audio_support_map")
    if isinstance(cache, dict):
        key = f"{endpoint}@{model_id}"
        if key in cache:
            return as_bool(cache[key])

    # 2. Catalog check — native audio input is via chat completions; STT-only models (AUDIO, no CHAT) use /audio/transcriptions.
    caps = get_model_capability(model_id, endpoint)
    if isinstance(caps, int) and caps != ModelCapability.NONE:
        has_audio = bool(caps & ModelCapability.AUDIO)
        has_chat = bool(caps & ModelCapability.CHAT)
        if has_audio and has_chat:
            return True
        if has_audio and not has_chat:
            # AUDIO-without-CHAT (Whisper, Voxtral) is not "unknown".
            # transcribe_audio treats None as "try chat", so those ids were
            # posted as input_audio and could sit until request_timeout.
            # False selects POST /audio/transcriptions. None stays unknown.
            return False

    # 3. Heuristics (Regex/Keywords) for known audio-native families
    # Gemini (Flash/Pro 1.5+)
    if "gemini" in model_id and "1.5" in model_id:
        return True
    # Explicit audio models
    if "audio-preview" in model_id or "multimodal" in model_id:
        return True
    # Catalog miss: dedicated STT names are not chat-audio. Unknown other ids
    # stay None so uncatalogued Gemini-like models still try chat.
    if "whisper" in model_id or "parakeet" in model_id:
        return False

    return None  # Unknown, allow trying native audio


def _update_support_map(config_key: str, map_key: str, supported: bool) -> None:
    """Read-modify-write one support map under the config write lock.

    Uses this module's ``get_config`` / ``set_config`` names so tests that
    patch them still see the write. The lock is the same one ``set_config``
    takes, so a second writer cannot replace the map with a stale copy.
    """
    from plugin.framework.config import _config_write_lock

    with _config_write_lock:
        cache = get_config(config_key)
        if not isinstance(cache, dict):
            cache = {}
        else:
            cache = dict(cache)
        cache[map_key] = bool(supported)
        set_config(config_key, cache)


def set_native_audio_support(model_id: Any, endpoint: Any, supported: bool) -> None:
    """Save the audio support status for a model+endpoint pair."""
    model_id = str(model_id).lower()
    endpoint = normalize_endpoint_url(endpoint)
    key = f"{endpoint}@{model_id}"
    _update_support_map("audio_support_map", key, supported)


# --- Resolved model getters (text / STT / grammar / image) ---


def _sanitize_stored_model_value(val: Any) -> str:
    """Drop combobox placeholder strings from persisted model config."""
    from plugin.chatbot.config_ui_helpers import _sanitize_model_combobox_value

    return _sanitize_model_combobox_value(str(val or ""))


def _stored_or_default(config_key: str, default_field: str) -> str:
    """Stored model id, or the current endpoint's provider default."""
    val = _sanitize_stored_model_value(get_config(config_key))
    if val:
        return val
    current_endpoint = get_current_endpoint()
    provider = get_provider_from_endpoint(current_endpoint)
    defaults = get_provider_defaults(provider)
    return str(defaults.get(default_field, "") or "").strip()


def get_text_model() -> str:
    """Return the text/chat model (stored as ``text_model``)."""
    return _stored_or_default("text_model", "text_model")


def get_stt_model() -> str:
    """Return the configured STT model.

    Settings → Speech stores ``audio.stt_model``. Configs saved before that
    move still have top-level ``stt_model``; prefer the new key when it is
    non-empty, otherwise the legacy key, otherwise the provider default.
    """
    val = _sanitize_stored_model_value(get_config("audio.stt_model"))
    if val:
        return val
    # Flat ``stt_model`` wins over the dotted alias inside get_config, so a
    # leftover legacy value is still visible when the new key is empty.
    legacy = _sanitize_stored_model_value(get_config("stt_model"))
    if legacy:
        return legacy
    return _stored_or_default("audio.stt_model", "stt_model")


def get_tts_model() -> str:
    """Return the configured TTS model, or default for the current endpoint's provider."""
    return _stored_or_default("audio.tts_model", "tts_model")


def set_tts_model(val: Any, update_lru: bool = True) -> None:
    """Set TTS model and optionally update tts_model_lru for the current endpoint."""
    if val is None:
        return
    val_str = _sanitize_stored_model_value(val)
    if not val_str:
        return

    current = str(get_config("audio.tts_model") or "").strip()
    if val_str == current:
        return

    set_config("audio.tts_model", val_str)
    if update_lru:
        from plugin.chatbot.config_ui_helpers import update_lru_history

        update_lru_history(val_str, "tts_model_lru", get_current_endpoint())


def get_grammar_model() -> str:
    """Return the configured grammar model, fallback to chat text model."""
    val = str(get_config("doc.grammar_proofreader_model") or "").strip()
    if val:
        return val
    return get_text_model()


def get_image_model() -> str:
    """Return current image model for endpoint-based generation."""
    return _stored_or_default("image_model", "image_model")


def set_text_model(val: Any, update_lru: bool = True, *, event_key: str | None = None) -> None:
    """Set text/chat model and optionally update model_lru for the current endpoint."""
    if val is None:
        return
    val_str = _sanitize_stored_model_value(val)
    if not val_str:
        return

    current = str(get_config("text_model") or "").strip()
    if val_str == current:
        return

    set_config("text_model", val_str, event_key=event_key)
    if update_lru:
        from plugin.chatbot.config_ui_helpers import update_lru_history

        update_lru_history(val_str, "model_lru", get_current_endpoint())


def set_image_model(val: Any, update_lru: bool = True, *, event_key: str | None = None) -> None:
    """Set image model and notify listeners."""
    if val is None:
        return
    val_str = _sanitize_stored_model_value(val)
    if not val_str:
        return

    current = str(get_config("image_model") or "").strip()
    if val_str == current:
        return

    set_config("image_model", val_str, event_key=event_key)
    if update_lru:
        from plugin.chatbot.config_ui_helpers import update_lru_history

        update_lru_history(val_str, "image_model_lru", get_current_endpoint())


def _model_in_vision_list(provider: str, vision_list: list[str], model_id: str) -> bool:
    """True when provider metadata lists this id as accepting image input."""
    if provider == "openrouter":
        from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent

        return any(openrouter_model_ids_equivalent(v_id, model_id) for v_id in vision_list)
    return model_id in vision_list


def _declared_vision_flag(provider: str | None, declared: dict[str, bool], model_id: str) -> bool | None:
    """True/False when this id's row listed input_modalities. None if it did not."""
    if provider == "openrouter":
        for declared_id, flag in declared.items():
            if openrouter_model_ids_equivalent(declared_id, model_id):
                return bool(flag)
        return None
    if model_id in declared:
        return bool(declared[model_id])
    return None


def _vision_memo_answer(provider: str | None, memo: list[str] | dict[str, bool] | None, model_id: str) -> bool | None:
    """Answer to persist, or None when the catalog did not say.

    A dict is the parse: only ids whose row listed input_modalities. A list is
    positive ids only. A hit on that list is yes. A miss is not a no — the
    list does not record an omitted field.
    """
    if isinstance(memo, dict):
        return _declared_vision_flag(provider, memo, model_id)
    if isinstance(memo, list) and _model_in_vision_list(provider or "", memo, model_id):
        return True
    return None


def _remember_vision_support(model_id: str, endpoint: str, supported: bool) -> None:
    """Persist a modalities answer. A write failure must not change the answer."""
    try:
        set_native_vision_support(model_id, endpoint, supported)
    except Exception as e:
        log.debug("has_native_vision persist failed: %s", e)


def has_native_vision(model_id: Any, endpoint: Any, *, allow_fetch: bool = True, unknown_is_vision: bool = False) -> bool:
    """Check if the model supports native multimodal vision input.

    Priority order:
    1. Persistent user config (``vision_support_map``), including an explicit False.
    2. Static default models list (``ModelCapability.VISION``). Defaults only —
       uncatalogued ids are not added there.
    3. Provider metadata:
       - OpenRouter/Together: ``input_modalities`` contains ``image``. The sidebar
         does not GET ``/v1/models`` for these hosts (the lists are huge). On a
         process-cache miss this function fetches once, then remembers an
         explicit yes or no. A row that omitted ``input_modalities`` is not stored.
       - Ollama: ``POST /api/show`` capabilities list.

    ``allow_fetch=False`` reads only those caches. Chat Send runs on the UI
    thread; a cold catalog GET (10s, up to 3 attempts) would freeze LibreOffice.
    ``unknown_is_vision`` is only for that UI gate: a model with no static row
    and no stored answer stays True so ``get_image`` is not hidden. Callers
    that attach an image keep the default False (do not attach when unknown).
    """
    if not model_id:
        return False
    model_id_str = str(model_id).strip()
    endpoint_str = normalize_endpoint_url(endpoint or "")

    # 1. Persistent User Config Cache check
    try:
        cache = get_config("vision_support_map")
        if isinstance(cache, dict):
            key = f"{endpoint_str}@{model_id_str.lower()}"
            if key in cache:
                return as_bool(cache[key])
    except Exception as e:
        log.debug("has_native_vision config cache read exception: %s", e)

    # 2. Static Default Models check
    caps = get_model_capability(model_id_str, endpoint_str)
    log.debug("has_native_vision: model=%r endpoint_str=%r caps=%r catalog_vision=%s", model_id_str, endpoint_str, caps, bool(caps & ModelCapability.VISION))
    if caps & ModelCapability.VISION:
        return True

    provider = get_provider_from_endpoint(endpoint_str)

    # 3. Dynamic provider metadata
    # 3a. OpenRouter / Together. Combobox population skips these hosts so the
    # dropdown stays LRU + defaults. Vision still needs architecture.input_modalities.
    # fetch_available_models memoizes a successful GET for the process. A failed
    # lookup is not stored, so the next send retries. Do not cache None: a
    # transient miss would hide vision until LibreOffice restarts.
    if provider in ("openrouter", "together"):
        is_owu = get_config_bool_safe("is_openwebui")
        suffix = get_api_version_suffix(endpoint_str, is_openwebui=is_owu)
        url = f"{endpoint_str}{suffix}/models"
        cache_key = _model_fetch_cache_key(url, endpoint_str)
        vision_memo = _model_fetch_vision_cache.get(cache_key)
        if vision_memo is None and allow_fetch:
            fetch_available_models(endpoint_str)
            vision_memo = _model_fetch_vision_cache.get(cache_key)
        # Persist vision only when the row listed input_modalities. A
        # successful catalog that omitted the field is not a "no": the config
        # map is checked first, so storing False would skip later lookups for
        # the rest of the process and the next one. An explicit list without
        # image is still a no. A missing field falls through and is not written.
        if vision_memo is not None:
            supported = _vision_memo_answer(provider, vision_memo, model_id_str)
            if supported is not None:
                _remember_vision_support(model_id_str, endpoint_str, supported)
                log.debug("has_native_vision: modalities model=%r vision=%s", model_id_str, supported)
                return supported

    # 3b. Ollama (query POST /api/show). None means the probe did not answer.
    # allow_fetch=False still honors a process cache hit from an earlier probe.
    if provider == "ollama":
        if not allow_fetch:
            cached_show = _ollama_show_cache.get(f"{endpoint_str}@{model_id_str}")
            if isinstance(cached_show, dict):
                show_caps = cached_show.get("capabilities") or []
                return "vision" in show_caps if isinstance(show_caps, list) else False
        else:
            try:
                res = query_ollama_model_capabilities(endpoint_str, model_id_str)
                if res is not None:
                    return res
            except Exception as e:
                log.debug("Ollama /api/show capability query failed: %s", e)

    # A static row without VISION is a real "no" (caps != NONE). No row and
    # no modalities/map answer above is "not looked up". The UI tool gate
    # must not treat that as text-only or it hides get_image from uncatalogued
    # vision models it refused to fetch.
    if unknown_is_vision and caps == ModelCapability.NONE:
        return True
    return False


def set_native_vision_support(model_id: Any, endpoint: Any, supported: bool) -> None:
    """Save the vision support status for a model+endpoint pair to config."""
    model_id_str = str(model_id).strip().lower()
    endpoint_str = normalize_endpoint_url(endpoint or "")
    key = f"{endpoint_str}@{model_id_str}"
    _update_support_map("vision_support_map", key, supported)


def parse_ollama_runtime_num_ctx(show_body: Any) -> int | None:
    """Runtime ``num_ctx`` from Ollama ``/api/show`` parameters or Modelfile.

    Trained ``model_info["*.context_length"]`` is ignored on purpose: issue #570
    crashed with live ``n_ctx=4096`` while ``qwen2.context_length`` was 32768.
    A missing or unparsable ``num_ctx`` returns None (crash copy stays generic).
    """
    if not isinstance(show_body, dict):
        return None
    parsed = _num_ctx_from_parameters(show_body.get("parameters"))
    if parsed is not None:
        return parsed
    return _num_ctx_from_modelfile(show_body.get("modelfile"))


def _num_ctx_from_parameters(parameters: Any) -> int | None:
    if isinstance(parameters, dict):
        for key, val in parameters.items():
            if str(key).strip().lower() == "num_ctx":
                return _parse_positive_ctx(val)
        return None
    if isinstance(parameters, str):
        for line in parameters.splitlines():
            matched = _OLLAMA_NUM_CTX_LINE.match(line)
            if matched:
                return _parse_positive_ctx(matched.group(1))
    return None


def _num_ctx_from_modelfile(modelfile: Any) -> int | None:
    if not isinstance(modelfile, str):
        return None
    last: int | None = None
    for line in modelfile.splitlines():
        matched = _OLLAMA_NUM_CTX_LINE.match(line)
        if matched:
            last = _parse_positive_ctx(matched.group(1))
    return last


def query_ollama_show(endpoint: str, model_id: str) -> dict[str, Any] | None:
    """POST ``/api/show`` once per process. Returns capabilities + runtime num_ctx."""
    if not endpoint or not model_id:
        return None
    endpoint = normalize_endpoint_url(endpoint)
    cache_key = f"{endpoint}@{model_id}"
    cached = _ollama_show_cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"{endpoint}/api/show"
    req_body = {"model": model_id}
    try:
        from plugin.framework.client.requests import sync_request

        headers = {"Content-Type": "application/json"}
        res = sync_request(url, data=json.dumps(req_body).encode("utf-8"), headers=headers, parse_json=True, timeout=_MODEL_FETCH_TIMEOUT)
        if isinstance(res, dict):
            caps = res.get("capabilities") or []
            if not isinstance(caps, list):
                caps = []

            # fallback: look inside model_info (vision projector keys only)
            model_info = res.get("model_info") or {}
            if isinstance(model_info, dict):
                for k in model_info.keys():
                    if "vision" in k or "projector" in k:
                        if "vision" not in caps:
                            caps.append("vision")
                        break

            info = {"capabilities": caps, "num_ctx": parse_ollama_runtime_num_ctx(res)}
            _ollama_show_cache[cache_key] = info
            return info
    except Exception as e:
        log.debug("query_ollama_show failed: %s", e)
    return None


def query_ollama_model_capabilities(endpoint: str, model_id: str) -> bool | None:
    """Query POST /api/show to check if an Ollama model supports vision."""
    try:
        info = query_ollama_show(endpoint, model_id)
    except Exception as e:
        log.debug("query_ollama_model_capabilities failed: %s", e)
        return None
    if info is None:
        return None
    caps = info.get("capabilities") or []
    return "vision" in caps if isinstance(caps, list) else False


def query_ollama_runtime_num_ctx(endpoint: str, model_id: str) -> int | None:
    """Live Ollama ``num_ctx`` from the cached ``/api/show`` body, or None."""
    if not endpoint or not model_id:
        return None
    try:
        info = query_ollama_show(endpoint, model_id)
    except Exception as e:
        log.debug("query_ollama_runtime_num_ctx failed: %s", e)
        return None
    if not info:
        return None
    num_ctx = info.get("num_ctx")
    if isinstance(num_ctx, int) and num_ctx > 0:
        return num_ctx
    return None


def cached_v1_context_tokens(endpoint: str, model_id: str, provider: str | None = None) -> int | None:
    """Already-memoized /v1/models window for ``model_id``, or None. Does not HTTP.

    Compact must not fetch: OpenRouter/Together lists are huge and the sidebar
    skips those providers on purpose. Cache miss → caller falls back to catalog.
    """
    if not endpoint or not model_id:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base or not endpoint_url_suitable_for_v1_models_fetch(base):
        return None
    is_owu = get_config_bool_safe("is_openwebui")
    suffix = get_api_version_suffix(base, is_openwebui=is_owu)
    cache_key = _model_fetch_cache_key(f"{base}{suffix}/models", base, None)
    lengths = _model_context_cache.get(cache_key)
    if not isinstance(lengths, dict) or not lengths:
        return None
    mid = str(model_id).strip()
    tokens = lengths.get(mid)
    if isinstance(tokens, int) and tokens > 0:
        return tokens
    if provider == "openrouter":
        for cached_id, cached_tokens in lengths.items():
            if isinstance(cached_tokens, int) and cached_tokens > 0 and openrouter_model_ids_equivalent(cached_id, mid):
                return cached_tokens
    return None


def is_image_only_model(endpoint: Any, model_id: Any) -> bool:
    """Check if the model outputs image but not text (dedicated image generator).

    The image-id list the fetch already returns is the catalog. A side map
    the catalog never filled made a real image id fall through to the name
    heuristic (a ``gemini`` image model looked like chat). ``None`` means the
    catalog did not answer; then the name heuristic still applies.
    """
    if not endpoint or not model_id:
        return False
    image_ids = fetch_available_image_models(str(endpoint))
    if image_ids is not None:
        return str(model_id) in image_ids

    # Fallback to name-based heuristic if metadata is not present (e.g. Ollama or custom local endpoints)
    lower_model = str(model_id).lower()
    is_chat = any(x in lower_model for x in ("gemini", "gpt", "claude", "llama", "mixtral", "qwen", "deepseek"))
    if is_chat:
        return False
    return any(x in lower_model for x in ("flux", "stable-diffusion", "sdxl", "dall-e", "dall-3", "imagen", "seedream", "midjourney", "playground", "aurora")) or lower_model.endswith("-image") or "/image" in lower_model
