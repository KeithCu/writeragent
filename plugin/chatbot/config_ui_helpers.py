"""
UI population helpers for LibreOffice dialogs and Settings.
"""
from typing import Any
from plugin.framework.config import (
    get_config,
    set_config,
    get_current_endpoint,
    get_api_key_for_endpoint,
)
from plugin.framework.client.auth import provider_requires_api_key
from plugin.framework.client.model_fetcher import (
    get_provider_from_endpoint,
    get_image_model,
    ENDPOINT_PRESETS,
)
from plugin.framework.url_utils import normalize_endpoint_url
from plugin.framework.default_models import DEFAULT_MODELS, resolve_model_id
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
    if req_cap == "audio":
        return bool(cap & ModelCapability.AUDIO)
    return False


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


def _model_known_for_provider(model_id: str, provider: str | None) -> bool:
    if not model_id or not provider:
        return False
    for m in DEFAULT_MODELS:
        ids = m.get("ids", {})
        mid = ids.get(provider)
        if mid and _catalog_mid_matches(model_id, str(mid), provider if provider == "openrouter" else None):
            return True
    return False


def _is_incompatible_model_for_provider(model_id: str, provider: str | None) -> bool:
    """True when model_id should not appear on this provider's combobox."""
    if not model_id or not provider:
        return False
    if _is_model_id_associated_with_other_provider(model_id, provider):
        return True
    # Slug catalogs (org/model ids); bare names are typical local Ollama picks on those hosts.
    if provider in {"openrouter", "together"} and "/" not in model_id:
        return True
    return False


def _filter_models_for_provider(models: list[str], provider: str | None) -> list[str]:
    return [mid for mid in models if not _is_incompatible_model_for_provider(mid, provider)]


def _effective_api_key(ctx, endpoint: str, api_key_override: str | None) -> str:
    if api_key_override is not None:
        return str(api_key_override).strip()
    return str(get_api_key_for_endpoint(endpoint) or "").strip()


