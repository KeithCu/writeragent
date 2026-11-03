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
serialize identical fetches. Iteration over shared caches snapshots the
dict first (e.g. ``list(...)``); a single-key write still needs no lock.
When a UNO ``ctx`` is passed, the cache key includes a hash of the API
key so two keys on the same host do not share a list and a dump of the
dict does not contain the key. OpenWebUI catalog GETs use the same
hostname-or-checkbox rule as ``get_api_config`` (``/api/models``, and a pasted
``/api`` is stripped once on the HTTP base). The hash is still the saved key for
the URL normalized **without** that strip, unless the caller passed
``api_key_override``.

Settings treats a successful OpenRouter or Together catalog as once per
process. ``settings_catalog_is_warm`` is that check. **Test Connection**
clears the memo with ``clear_settings_catalog_cache``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import urllib.parse
import json
import ipaddress
import logging
import re
import threading
from typing import Any

from plugin.framework.constants import ModelCapability
from plugin.framework.default_models import (
    DEFAULT_MODELS,
    get_model_capability,
    get_provider_defaults,
    resolve_model_id,
)
from plugin.framework.url_utils import normalize_endpoint_url, get_api_version_suffix
from plugin.framework.client.provider_detection import get_provider_from_endpoint, is_openrouter_endpoint, is_openwebui_endpoint
from plugin.framework.errors import NetworkError, best_effort
from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent, resolve_openrouter_catalog_id
from plugin.framework.queue_executor import main
from plugin.framework.worker_pool import run_in_background
from plugin.framework.config import get_api_key_for_endpoint, get_config, get_current_endpoint, save, update_config_mapping
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


_TTS_RESPONSE_FORMATS = ("mp3", "pcm", "wav")


@dataclass
class CatalogMemo:
    """Catalog metadata memoized for one endpoint-plus-key slot."""

    ids: list[str] | None = None
    image_ids: list[str] | None = None
    vision_declarations: dict[str, bool] | None = None
    context_tokens: dict[str, int] = field(default_factory=dict)
    tts_ids: list[str] | None = None
    stt_ids: list[str] | None = None
    together_voices: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    ollama_show: dict[str, dict[str, Any]] = field(default_factory=dict)

    def is_warm(self, provider: str | None) -> bool:
        """True when this memo has the catalogs Settings would show."""
        if self.ids is None or self.image_ids is None:
            return False
        if provider == "openrouter":
            if self.tts_ids is None:
                return False
            return self.stt_ids is not None
        if provider == "together":
            return bool(self.together_voices)
        return True


_catalog_memos: dict[str, CatalogMemo] = {}

# Per speech model id, process lifetime, outside CatalogMemo so Test Connection keeps them.
# OpenRouter supported_voices and Together /v1/voices both fill the voice map.
_tts_supported_voices: dict[str, list[str]] = {}
# Learned response_format (mp3 / pcm / wav); Gemini-class models reject mp3.
_tts_response_format: dict[str, str] = {}


def _model_key(table: dict[str, Any], model_id: str) -> str:
    """Existing key that case-folds to *model_id*, else *model_id*."""
    folded = model_id.casefold()
    for existing in table:
        if existing.casefold() == folded:
            return existing
    return model_id
# Keys in flight for background vision capability resolution (UI thread schedule guard).
_vision_in_flight_lock = threading.Lock()
_vision_in_flight: set[str] = set()

# Runtime PARAMETER / Modelfile line. Do not use this on model_info context_length.
_OLLAMA_NUM_CTX_LINE = re.compile(r"(?im)^\s*(?:PARAMETER\s+)?num_ctx\s+(\d+)\s*$")

# /v1/models response shapes (GET {endpoint}/v1/models):
# - Together (api.together.xyz): top-level JSON array [{id, type, ...}, ...]; image rows use type="image".
# - OpenRouter (openrouter.ai): {data: [...]}; image rows use architecture.output_modalities (not slug names).
#   TTS is GET /v1/models?output_modalities=speech (not audio). STT is output_modalities=transcription.
# - OpenAI-compatible (Ollama, LM Studio, most hosted chat APIs): {data: [{id}, ...]}; image models
#   are not typed — the image memo for those hosts is the slug keyword filter
#   (flux, sdxl, …), the same list fetch_available_image_models returns.
# Image-output IDs are stored at fetch time in CatalogMemo.image_ids. Settings
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
        arch = m.get("architecture")
        if not isinstance(arch, dict):
            arch = {}
        modalities = arch.get("output_modalities")
        # OpenRouter: google/gemini-2.5-flash-image, openai/gpt-5-image, etc.
        if isinstance(modalities, list) and "image" in modalities:
            out.append(str(mid))
        # Together: google/flash-image-2.5, black-forest-labs/FLUX.* — type enum, not architecture.
        elif str(m.get("type") or "").lower() == "image":
            out.append(str(mid))
    return out


