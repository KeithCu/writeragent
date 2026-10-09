"""
UI population helpers for LibreOffice dialogs and Settings.
"""
from typing import Any
from plugin.framework.config import (
    LRU_MAX_ITEMS,
    get_config,
    set_config,
    set_configs,
    get_current_endpoint,
    get_api_key_for_endpoint,
)
from plugin.framework.client.auth import provider_requires_api_key, provider_requires_slug_model_id
from plugin.framework.client.provider_detection import get_provider_from_endpoint
from plugin.framework.client.model_fetcher import (
    get_image_model,
    ENDPOINT_PRESETS,
)
from plugin.framework.url_utils import normalize_endpoint_url
from plugin.framework.default_models import DEFAULT_MODELS, resolve_model_id, together_speech_ids
from plugin.framework.constants import ModelCapability
from plugin.framework.client.model_fetcher import fetch_available_models, _filter_fetched_models

def _default_model_row_matches_combo(capability: Any, req_cap: str) -> bool:
    """True if a DEFAULT_MODELS row applies to this combobox (text/image/audio).

    Catalog entries use :class:`ModelCapability` bitmasks; legacy configs may use
    comma-separated labels (e.g. ``text``).
    """
    if isinstance(capability, str):
        parts = [p.strip() for p in capability.split(",") if p.strip()]
        return req_cap in parts
    try:
        cap = capability if isinstance(capability, ModelCapability) else ModelCapability(int(capability))
    except (TypeError, ValueError):
        return False
    if req_cap == "text":
        return bool(cap & ModelCapability.CHAT)
    if req_cap == "image":
        return bool(cap & ModelCapability.IMAGE)
    if req_cap in ("audio", "tts"):
        return bool(cap & ModelCapability.AUDIO)
    return False


def _has_openrouter_casefold(models: list[str], model_id: str) -> bool:
    """True when ``models`` already has ``model_id`` ignoring ASCII case.

    OpenRouter's speech list returns ``hexgrad/kokoro-82m``; the catalog id is
    ``hexgrad/Kokoro-82M``. Suffix variants (``:nitro``) are not the same id.
    """
    folded = model_id.casefold()
    return any(mid.casefold() == folded for mid in models)


def _prefer_openrouter_api_id(to_show: list[str], api_id: str) -> None:
    """Insert ``api_id``, collapsing case-only duplicates onto that spelling."""
    folded = api_id.casefold()
    replaced = False
    kept: list[str] = []
    for mid in to_show:
        if mid.casefold() == folded:
            if not replaced:
                kept.append(api_id)
                replaced = True
            continue
        kept.append(mid)
    if not replaced:
        kept.append(api_id)
    to_show[:] = kept


def _catalog_mid_matches(model_id: str, catalog_mid: str, provider: str | None) -> bool:
    if catalog_mid == model_id:
        return True
    if provider == "openrouter":
        from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent

        return openrouter_model_ids_equivalent(catalog_mid, model_id)
    return False


def _is_model_id_associated_with_other_provider(model_id: str, current_provider: str | None) -> bool:
    """True if model_id is a known default for SOME provider, but NOT the current_provider.

    This helps filter out 'sticky' models from a previous endpoint (e.g. Gemini appearing
    in a Z.ai dropdown) when a remote fetch fails.
    """
    if not model_id or not current_provider:
        return False

    from plugin.framework.default_models import DEFAULT_MODELS

    # Check if this model ID is mapped to ANY provider in our catalog
    is_known_elsewhere = False
    is_known_here = False

    for m in DEFAULT_MODELS:
        ids = m.get("ids", {})
        for pid, mid in ids.items():
            if not _catalog_mid_matches(model_id, str(mid), pid if pid == "openrouter" else None):
                continue
            if pid == current_provider:
                is_known_here = True
            else:
                is_known_elsewhere = True

    # If it's a known model for others but NOT known for us, it's probably a 'stray'
    return is_known_elsewhere and not is_known_here