_MODEL_COMBO_PLACEHOLDER_MSGIDS = (
    "(Enter API Key to load models)",
    "(Connection failed)",
    "(No image models on this endpoint)",
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
    if provider and req_cap in ("text", "audio"):
        from plugin.framework.default_models import get_provider_defaults

        key = "text_model" if req_cap == "text" else "stt_model"
        preferred = str(get_provider_defaults(provider).get(key, "") or "").strip()

    if preferred:
        for mid in to_show:
            if provider == "openrouter":
                from plugin.framework.openrouter_model_id import openrouter_model_ids_equivalent

                if openrouter_model_ids_equivalent(mid, preferred):
                    return preferred if preferred in to_show else mid
            elif mid == preferred:
                return mid

    return first


# Free-text LRU lists (prompts, image base sizes)—not model-id comboboxes.
_PLAIN_LRU_KEYS: frozenset[str] = frozenset({"prompt_lru", "image_base_size_lru"})


def _populate_plain_combobox_with_lru(ctx, ctrl, current_val, lru_key, endpoint) -> str:
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
        is_default = False
        if req_cap == "text" and m.get("default_text"):
            is_default = True
        elif req_cap == "image" and m.get("default_image"):
            is_default = True
        elif req_cap == "audio" and m.get("default_audio"):
            is_default = True
        if not is_default:
            continue
        effective_id = resolve_model_id(m, provider)
        if effective_id and effective_id not in to_show:
            to_show.append(effective_id)


def populate_combobox_with_lru(
    ctx,
    ctrl,
    current_val,
    lru_key,
    endpoint,
    *,
    remote_models: list[str] | None = None,
    skip_remote_fetch: bool = False,
    api_key_override: str | None = None,
):
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
    req_cap = "image" if "image" in lru_key.lower() else "audio" if "audio" in lru_key.lower() or "stt" in lru_key.lower() else "text"
    effective_key = _effective_api_key(ctx, endpoint, api_key_override)
    auth_blocked = bool(provider and provider_requires_api_key(provider) and not effective_key and remote_models is None)

    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(scoped_key)
    if not isinstance(lru, list):
        lru = []

    fetch_succeeded = False
    to_show: list[str] = []
    if not auth_blocked:
        lru_clean = [m for m in lru if not _is_model_combobox_placeholder(str(m))]
        to_show = _filter_models_for_provider(lru_clean, provider)

        # We do NOT inline-fetch for known massive providers (openrouter, together).
        massive_providers = {"openrouter", "together"}
        fetched_models: list[str] | None = None
        if remote_models is not None:
            # OpenRouter/Together /v1/models has no audio modality; curated STT list only.
            if not (req_cap == "audio" and provider in massive_providers):
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
            if remote_models is not None and req_cap == "image":
                filtered = list(fetched_models)
            else:
                filtered = _filter_fetched_models(fetched_models, req_cap)
            for mid in _filter_models_for_provider(filtered, provider):
                if mid not in to_show:
                    to_show.append(mid)

        if provider:
            _merge_provider_default_models(to_show, provider, req_cap)

    curr_val_str = _sanitize_model_combobox_value(current_val)
    if not auth_blocked and not curr_val_str and req_cap == "text":
        if provider:
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

    is_incompatible = _is_incompatible_model_for_provider(curr_val_str, provider)
    if auth_blocked:
        curr_val_str = ""
        is_incompatible = True

    if curr_val_str and not is_incompatible and curr_val_str not in to_show:
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

def update_lru_history(val, lru_key, endpoint, max_items=None):
    """Helper to update an LRU list in config. Scoped to endpoint."""
    if max_items is None:
        from plugin.framework.config import LRU_MAX_ITEMS
        max_items = LRU_MAX_ITEMS
    val_str = str(val).strip()
    if not val_str:
        return

    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(scoped_key)
    if not isinstance(lru, list):
        lru = []
    lru = list(lru)
    if val_str in lru:
        lru.remove(val_str)
    lru.insert(0, val_str)
    new_lru = lru[:max_items]
    old = get_config(scoped_key)
    if isinstance(old, list) and old == new_lru:
        return
    set_config(scoped_key, new_lru)


def sync_sidebar_text_model(ctx, ctrl) -> str | None:
    """Persist sidebar chat model combobox text to text_model and model_lru.

    Dropdown picks fire ItemListener; paste/typing only change ComboBox text.
    Send and TextListener call this so get_text_model/get_api_config match the UI.
    """
    if not ctrl or not hasattr(ctrl, "getText"):
        return None
    txt = _sanitize_model_combobox_value(str(ctrl.getText() or ""))
    if not txt:
        return None
    from plugin.framework.client.model_fetcher import get_text_model, set_text_model

    if txt != get_text_model():
        set_text_model(txt, update_lru=False)
    update_lru_history(txt, "model_lru", get_current_endpoint())
    return txt


def endpoint_from_selector_text(text):
    """Resolve combobox text to endpoint URL. If text is a preset label, return its URL; else return normalized text."""
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    for label, url in ENDPOINT_PRESETS:
        if label == t:
            return normalize_endpoint_url(url)
    return normalize_endpoint_url(t)

def endpoint_to_selector_display(current_url):
    """Return string to show in endpoint combobox: preset label if URL matches a preset, else the URL."""
    url = normalize_endpoint_url(current_url or "")
    if not url:
        return ""
    for label, preset_url in ENDPOINT_PRESETS:
        if normalize_endpoint_url(preset_url) == url:
            return label
    return url

def populate_endpoint_selector(ctx, ctrl, current_endpoint):
    """Populate endpoint combobox: preset labels first, then endpoint_lru URLs. Combobox text = URL (visible and editable)."""
    if not ctrl:
        return
    current_url = normalize_endpoint_url(current_endpoint or "")

    preset_labels = [label for label, _ in ENDPOINT_PRESETS]
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

def get_endpoint_options(services):
    """Options provider for AI endpoint combobox in Tools → Options."""
    options = []
    presets = ENDPOINT_PRESETS
    preset_urls = set()
    for label, url in presets:
        url_norm = normalize_endpoint_url(url)
        preset_urls.add(url_norm)
        options.append({"value": url_norm, "label": label})

    lru = get_config("endpoint_lru")
    if not isinstance(lru, list):
        lru = []
    for url in lru:
        u = normalize_endpoint_url(url)
        if not u or u in preset_urls:
            continue
        options.append({"value": u, "label": u})
    return options

def get_text_model_options(services):
    """Options provider for the simple text model combobox in Tools → Options."""
    endpoint = get_current_endpoint()
    scoped_key = f"model_lru@{endpoint}" if endpoint else "model_lru"
    lru = get_config(scoped_key)
    if not isinstance(lru, list):
        lru = []
    options = [{"value": "", "label": "(none)"}]
    for mid in lru:
        mid_str = str(mid).strip()
        if not mid_str:
            continue
        options.append({"value": mid_str, "label": mid_str})
    return options

def get_image_model_options(services):
    """Options provider for the simple image model combobox in Tools → Options."""
    endpoint = get_current_endpoint()
    scoped_key = f"image_model_lru@{endpoint}" if endpoint else "image_model_lru"
    lru = get_config(scoped_key)
    if not isinstance(lru, list):
        lru = []
    options = [{"value": "", "label": "(none)"}]
    for mid in lru:
        mid_str = str(mid).strip()
        if not mid_str:
            continue
        options.append({"value": mid_str, "label": mid_str})
    return options

def populate_image_model_selector(
    ctx,
    ctrl,
    override_endpoint=None,
    *,
    remote_models: list[str] | None = None,
    skip_remote_fetch: bool = False,
    api_key_override: str | None = None,
):
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