def _stated_input_modalities(row: dict[str, Any]) -> list[Any] | None:
    """input_modalities when this row states a list. None when the field is absent.

    An empty list is a statement (no image). A missing field is not.
    A non-dict architecture is treated as field absent; a top-level list
    on the row still counts.
    """
    arch = row.get("architecture")
    nested = None
    if isinstance(arch, dict):
        nested = arch.get("input_modalities")
    chosen = row.get("input_modalities")
    if chosen is None:
        chosen = nested
    if isinstance(chosen, list):
        return chosen
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
    input_modalities.
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
    return models, image_models, _vision_declarations_from_v1_entries(entries)


def _resolved_openwebui(endpoint: str) -> bool:
    # Resolve Open WebUI by host or explicit setting matching LlmClient._api_path.
    return is_openwebui_endpoint(endpoint, explicit_is_openwebui=as_bool(get_config("is_openwebui")))


def _key_slot_base(endpoint: str) -> str:
    if not endpoint or not isinstance(endpoint, str):
        return ""
    return normalize_endpoint_url(endpoint, is_openwebui=False)


def _v1_prefix(endpoint: str) -> str:
    if not endpoint or not isinstance(endpoint, str):
        http_base = ""
    else:
        http_base = normalize_endpoint_url(endpoint, is_openwebui=_resolved_openwebui(endpoint))
    if not http_base or not endpoint_url_suitable_for_v1_models_fetch(http_base):
        return ""
    return f"{http_base}{get_api_version_suffix(http_base, is_openwebui=_resolved_openwebui(http_base))}"


def _v1_url(endpoint: str, path: str, **query: str) -> str:
    """``prefix + path`` when the endpoint can serve ``/v1``, else ``""``.

    ``path`` includes the leading slash. An empty query value is left off so
    ``model=""`` stays the list-all voices URL.
    """
    prefix = _v1_prefix(endpoint)
    if not prefix:
        return ""
    url = f"{prefix}{path}"
    parts = {key: value for key, value in query.items() if value != ""}
    if not parts:
        return url
    return f"{url}?{urllib.parse.urlencode(parts)}"


def _catalog_base(endpoint: str) -> str:
    if not _v1_url(endpoint, "/models"):
        return ""
    return _key_slot_base(endpoint)


def _catalog_memo_key(endpoint: str, api_key_override: str | None = None) -> str:
    """Cache key for endpoint catalog memoization.

    Both the override and the saved key are stripped so they match the key
    ``build_auth_headers`` sends.
    """
    if not endpoint or not isinstance(endpoint, str):
        return ""
    base = _v1_prefix(endpoint) or normalize_endpoint_url(endpoint)
    if not base:
        return ""
    key_slot = _key_slot_base(endpoint)
    if api_key_override is not None:
        key = str(api_key_override).strip()
    else:
        key = str(get_api_key_for_endpoint(key_slot) or "").strip()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{base}\x1f{digest}"


def _get_catalog_memo(endpoint: str = "", api_key_override: str | None = None) -> CatalogMemo:
    key = _catalog_memo_key(endpoint, api_key_override)
    memo = _catalog_memos.get(key)
    if memo is None:
        memo = CatalogMemo()
        _catalog_memos[key] = memo
    return memo


def _v1_models_request(endpoint: str, api_key_override: str | None = None) -> tuple[str, str, str] | None:
    url = _v1_url(endpoint, "/models")
    if not url:
        return None
    key_slot = _key_slot_base(endpoint)
    return key_slot, url, _catalog_memo_key(endpoint, api_key_override)