def _is_incompatible_model_for_provider(model_id: str, provider: str | None) -> bool:
    """True when model_id should not appear on this provider's combobox."""
    if not model_id or not provider:
        return False
    if _is_model_id_associated_with_other_provider(model_id, provider):
        return True
    if provider_requires_slug_model_id(provider) and "/" not in model_id:
        return True
    return False


def _filter_models_for_provider(models: list[str], provider: str | None) -> list[str]:
    return [mid for mid in models if not _is_incompatible_model_for_provider(mid, provider)]


def _effective_api_key(ctx: Any, endpoint: str, api_key_override: str | None) -> str:
    if api_key_override is not None:
        return str(api_key_override).strip()
    return str(get_api_key_for_endpoint(endpoint) or "").strip()


_MODEL_COMBO_PLACEHOLDER_MSGIDS = (
    "(Enter API Key to load models)",
    "(Connection failed)",
    "(No image models on this endpoint)",
    "(Default for current endpoint)",
)


def _is_model_combobox_placeholder(val: str) -> bool:
    text = str(val or "").strip()
    if not text:
        return False
    from plugin.framework.i18n import _

    for msgid in _MODEL_COMBO_PLACEHOLDER_MSGIDS:
        if text == msgid or text == _(msgid):
            return True
    return False


def _sanitize_model_combobox_value(val: str) -> str:
    """Drop Settings combobox placeholder strings; they are not real model ids."""
    cleaned = str(val or "").strip()
    return "" if _is_model_combobox_placeholder(cleaned) else cleaned


def _resolve_display_model_for_combobox(
    curr_val_str: str,
    is_incompatible: bool,
    to_show: list[str],
    provider: str | None,
    req_cap: str,
) -> str:
    """Pick combobox display value: keep valid current, else curated default, else first item."""
    if curr_val_str and not is_incompatible:
        return curr_val_str
    if not to_show:
        return ""
    first = to_show[0]
    if _is_model_combobox_placeholder(first):
        return first

    preferred = ""
    if provider and req_cap in ("text", "audio", "tts"):
        from plugin.framework.default_models import get_provider_defaults

        if req_cap == "text":
            key = "text_model"
        elif req_cap == "tts":
            key = "tts_model"
        else:
            key = "stt_model"
        preferred = str(get_provider_defaults(provider).get(key, "") or "").strip()

    if preferred:
        for mid in to_show:
            if provider == "openrouter":
                from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent

                case_same = mid.casefold() == preferred.casefold()
                if openrouter_model_ids_equivalent(mid, preferred) or case_same:
                    # Speech-list spelling wins when the catalog id is not listed.
                    if case_same and preferred not in to_show:
                        return mid
                    return preferred if preferred in to_show else mid
            elif mid == preferred:
                return mid

    return first


# Free-text LRU lists (prompts, image base sizes)—not model-id comboboxes.
_PLAIN_LRU_KEYS: frozenset[str] = frozenset({"prompt_lru", "image_base_size_lru"})


def _populate_plain_combobox_with_lru(ctx: Any, ctrl: Any, current_val: Any, lru_key: str, endpoint: str) -> str:
    """Populate combobox from LRU only; no model fetch or text_model fallback."""
    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(scoped_key)
    if not isinstance(lru, list):
        lru = []
    curr_val_str = str(current_val or "").strip()
    to_show = [str(m).strip() for m in lru if str(m).strip()]
    if curr_val_str and curr_val_str not in to_show:
        to_show.insert(0, curr_val_str)
    if to_show:
        ctrl.removeItems(0, ctrl.getItemCount())
        ctrl.addItems(tuple(to_show), 0)
    if curr_val_str:
        ctrl.setText(curr_val_str)
    elif ctrl.getItemCount() == 0 and hasattr(ctrl, "setText"):
        ctrl.setText("")
    return curr_val_str


