"""
UI population helpers for LibreOffice dialogs and Settings.
"""
from typing import Any
from plugin.framework.config import (
    get_config,
    set_config,
    get_current_endpoint,
    get_provider_from_endpoint,
    get_image_model,
    ENDPOINT_PRESETS,
)
from plugin.framework.url_utils import normalize_endpoint_url
from plugin.framework.uno_context import get_ctx
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
            if mid == model_id:
                if pid == current_provider:
                    is_known_here = True
                else:
                    is_known_elsewhere = True

    # If it's a known model for others but NOT known for us, it's probably a 'stray'
    return is_known_elsewhere and not is_known_here


def populate_combobox_with_lru(ctx, ctrl, current_val, lru_key, endpoint, *, remote_models: list[str] | None = None, skip_remote_fetch: bool = False):
    """Helper to populate a combobox with values from an LRU list in config.
    LRU is scoped to the provided endpoint.
    Merges relevant default models based on the capability inferred from lru_key.
    Returns the value set.

    remote_models: when set, use as /v1/models IDs for **text** comboboxes only (skip internal fetch).
    skip_remote_fetch: when True, never call fetch_available_models (LRU + provider defaults).
    """
    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(ctx, scoped_key)
    if not isinstance(lru, list):
        lru = []

    provider = get_provider_from_endpoint(endpoint)
    req_cap = "image" if "image" in lru_key.lower() else "audio" if "audio" in lru_key.lower() or "stt" in lru_key.lower() else "text"

    to_show = list(lru)

    # For text models, determine if we should fetch from the API.
    # We do NOT fetch for known massive providers (openrouter, together).
    massive_providers = {"openrouter", "together"}
    fetched_models: list[str] | None = None
    if remote_models is not None:
        if req_cap == "text":
            fetched_models = remote_models
    elif skip_remote_fetch:
        fetched_models = None
    elif endpoint and (not provider or provider not in massive_providers):
        fetched_models = fetch_available_models(endpoint, ctx)

    if fetched_models is not None:
        filtered = _filter_fetched_models(fetched_models, req_cap)
        for mid in filtered:
            if mid not in to_show:
                to_show.append(mid)
    else:
        # Merge defaults into the list if no fetching was done or fetching failed
        if provider:
            for m in DEFAULT_MODELS:
                capability = m.get("capability", ModelCapability.CHAT)
                if not _default_model_row_matches_combo(capability, req_cap):
                    continue
                # Only add models that are marked as default for this capability
                is_default = False
                if req_cap == "text" and m.get("default_text"):
                    is_default = True
                elif req_cap == "image" and m.get("default_image"):
                    is_default = True
                elif req_cap == "audio" and m.get("default_audio"):
                    is_default = True

                if is_default:
                    effective_id = resolve_model_id(m, provider)
                    if effective_id and effective_id not in to_show:
                        to_show.append(effective_id)

    curr_val_str = str(current_val).strip()
    # Filter out models that belong to other providers if we are on a specific provider endpoint
    is_stray = _is_model_id_associated_with_other_provider(curr_val_str, provider)
    
    if curr_val_str and not is_stray and curr_val_str not in to_show:
        to_show.insert(0, curr_val_str)

    # If the list is empty (fetch failed and no defaults), add a helpful placeholder
    if not to_show:
        from plugin.framework.i18n import _
        if provider:
            to_show.append(_("(Enter API Key to load models)"))
        else:
            to_show.append(_("(Connection failed)"))

    display_val = curr_val_str if (curr_val_str and not is_stray) else (to_show[0] if to_show else "")

    if to_show:
        ctrl.removeItems(0, ctrl.getItemCount())
        ctrl.addItems(tuple(to_show), 0)
    if display_val:
        ctrl.setText(display_val)
    elif ctrl.getItemCount() == 0 and hasattr(ctrl, "setText"):
        ctrl.setText("")
    return display_val if display_val else ""

def update_lru_history(ctx, val, lru_key, endpoint, max_items=None):
    """Helper to update an LRU list in config. Scoped to endpoint."""
    if max_items is None:
        from plugin.framework.config import LRU_MAX_ITEMS
        max_items = LRU_MAX_ITEMS
    val_str = str(val).strip()
    if not val_str:
        return

    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(ctx, scoped_key)
    if not isinstance(lru, list):
        lru = []
    lru = list(lru)
    if val_str in lru:
        lru.remove(val_str)
    lru.insert(0, val_str)
    new_lru = lru[:max_items]
    old = get_config(ctx, scoped_key)
    if isinstance(old, list) and old == new_lru:
        return
    set_config(ctx, scoped_key, new_lru)

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
    lru = get_config(ctx, "endpoint_lru")
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
    ctx = get_ctx()
    options = []
    presets = ENDPOINT_PRESETS
    preset_urls = set()
    for label, url in presets:
        url_norm = normalize_endpoint_url(url)
        preset_urls.add(url_norm)
        options.append({"value": url_norm, "label": label})

    lru = get_config(ctx, "endpoint_lru")
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
    ctx = get_ctx()
    endpoint = get_current_endpoint(ctx)
    scoped_key = f"model_lru@{endpoint}" if endpoint else "model_lru"
    lru = get_config(ctx, scoped_key)
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
    ctx = get_ctx()
    endpoint = get_current_endpoint(ctx)
    scoped_key = f"image_model_lru@{endpoint}" if endpoint else "image_model_lru"
    lru = get_config(ctx, scoped_key)
    if not isinstance(lru, list):
        lru = []
    options = [{"value": "", "label": "(none)"}]
    for mid in lru:
        mid_str = str(mid).strip()
        if not mid_str:
            continue
        options.append({"value": mid_str, "label": mid_str})
    return options

def populate_image_model_selector(ctx, ctrl, override_endpoint=None, *, remote_models: list[str] | None = None, skip_remote_fetch: bool = False):
    """Adaptive population of image model selector (ComboBox) based on provider."""
    if not ctrl:
        return ""
    image_provider = get_config(ctx, "image_provider")
    if image_provider == "aihorde":
        current_image_model = get_image_model(ctx)
        from plugin.contrib.aihordeclient import MODELS

        ctrl.removeItems(0, ctrl.getItemCount())
        ctrl.addItems(tuple(MODELS), 0)
        ctrl.setText(current_image_model)
        return current_image_model
    current_image_model = get_image_model(ctx)
    endpoint = override_endpoint if override_endpoint is not None else get_current_endpoint(ctx)
    return populate_combobox_with_lru(ctx, ctrl, current_image_model, "image_model_lru", endpoint, remote_models=remote_models, skip_remote_fetch=skip_remote_fetch)