def endpoint_url_suitable_for_v1_models_fetch(endpoint: str) -> bool:
    """True if endpoint looks like a complete http(s) URL with a real host and valid port.

    Skips mid-typing (e.g. 'http:/'), half-typed schemes, and non-numeric or
    out-of-range ports.
    """
    if not endpoint or not isinstance(endpoint, str):
        return False
    try:
        p = urllib.parse.urlparse(endpoint.strip())
        # Accessing .port raises ValueError on invalid port strings (e.g. '1a') or out-of-range ints (>65535).
        # Reject bad port values at the suitability gate.
        getattr(p, "port")
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

    Successful responses are cached in the endpoint's ``CatalogMemo`` for the process
    lifetime. Failed lookups are not cached, so the next Settings open retries.
    Settings skips the request when ``settings_catalog_is_warm`` is already true.
    """
    built = _v1_models_request(endpoint, api_key_override)
    if built is None:
        return None
    key_slot, url, _ = built
    memo = _get_catalog_memo(endpoint, api_key_override)
    if memo.ids is not None:
        return list(memo.ids)

    is_openwebui = _resolved_openwebui(endpoint)
    # Hostname equality, same rule as get_provider_from_endpoint. A path that
    # merely contains "openrouter.ai" is not this provider.
    is_openrouter = is_openrouter_endpoint(key_slot, explicit_is_openrouter=as_bool(get_config("is_openrouter")))
    req_headers = _model_fetch_auth_headers(key_slot, api_key_override, url, is_openwebui=is_openwebui, is_openrouter=is_openrouter, log_label="fetch_available_models")
    if req_headers is None:
        return None

    try:
        from plugin.framework.client.requests import sync_request

        data = sync_request(url, parse_json=True, headers=req_headers, timeout=_MODEL_FETCH_TIMEOUT)
        parsed = _parse_v1_models_response(data)
        if parsed is not None:
            models, image_models, vision_declared = parsed
            entries = _v1_models_entries_from_body(data) or []
            provider = get_provider_from_endpoint(key_slot)
            # Ollama, LM Studio, and other non-Together hosts usually omit
            # architecture/type image ids, so a memo of only those ids is []
            # while keyword matching finds flux/sdxl/…. Settings reads the
            # memo, and the startup worker does not do a second image GET for
            # those hosts. Store the keyword list the image fetch returns.
            # Together keeps type=image. OpenRouter Settings uses
            # /v1/images/models, not this slice.
            if provider not in ("together", "openrouter"):
                image_models = _filter_fetched_models(models, "image")
            memo.ids = list(models)
            if provider != "openrouter":
                memo.image_ids = list(image_models) if image_models is not None else None
            memo.vision_declarations = dict(vision_declared) if isinstance(vision_declared, dict) else None
            memo.context_tokens = _context_tokens_from_v1_entries(entries)
            return list(models)
        log.warning("fetch_available_models unexpected body structure for %s: %s", url, type(data).__name__)
    except NetworkError as e:
        log.warning("fetch_available_models NetworkError for %s: %s", url, e)
    except (ValueError, TypeError, IOError) as e:
        log.warning("fetch_available_models network/parse error for %s: %s", url, e)
    except Exception:
        log.exception("fetch_available_models unexpected error for %s", url)
    return None


def _fetch_openrouter_entries(
    endpoint: str,
    key_slot: str,
    api_key_override: str | None,
    url: str,
    log_label: str,
) -> list[Any] | None:
    req_headers = _model_fetch_auth_headers(
        key_slot,
        api_key_override,
        url,
        is_openwebui=_resolved_openwebui(endpoint),
        is_openrouter=True,
        log_label=log_label,
    )
    if req_headers is None:
        return None
    try:
        from plugin.framework.client.requests import sync_request

        data = sync_request(url, parse_json=True, headers=req_headers, timeout=_MODEL_FETCH_TIMEOUT)
        return _v1_models_entries_from_body(data)
    except Exception as e:
        log.warning("%s failed for %s: %s", log_label, url, e)
    return None


def fetch_available_image_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Image-output model IDs. Same list ``cached_image_models`` returns. No extra GET.

    Provider policy (see module comment above):
    - openrouter: queries /v1/images/models.
    - together: metadata only (type=image) from the standard /v1/models memo.
    - ollama / lm studio / custom: keyword filter (flux, sdxl, …) stored with
      that same /v1/models memo. The catalog is not fetched again here.
    """
    built = _v1_models_request(endpoint, api_key_override)
    if built is None:
        return None
    key_slot, _url, _ = built
    memo = _get_catalog_memo(endpoint, api_key_override)
    provider = get_provider_from_endpoint(key_slot)

    if provider == "openrouter":
        if memo.image_ids is not None:
            return list(memo.image_ids)

        url = _v1_url(endpoint, "/images/models")
        entries = _fetch_openrouter_entries(
            endpoint, key_slot, api_key_override, url, "fetch_available_image_models openrouter"
        )
        if entries is not None:
            image_models = []
            for m in entries:
                if isinstance(m, dict) and m.get("id"):
                    image_models.append(str(m["id"]))
            memo.image_ids = list(image_models)
            return list(image_models)
        return None

    all_models = fetch_available_models(endpoint, api_key_override=api_key_override)
    if all_models is None:
        return None
    if isinstance(memo.image_ids, list):
        return list(memo.image_ids)
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