def _merge_provider_default_models(to_show: list[str], provider: str, req_cap: str) -> None:
    """Append curated default model ids for this provider/capability."""
    for m in DEFAULT_MODELS:
        capability = m.get("capability", ModelCapability.CHAT)
        if not _default_model_row_matches_combo(capability, req_cap):
            continue
        effective_id = resolve_model_id(m, provider)
        if not effective_id:
            continue
        is_default = False
        if req_cap == "text" and (m.get("default_text") or effective_id == "openrouter/free"):
            is_default = True
        elif req_cap == "image" and m.get("default_image"):
            is_default = True
        elif req_cap == "audio" and (m.get("default_audio") or m.get("stt")):
            # stt marks non-default speech-to-text rows (Together Whisper / Nemotron).
            # AUDIO|CHAT models such as Gemini are not STT entries.
            is_default = True
        elif req_cap == "tts" and (m.get("default_tts") or m.get("tts")):
            is_default = True
        if not is_default:
            continue
        # Keep the API id when the speech list already supplied a case variant.
        if provider == "openrouter" and _has_openrouter_casefold(to_show, effective_id):
            continue
        if effective_id not in to_show:
            to_show.append(effective_id)


def populate_combobox_with_lru(
    ctx: Any,
    ctrl: Any,
    current_val: Any,
    lru_key: str,
    endpoint: str,
    *,
    remote_models: list[str] | None = None,
    skip_remote_fetch: bool = False,
    api_key_override: str | None = None,
) -> str:
    """Helper to populate a combobox with values from an LRU list in config.
    LRU is scoped to the provided endpoint.
    Merges relevant default models based on the capability inferred from lru_key.
    Returns the value set.

    remote_models: when set, use as /v1/models IDs (skip internal fetch).
    skip_remote_fetch: when True, never call fetch_available_models (LRU + provider defaults).
    api_key_override: live Settings field value; wins over saved config for auth gating.
    """
    if lru_key in _PLAIN_LRU_KEYS:
        return _populate_plain_combobox_with_lru(ctx, ctrl, current_val, lru_key, endpoint)

    provider = get_provider_from_endpoint(endpoint)
    req_cap = (
        "tts"
        if "tts" in lru_key.lower()
        else "image"
        if "image" in lru_key.lower()
        else "audio"
        if "audio" in lru_key.lower() or "stt" in lru_key.lower()
        else "text"
    )
    effective_key = _effective_api_key(ctx, endpoint, api_key_override)
    auth_blocked = bool(provider and provider_requires_api_key(provider) and not effective_key and remote_models is None)

    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(scoped_key)
    if not isinstance(lru, list):
        lru = []

    fetch_succeeded = False
    to_show: list[str] = []
    # get_config LRU values are JSON-shaped; normalize to str ids for the filter.
    lru_clean = [str(m) for m in lru if not _is_model_combobox_placeholder(str(m))]
    if not auth_blocked:
        to_show = _filter_models_for_provider(lru_clean, provider)

        # We do NOT inline-fetch for known massive providers (openrouter, together).
        massive_providers = {"openrouter", "together"}
        fetched_models: list[str] | None = None
        if remote_models is not None:
            # Together GET /v1/models has no speech type. Keep catalog TTS/STT
            # ids and any remote id in those families; drop the chat catalog.
            # OpenRouter callers pass modality-filtered ids (output_modalities=
            # speech or transcription). output_modalities=audio is not TTS.
            if provider == "together" and req_cap in ("audio", "tts"):
                fetch_succeeded = True
                kind = "tts" if req_cap == "tts" else "stt"
                fetched_models = together_speech_ids(kind, remote_models)
            else:
                fetch_succeeded = True
                fetched_models = remote_models
        elif skip_remote_fetch:
            fetched_models = None
        elif endpoint and (not provider or provider not in massive_providers):
            fetched_models = fetch_available_models(endpoint, api_key_override=api_key_override)
            fetch_succeeded = fetched_models is not None

        if fetched_models is not None:
            # Image remote_models from fetch_available_image_models are already metadata-curated
            # (OpenRouter architecture / Together type=image). Re-running slug keywords strips
            # ids like google/gemini-2.5-flash-image that lack flux/sdxl/imagen substrings.
            # OpenRouter speech/transcription ids are already filtered by the
            # Models API. Slug keywords drop rows such as microsoft/mai-voice-2.
            modality_list = provider == "openrouter" and req_cap in ("tts", "audio")
            # Together speech ids are already the serverless catalog (+ prefixes).
            together_audio = provider == "together" and req_cap in ("tts", "audio")
            if remote_models is not None and (req_cap == "image" or modality_list or together_audio):
                filtered = list(fetched_models)
            else:
                filtered = _filter_fetched_models(fetched_models, req_cap)
            for mid in _filter_models_for_provider(filtered, provider):
                if modality_list:
                    _prefer_openrouter_api_id(to_show, mid)
                elif mid not in to_show:
                    to_show.append(mid)

        if provider:
            _merge_provider_default_models(to_show, provider, req_cap)

        if provider == "openrouter" and req_cap == "text" and "openrouter/free" in to_show:
            to_show.remove("openrouter/free")
            to_show.insert(0, "openrouter/free")

    curr_val_str = _sanitize_model_combobox_value(current_val)
    if not auth_blocked and not curr_val_str and req_cap == "text":
        # After an endpoint switch, use this endpoint's last-used model (the
        # LRU is scoped per endpoint, newest first) before the provider default.
        lru_for_provider = _filter_models_for_provider(lru_clean, provider)
        if lru_for_provider:
            curr_val_str = lru_for_provider[0]

        if not curr_val_str and provider:
            from plugin.framework.default_models import get_provider_defaults

            curr_val_str = str(get_provider_defaults(provider).get("text_model", "") or "").strip()
        if not curr_val_str:
            from plugin.framework.client.model_fetcher import get_text_model

            curr_val_str = _sanitize_model_combobox_value(str(get_text_model() or ""))
    elif not auth_blocked and not curr_val_str and req_cap == "audio":
        if provider:
            from plugin.framework.default_models import get_provider_defaults

            curr_val_str = str(get_provider_defaults(provider).get("stt_model", "") or "").strip()
        if not curr_val_str:
            from plugin.framework.client.model_fetcher import get_stt_model

            curr_val_str = _sanitize_model_combobox_value(str(get_stt_model() or ""))
    elif not auth_blocked and not curr_val_str and req_cap == "tts":
        if provider:
            from plugin.framework.default_models import get_provider_defaults

            curr_val_str = str(get_provider_defaults(provider).get("tts_model", "") or "").strip()
        if not curr_val_str:
            from plugin.framework.client.model_fetcher import get_tts_model

            curr_val_str = _sanitize_model_combobox_value(str(get_tts_model() or ""))

    is_incompatible = _is_incompatible_model_for_provider(curr_val_str, provider)
    if auth_blocked:
        curr_val_str = ""
        is_incompatible = True

    if curr_val_str and not is_incompatible:
        if provider == "openrouter" and req_cap in ("tts", "audio"):
            folded = curr_val_str.casefold()
            listed = next((mid for mid in to_show if mid.casefold() == folded), None)
            if listed is None:
                to_show.insert(0, curr_val_str)
            else:
                # Saved catalog casing (hexgrad/Kokoro-82M) vs the speech-list id.
                curr_val_str = listed
        elif curr_val_str not in to_show:
            to_show.insert(0, curr_val_str)

    to_show = [m for m in _filter_models_for_provider(to_show, provider) if not _is_model_combobox_placeholder(m)]

    # If the list is empty (fetch failed and no defaults), add a helpful placeholder
    if not to_show:
        from plugin.framework.i18n import _
        # prompt_lru / image_base_size_lru use endpoint="" — no /v1/models fetch attempted.
        if endpoint:
            if auth_blocked or (provider and provider_requires_api_key(provider) and not fetch_succeeded):
                to_show.append(_("(Enter API Key to load models)"))
            elif req_cap == "image" and fetch_succeeded:
                to_show.append(_("(No image models on this endpoint)"))
            else:
                to_show.append(_("(Connection failed)"))

    display_val = _resolve_display_model_for_combobox(curr_val_str, is_incompatible, to_show, provider, req_cap)

    if to_show:
        ctrl.removeItems(0, ctrl.getItemCount())
        ctrl.addItems(tuple(to_show), 0)
    if display_val:
        ctrl.setText(display_val)
    elif ctrl.getItemCount() == 0 and hasattr(ctrl, "setText"):
        ctrl.setText("")
    return display_val if display_val else ""