def _fetch_openrouter_modality_models(endpoint: str, modality: str, api_key_override: str | None) -> list[str] | None:
    """OpenRouter ``GET /v1/models?output_modalities=`` list, memoized like other fetches.

    Together's ``/v1/models`` type enum has no speech or transcription value, so
    this returns None there and the Speech tab keeps curated catalog rows.
    A failed lookup is not cached, same as ``fetch_available_models``.
    """
    if modality not in ("speech", "transcription"):
        return None
    if not endpoint:
        return None
    key_slot = _key_slot_base(endpoint)
    if not key_slot or not endpoint_url_suitable_for_v1_models_fetch(key_slot):
        return None
    if get_provider_from_endpoint(key_slot) != "openrouter":
        return None

    memo = _get_catalog_memo(endpoint, api_key_override)
    cached = memo.tts_ids if modality == "speech" else memo.stt_ids
    if cached is not None:
        return list(cached)

    url = _v1_url(endpoint, "/models", output_modalities=modality)
    if not url:
        return None

    entries = _fetch_openrouter_entries(
        endpoint, key_slot, api_key_override, url, f"fetch openrouter {modality} models"
    )
    if entries is not None:
        model_ids, voices = _modality_ids_from_entries(entries, modality)
        if modality == "speech":
            if voices:
                for speech_id, speech_voices in voices.items():
                    remember_tts_supported_voices(speech_id, speech_voices)
            memo.tts_ids = list(model_ids)
        else:
            memo.stt_ids = list(model_ids)
        return list(model_ids)
    return None


def fetch_available_tts_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """OpenRouter TTS model ids: ``GET /v1/models?output_modalities=speech``.

    Do not use ``output_modalities=audio`` — that is Lyria / gpt-audio, not this combo.
    """
    return _fetch_openrouter_modality_models(endpoint, "speech", api_key_override)


def fetch_available_stt_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """OpenRouter STT model ids: ``GET /v1/models?output_modalities=transcription``."""
    return _fetch_openrouter_modality_models(endpoint, "transcription", api_key_override)


def cached_text_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized ``/v1/models`` ids, or None. Does not HTTP."""
    built = _v1_models_request(endpoint, api_key_override)
    if built is None:
        return None
    memo = _get_catalog_memo(endpoint, api_key_override)
    return list(memo.ids) if isinstance(memo.ids, list) else None


def cached_image_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized image-output ids, or None. Does not HTTP.

    OpenRouter is ``GET /v1/images/models``. Together is the ``type=image`` slice
    stored with ``/v1/models``. Other hosts are the keyword filter (flux, sdxl, …)
    stored with that same response — the list ``fetch_available_image_models``
    returns. Settings reads this. It does not fetch the catalog again.
    """
    built = _v1_models_request(endpoint, api_key_override)
    if built is None:
        return None
    memo = _get_catalog_memo(endpoint, api_key_override)
    return list(memo.image_ids) if isinstance(memo.image_ids, list) else None


def cached_tts_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized OpenRouter speech ids, or None. Does not HTTP."""
    base = _catalog_base(endpoint)
    if not base or get_provider_from_endpoint(base) != "openrouter":
        return None
    memo = _get_catalog_memo(endpoint, api_key_override)
    return list(memo.tts_ids) if isinstance(memo.tts_ids, list) else None


def cached_stt_models(endpoint: str, api_key_override: str | None = None) -> list[str] | None:
    """Memoized OpenRouter transcription ids, or None. Does not HTTP."""
    base = _catalog_base(endpoint)
    if not base or get_provider_from_endpoint(base) != "openrouter":
        return None
    memo = _get_catalog_memo(endpoint, api_key_override)
    return list(memo.stt_ids) if isinstance(memo.stt_ids, list) else None


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
    key = _catalog_memo_key(endpoint, api_key_override)
    memo = _catalog_memos.get(key)
    if memo is None:
        return False
    return memo.is_warm(get_provider_from_endpoint(base))


def clear_settings_catalog_cache(endpoint: str, api_key_override: str | None = None) -> None:
    """Drop this endpoint+key's memo so the next fetch hits the network.

    Test Connection uses this. Other endpoints and keys stay cached, and so do
    the per-model speech voices and response formats.
    """
    key = _catalog_memo_key(endpoint, api_key_override)
    if key:
        _catalog_memos.pop(key, None)


def cached_tts_supported_voices(model_id: str) -> list[str]:
    """Voice tokens harvested for this speech model, if any.

    OpenRouter speech rows and Together ``/v1/voices`` share this map.
    ``hexgrad/Kokoro-82M`` and ``hexgrad/kokoro-82m`` are one entry.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return []
    return list(_tts_supported_voices.get(_model_key(_tts_supported_voices, mid), []))


def cached_tts_response_format(model_id: str) -> str | None:
    """``response_format`` learned for this speech model, if any.

    Absent means the caller should try ``mp3``. Lookup matches the voice cache:
    ``hexgrad/Kokoro-82M`` and ``hexgrad/kokoro-82m`` share one entry.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return None
    fmt = _tts_response_format.get(_model_key(_tts_response_format, mid))
    return fmt if fmt in _TTS_RESPONSE_FORMATS else None


def remember_tts_response_format(model_id: str, fmt: str) -> None:
    """Remember a speech ``response_format`` for this model until the process exits.

    Process-level, not per endpoint: Test Connection does not clear it.
    """
    mid = str(model_id or "").strip()
    clean = str(fmt or "").strip().lower()
    if not mid or clean not in _TTS_RESPONSE_FORMATS:
        return
    _tts_response_format[_model_key(_tts_response_format, mid)] = clean


def remember_tts_supported_voices(model_id: str, voices: list[str]) -> None:
    """Store speech voice tokens for ``model_id`` until this process exits.

    Process-level, not per endpoint: Test Connection does not clear them.
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
    _tts_supported_voices[_model_key(_tts_supported_voices, mid)] = clean


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
    """GET Together ``/v1/voices`` and copy tokens into ``voices``.

    ``model_id`` set: ``GET /v1/voices?model=``. Omitted: list every model.
    Unauthenticated listing often works; a saved or dialog key is still sent.
    None when the host is not Together or the request fails. Memoized like
    the other model fetches. The combo reads ``cached_tts_supported_voices``.
    """
    if not endpoint:
        return None
    key_slot = _key_slot_base(endpoint)
    if not key_slot or not endpoint_url_suitable_for_v1_models_fetch(key_slot):
        return None
    if get_provider_from_endpoint(key_slot) != "together":
        return None

    requested = str(model_id or "").strip()
    memo = _get_catalog_memo(endpoint, api_key_override)
    if requested in memo.together_voices:
        stored = memo.together_voices[requested]
        return {k: list(v) for k, v in stored.items()}

    url = _v1_url(endpoint, "/voices", model=requested)
    if not url:
        return None

    req_headers = _model_fetch_auth_headers(key_slot, api_key_override, url, is_openwebui=_resolved_openwebui(endpoint), is_openrouter=False, log_label="fetch together voices")
    if req_headers is None:
        return None

    try:
        from plugin.framework.client.requests import sync_request

        data = sync_request(url, parse_json=True, headers=req_headers, timeout=_MODEL_FETCH_TIMEOUT)
        found = _voices_from_together_body(data, requested or None)
        for speech_id, speech_voices in found.items():
            remember_tts_supported_voices(speech_id, speech_voices)
        stored_copy = {k: list(v) for k, v in found.items()}
        memo.together_voices[requested] = stored_copy
        return {k: list(v) for k, v in stored_copy.items()}
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
    for memo in _catalog_memos.values():
        if not memo.tts_ids:
            continue
        for api_id in memo.tts_ids:
            if api_id.casefold() == folded:
                return api_id
    return mid


# Specific non-chat names to exclude from text model listings.
_TEXT_MODEL_EXCLUDES = frozenset({
    "embedding", "embed", "aqa", "attribution", "retrieval", "rerank",
    "classifier", "moderation", "whisper", "speech", "audio",
    "stable-diffusion", "sdxl", "dall", "aurora", "imagen", "codellama",
    "codegemma", "starcoder", "deepseek-coder",
})

_INCLUDE_MODEL_KEYWORDS: dict[str, frozenset[str]] = {
    "image": frozenset({
        "flux", "stable-diffusion", "sdxl", "dall-e", "aurora", "imagen",
        "dreamshaper", "playground", "juggernaut", "-image",
    }),
    "tts": frozenset({"tts", "kokoro", "sonic", "speech"}),
}

# Audio/STT heuristics for local /v1/models, and the fallback for any other req_cap.
# OpenRouter STT uses fetch_available_stt_models. Together's type enum has no transcription value.
_AUDIO_MODEL_KEYWORDS = frozenset({
    "whisper", "voxtral", "parakeet", "transcribe", "speech", "asr",
})


def _filter_fetched_models(models: list[str], req_cap: str) -> list[str]:
    """Filter raw model IDs from /v1/models based on the requested capability (text/image/audio)."""
    if not models:
        return []
    if req_cap == "text":
        return [m for m in models if not any(kw in m.lower() for kw in _TEXT_MODEL_EXCLUDES)]
    include = _INCLUDE_MODEL_KEYWORDS.get(req_cap, _AUDIO_MODEL_KEYWORDS)
    return [m for m in models if any(kw in m.lower() for kw in include)]


# --- Model capability and audio support ---


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
    """Read-modify-write one support map under the config write lock."""
    def _mutate(data: dict[str, Any]) -> None:
        data[map_key] = bool(supported)

    update_config_mapping(config_key, _mutate)