def lru_config_key(lru_key: str, endpoint: str) -> str:
    """Config key for an LRU list. Endpoint-scoped lists are ``name@url``."""
    return f"{lru_key}@{endpoint}" if endpoint else lru_key


def next_lru_list(current: Any, val: Any, max_items: int) -> list[str] | None:
    """LRU list after prepending *val*, or None when that would not be a write.

    Blank values are not entries. A value that is already the first item is
    not a write. *current* may be a non-list (a missing key read as something
    else); that still becomes a one-item list, matching the old
    ``update_lru_history`` compare.
    """
    val_str = str(val).strip()
    if not val_str:
        return None
    # LRU entries are model id strings; get_config is JSON-shaped so normalize explicitly.
    lru: list[str] = [str(item) for item in current] if isinstance(current, list) else []
    # Already at the head: a set_config here would rewrite the file and emit
    # config:changed for a list the UI already shows first.
    if lru and lru[0] == val_str:
        return None
    if val_str in lru:
        lru.remove(val_str)
    lru.insert(0, val_str)
    new_lru = lru[:max_items]
    if isinstance(current, list) and current == new_lru:
        return None
    return new_lru


def update_lru_history(val: Any, lru_key: str, endpoint: str, max_items: int | None = None) -> None:
    """Prepend *val* to an endpoint-scoped LRU list in writeragent.json.

    No write when the value is blank or already the first item. Settings OK
    and the sidebar model sync do not call this once per key: they merge
    ``next_lru_list`` into the same ``set_configs`` as the field change.
    """
    if max_items is None:
        max_items = LRU_MAX_ITEMS
    scoped_key = lru_config_key(lru_key, endpoint)
    new_lru = next_lru_list(get_config(scoped_key), val, max_items)
    if new_lru is None:
        return
    set_config(scoped_key, new_lru)


def sync_sidebar_image_model(ctrl: Any, update_lru: bool = True) -> str | None:
    """Persist sidebar image model combobox text to image_model and optionally image_model_lru."""
    if not ctrl or not hasattr(ctrl, "getText"):
        return None
    txt = _sanitize_model_combobox_value(str(ctrl.getText() or ""))
    if not txt:
        return None
    from plugin.framework.client.model_fetcher import get_image_model

    patch: dict[str, Any] = {}
    if txt != get_image_model():
        patch["image_model"] = txt

    if update_lru:
        lru_key = lru_config_key("image_model_lru", get_current_endpoint())
        updated = next_lru_list(get_config(lru_key), txt, LRU_MAX_ITEMS)
        if updated is not None:
            patch[lru_key] = updated

    if patch:
        set_configs(patch)
    return txt


def sync_sidebar_text_model(ctx: Any, ctrl: Any) -> str | None:
    """Persist sidebar chat model combobox text to text_model and model_lru.

    Dropdown picks fire ItemListener; paste/typing only change ComboBox text.
    Send and TextListener call this so get_text_model/get_api_config match the UI.

    Both ``text_model`` and ``model_lru@endpoint`` go in one ``set_configs``
    when they differ from disk. Separate writes each rewrite
    ``writeragent.json`` and emit ``config:changed``. A model that already
    matches, or an LRU head that already matches, is left out of that dict,
    so one real change is still one write and one event.
    """
    del ctx  # Listeners pass the panel context; the write uses the config store.
    if not ctrl or not hasattr(ctrl, "getText"):
        return None
    txt = _sanitize_model_combobox_value(str(ctrl.getText() or ""))
    if not txt:
        return None
    from plugin.framework.client.model_fetcher import get_text_model

    patch: dict[str, Any] = {}
    if txt != get_text_model():
        patch["text_model"] = txt
    lru_key = lru_config_key("model_lru", get_current_endpoint())
    updated = next_lru_list(get_config(lru_key), txt, LRU_MAX_ITEMS)
    if updated is not None:
        patch[lru_key] = updated
    if patch:
        set_configs(patch)
    return txt