def set_native_audio_support(model_id: Any, endpoint: Any, supported: bool) -> None:
    """Save the audio support status for a model+endpoint pair."""
    model_id = str(model_id).lower()
    endpoint = normalize_endpoint_url(endpoint)
    key = f"{endpoint}@{model_id}"
    _update_support_map("audio_support_map", key, supported)


# --- Resolved model getters (text / STT / grammar / image) ---


def _sanitize_stored_model_value(val: Any) -> str:
    """Drop combobox placeholder strings from persisted model config."""
    from plugin.framework.model_lru import _sanitize_model_combobox_value

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
    """Return the configured STT model."""
    val = _sanitize_stored_model_value(get_config("audio.stt_model"))
    if val:
        return val
    return _stored_or_default("audio.stt_model", "stt_model")


def get_tts_model() -> str:
    """Return the configured TTS model, or default for the current endpoint's provider."""
    return _stored_or_default("audio.tts_model", "tts_model")


def _set_stored_model(config_key: str, lru_key: str, val: Any, update_lru: bool, event_key: str | None = None) -> None:
    """Helper for persisting text/image/TTS model selections and updating LRU."""
    if val is None:
        return
    val_str = _sanitize_stored_model_value(val)
    if not val_str:
        return

    current = str(get_config(config_key) or "").strip()
    if val_str == current:
        return

    save({config_key: val_str}, event_key=event_key)
    if update_lru:
        from plugin.framework.model_lru import update_lru_history

        update_lru_history(val_str, lru_key, get_current_endpoint())


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
    _set_stored_model("text_model", "model_lru", val, update_lru, event_key=event_key)


def set_image_model(val: Any, update_lru: bool = True, *, event_key: str | None = None) -> None:
    """Set image model and notify listeners."""
    _set_stored_model("image_model", "image_model_lru", val, update_lru, event_key=event_key)


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


def _vision_support_key(endpoint: Any, model_id: Any) -> str:
    """The one vision key: chat's endpoint normalization, OpenRouter suffix fold, casefold."""
    raw_ep = endpoint if isinstance(endpoint, str) else ""
    endpoint_str = normalize_endpoint_url(raw_ep, is_openwebui=_resolved_openwebui(raw_ep))
    model_str = str(model_id).strip()
    if get_provider_from_endpoint(endpoint_str) == "openrouter":
        model_str = resolve_openrouter_catalog_id(model_str)
    return f"{endpoint_str}@{model_str.casefold()}"


def _vision_provider_memo(provider: str | None, endpoint: str, model_id: str, api_key_override: str | None) -> tuple[bool, bool | None]:
    """(memo is warm, answer) from the in-process provider memos. No I/O.

    Providers without vision metadata are always warm with no answer.
    """
    if provider in ("openrouter", "together"):
        # Uses the normalized request triple and api_key_override matching catalog fetch.
        built = _v1_models_request(endpoint, api_key_override)
        if built is None:
            return True, None
        key = _catalog_memo_key(endpoint, api_key_override)
        memo = _catalog_memos.get(key)
        if memo is None or (memo.ids is None and memo.vision_declarations is None):
            return False, None
        # A row that omitted input_modalities is not a "no": it stays unknown.
        if not isinstance(memo.vision_declarations, dict):
            return True, None
        return True, _declared_vision_flag(provider, memo.vision_declarations, model_id)
    if provider == "ollama":
        key = _catalog_memo_key(endpoint)
        memo = _catalog_memos.get(key)
        if memo is None:
            return False, None
        cached_show = memo.ollama_show.get(model_id)
        if cached_show is None:
            return False, None
        show_caps = cached_show.get("capabilities") or []
        return True, "vision" in show_caps if isinstance(show_caps, list) else False
    return True, None


def _schedule_vision_lookup(support_key: str, model_id: str, endpoint: str, api_key_override: str | None) -> None:
    """One background has_native_vision per key. A failed job leaves nothing behind."""
    with _vision_in_flight_lock:
        if support_key in _vision_in_flight:
            return
        _vision_in_flight.add(support_key)

    def job() -> None:
        try:
            has_native_vision(model_id, endpoint, api_key_override=api_key_override)
        finally:
            with _vision_in_flight_lock:
                _vision_in_flight.discard(support_key)

    run_in_background(job, name="vision-lookup")