def endpoint_from_selector_text(text: Any) -> str:
    """Resolve combobox text to endpoint URL. If text is a preset label, return its URL; else return normalized text."""
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    for label, url in ENDPOINT_PRESETS:
        if label == t:
            return normalize_endpoint_url(url)
    return normalize_endpoint_url(t)

def endpoint_to_selector_display(current_url: Any) -> str:
    """Return string to show in endpoint combobox: preset label if URL matches a preset, else the URL."""
    url = normalize_endpoint_url(current_url or "")
    if not url:
        return ""
    for label, preset_url in ENDPOINT_PRESETS:
        if normalize_endpoint_url(preset_url) == url:
            return label
    return url

def populate_endpoint_selector(ctx: Any, ctrl: Any, current_endpoint: Any) -> None:
    """Populate endpoint combobox: preset labels first, then endpoint_lru URLs. Combobox text = URL (visible and editable)."""
    if not ctrl:
        return
    current_url = normalize_endpoint_url(current_endpoint or "")

    preset_labels = [label for label, _unused in ENDPOINT_PRESETS]
    lru = get_config("endpoint_lru")
    if not isinstance(lru, list):
        lru = []

    preset_urls_normalized = {normalize_endpoint_url(p[1]) for p in ENDPOINT_PRESETS}
    to_show = list(preset_labels)
    for url in lru:
        u = normalize_endpoint_url(url)
        if not u or u in preset_urls_normalized:
            continue
        if u not in to_show:
            to_show.append(u)
    # Ensure current URL is in list when it's custom (not a preset)
    if current_url and current_url not in preset_urls_normalized and current_url not in to_show:
        to_show.append(current_url)

    ctrl.removeItems(0, ctrl.getItemCount())
    if to_show:
        ctrl.addItems(tuple(to_show), 0)
    # Always show the actual URL in the text field so user can see and edit it
    if current_url:
        ctrl.setText(current_url)


def populate_image_model_selector(
    ctx: Any,
    ctrl: Any,
    override_endpoint: str | None = None,
    *,
    remote_models: list[str] | None = None,
    skip_remote_fetch: bool = False,
    api_key_override: str | None = None,
) -> str:
    """Adaptive population of image model selector (ComboBox) for endpoint generation."""
    if not ctrl:
        return ""
    current_image_model = get_image_model()
    endpoint = override_endpoint if override_endpoint is not None else get_current_endpoint()
    return populate_combobox_with_lru(
        ctx,
        ctrl,
        current_image_model,
        "image_model_lru",
        endpoint,
        remote_models=remote_models,
        skip_remote_fetch=skip_remote_fetch,
        api_key_override=api_key_override,
    )


PROVIDER_SIGNUP_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/keys",
    "together": "https://api.together.ai/settings/api-keys",
    "huggingface": "https://huggingface.co/settings/tokens",
    "groq": "https://console.groq.com/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "mistral": "https://console.mistral.ai/api-keys/",
    "google": "https://aistudio.google.com/app/apikey",
    "gemini": "https://aistudio.google.com/app/apikey",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "cerebras": "https://cloud.cerebras.ai/",
    "perplexity": "https://www.perplexity.ai/settings/api",
    "xai": "https://console.x.ai/",
    "grok": "https://console.x.ai/",
    "zai": "https://api.z.ai/",
    "nvidia": "https://build.nvidia.com/settings/api-keys",
    "nim": "https://build.nvidia.com/settings/api-keys",
}


def get_signup_url_for_endpoint(endpoint: str) -> str | None:
    """Return signup / API key dashboard URL for a given endpoint if known."""
    if not endpoint or not isinstance(endpoint, str):
        return None
    url_lower = endpoint.lower().strip()
    if "localhost" in url_lower or "127.0.0.1" in url_lower:
        return None
    provider = get_provider_from_endpoint(endpoint)
    if provider and provider in PROVIDER_SIGNUP_URLS:
        return PROVIDER_SIGNUP_URLS[provider]
    for key, signup_url in PROVIDER_SIGNUP_URLS.items():
        if key in url_lower:
            return signup_url
    return None