def has_native_vision(model_id: Any, endpoint: Any, *, api_key_override: str | None = None) -> bool | None:
    """Check if the model supports native multimodal vision input.

    Returns:
        True: Model has native vision support.
        False: Model does not have native vision support.
        None: Capability is unknown.

    Callers attach image input only when the result ``is True`` and hide
    ``get_image`` only when the result ``is False`` (never rely on truthiness,
    since None is falsy).

    Priority order:
    1. Empty model_id -> False.
    2. Persistent user config (``vision_support_map`` under ``_vision_support_key``),
       including explicit False.
    3. Static default models list (``ModelCapability.VISION``).
    4. Warm in-process provider memo (OpenRouter/Together ``input_modalities``,
       Ollama ``/api/show`` capabilities).
    5. Cold memo (OpenRouter, Together, Ollama): on the UI thread
       (``main.is_current()``) never fetch; schedule one background lookup and
       fall through. Off the UI thread, fetch inline and re-read the memo.
       Answers live only in those process memos; a failed fetch stores
       nothing, so the next check tries again.
    6. Not settled: False when the static catalog has a row for this model
       without VISION (caps != NONE), else None. No name heuristics.
    """
    if not model_id:
        return False
    model_id_str = str(model_id).strip()

    # 2. Persistent User Config Cache check
    support_key = _vision_support_key(endpoint, model_id_str)
    cache = get_config("vision_support_map")
    if isinstance(cache, dict) and support_key in cache:
        return as_bool(cache[support_key])

    # 3. Static Default Models check
    raw_ep = endpoint if isinstance(endpoint, str) else ""
    endpoint_str = normalize_endpoint_url(raw_ep)
    caps = get_model_capability(model_id_str, endpoint_str)
    log.debug("has_native_vision: model=%r endpoint_str=%r caps=%r catalog_vision=%s", model_id_str, endpoint_str, caps, bool(caps & ModelCapability.VISION))
    if caps & ModelCapability.VISION:
        return True

    provider = get_provider_from_endpoint(endpoint_str)

    # 4. Warm in-process memo. 5. Cold: the UI thread schedules, a worker fetches.
    warm, answer = _vision_provider_memo(provider, raw_ep, model_id_str, api_key_override)
    if not warm:
        if main.is_current():
            _schedule_vision_lookup(support_key, model_id_str, raw_ep, api_key_override)
        else:
            assert not main.is_current()
            if provider == "ollama":
                query_ollama_show(raw_ep, model_id_str)
            else:
                fetch_available_models(raw_ep, api_key_override=api_key_override)
            answer = _vision_provider_memo(provider, raw_ep, model_id_str, api_key_override)[1]
    if answer is not None:
        return answer

    # 6. A catalog row without VISION is a no; anything else is unknown.
    if caps != ModelCapability.NONE:
        return False
    return None


def set_native_vision_support(model_id: Any, endpoint: Any, supported: bool) -> None:
    """Save the vision support status for a model+endpoint pair to config."""
    key = _vision_support_key(endpoint, model_id)
    _update_support_map("vision_support_map", key, supported)


def _num_ctx_from_ollama(value: Any, *, last: bool) -> int | None:
    if isinstance(value, dict):
        if last:
            return None
        for key, val in value.items():
            if str(key).strip().lower() == "num_ctx":
                return _parse_positive_ctx(val)
        return None
    if not isinstance(value, str):
        return None
    found: int | None = None
    for line in value.splitlines():
        matched = _OLLAMA_NUM_CTX_LINE.match(line)
        if not matched:
            continue
        found = _parse_positive_ctx(matched.group(1))
        if not last:
            return found
    return found


def parse_ollama_runtime_num_ctx(show_body: Any) -> int | None:
    """Runtime ``num_ctx`` from Ollama ``/api/show`` parameters or Modelfile.

    Trained ``model_info["*.context_length"]`` is ignored on purpose: issue #570
    crashed with live ``n_ctx=4096`` while ``qwen2.context_length`` was 32768.
    A missing or unparsable ``num_ctx`` returns None (crash copy stays generic).
    """
    if not isinstance(show_body, dict):
        return None
    parsed = _num_ctx_from_ollama(show_body.get("parameters"), last=False)
    if parsed is not None:
        return parsed
    return _num_ctx_from_ollama(show_body.get("modelfile"), last=True)


def query_ollama_show(endpoint: str, model_id: str) -> dict[str, Any] | None:
    """POST ``/api/show`` once per process. Returns capabilities + runtime num_ctx.

    When an API key is saved for that endpoint, the probe sends the same Bearer header as chat.
    """
    if not endpoint or not model_id:
        return None
    endpoint = normalize_endpoint_url(endpoint)
    memo = _get_catalog_memo(endpoint)
    cached = memo.ollama_show.get(model_id)
    if cached is not None:
        return cached

    url = f"{endpoint}/api/show"
    req_body = {"model": model_id}
    try:
        from plugin.framework.client.auth import build_auth_headers, resolve_auth_for_config
        from plugin.framework.client.requests import sync_request

        # Authenticated Ollama proxies require credentials for /api/show as well.
        api_key = str(get_api_key_for_endpoint(endpoint) or "").strip()
        mini = {"endpoint": endpoint, "api_key": api_key}
        headers = build_auth_headers(resolve_auth_for_config(mini))
        headers["Content-Type"] = "application/json"
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
            memo.ollama_show[model_id] = info
            return info
    except Exception as e:
        log.debug("query_ollama_show failed: %s", e)
    return None


def query_ollama_runtime_num_ctx(endpoint: str, model_id: str) -> int | None:
    """Live Ollama ``num_ctx`` from the cached ``/api/show`` body, or None."""
    if not endpoint or not model_id:
        return None
    info = query_ollama_show(endpoint, model_id)
    if not info:
        return None
    num_ctx = info.get("num_ctx")
    if isinstance(num_ctx, int) and num_ctx > 0:
        return num_ctx
    return None


def peek_live_ollama_num_ctx(client: Any) -> int | None:
    """Cached Ollama runtime num_ctx for crash copy / 500 logs. Never raises.

    ``query_ollama_runtime_num_ctx`` POSTs ``/api/show`` (10s) while
    ``llm_request_lane`` is still held on the HTTP 500 path. Format the 500
    from the body we already have. Read num_ctx only when a previous probe
    filled the show cache. Do not start a new HTTP call here.
    """
    try:
        if client._get_provider() != "ollama":
            return None
        model_name = str(client.config.get("model") or "").strip()
        if not model_name:
            return None
        endpoint = str(client._endpoint() or "")
        key = _catalog_memo_key(endpoint)
        memo = _catalog_memos.get(key)
        if memo is None:
            return None
        cached = memo.ollama_show.get(model_name)
        if not isinstance(cached, dict):
            return None
        num_ctx = cached.get("num_ctx")
        if isinstance(num_ctx, int) and num_ctx > 0:
            return num_ctx
        return None
    except Exception:
        log.debug("HTTP 500: live num_ctx lookup failed", exc_info=True)
        return None


def cached_v1_context_tokens(endpoint: str, model_id: str, provider: str | None = None, *, api_key_override: str | None = None) -> int | None:
    """Already-memoized /v1/models window for ``model_id``, or None. Does not HTTP.

    Compact must not fetch: OpenRouter/Together lists are huge and the sidebar
    skips those providers on purpose. Cache miss → caller falls back to catalog.
    """
    if not endpoint or not model_id:
        return None
    # Look up context cache with the same request triple and key override as catalog fetch.
    built = _v1_models_request(endpoint, api_key_override)
    if built is None:
        return None
    memo = _get_catalog_memo(endpoint, api_key_override)
    lengths = memo.context_tokens
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


def resolve_context_window(client: Any, model_id: str | None = None) -> int | None:
    """Ollama: live ``num_ctx`` only. Else cached /v1/models, then catalog. None → skip compact."""
    model_id = str(model_id or (client.config or {}).get("model") or "").strip() or None
    if not model_id:
        return None
    provider = best_effort(lambda: client._get_provider(), "resolve_context_window: provider")
    endpoint = best_effort(lambda: client._endpoint(), "resolve_context_window: endpoint")
    if provider == "ollama":
        # Issue #570: trained context_length is not the runtime window.
        # Missing num_ctx → None; do not fall back to v1 cache or catalog.
        if not endpoint:
            return None
        num_ctx = query_ollama_runtime_num_ctx(endpoint, model_id)
        if isinstance(num_ctx, int) and num_ctx > 0:
            return num_ctx
        return None
    if endpoint:
        # Harvest-only: Settings/sidebar already fetched. Do not GET /v1/models here.
        typed_key = str((client.config or {}).get("api_key") or "").strip()
        live = cached_v1_context_tokens(endpoint, model_id, provider, api_key_override=typed_key if typed_key else None)
        if isinstance(live, int) and live > 0:
            return live
    for row in DEFAULT_MODELS:
        rid = resolve_model_id(row, provider)
        matched = rid == model_id
        if (
            not matched
            and provider == "openrouter"
            and rid
            and openrouter_model_ids_equivalent(rid, model_id)
        ):
            matched = True
        if not matched:
            continue
        cl = row.get("context_length")
        if isinstance(cl, int) and cl > 0:
            return cl
    # Custom OpenAI-compatible endpoints (including writeragent-mock) are
    # provider ``custom``, so the provider-keyed walk misses them. Match any
    # catalog id. Ollama already returned above — never this fallback.
    for row in DEFAULT_MODELS:
        ids = row.get("ids")
        if not isinstance(ids, dict) or model_id not in ids.values():
            continue
        cl = row.get("context_length")
        if isinstance(cl, int) and cl > 0:
            return cl
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
