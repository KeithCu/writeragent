"""
Configuration logic for LocalWriter.
Reads/writes localwriter.json in LibreOffice's user config directory.
"""
import os
import json
import uno
# from plugin.contrib.default_models import DEFAULT_MODELS
DEFAULT_MODELS = {}


CONFIG_FILENAME = "localwriter.json"

# MCP server: mcp_enabled (bool, default False), mcp_port (int, default 8765)

# Max items for all LRU lists (model_lru, prompt_lru, image_model_lru, endpoint_lru).
LRU_MAX_ITEMS = 6

# Endpoint presets: local first, then FOSS-friendly / open-model providers, proprietary last. Base URLs only; api.py adds /v1 (or /api for OpenWebUI).
# Uncomment any FOSS-focused line below once the base URL is verified OpenAI-compatible.
ENDPOINT_PRESETS = [
    ("Local (Ollama)", "http://localhost:11434"),
    ("OpenRouter", "https://openrouter.ai/api"),
    ("Mistral", "https://api.mistral.ai"),
    ("Together AI", "https://api.together.xyz"),
    # ("Hugging Face", "https://api-inference.huggingface.co"),  # verify OpenAI-compatible base URL
    # ("Groq", "https://api.groq.com/openai"),
    # ("Fireworks AI", "https://api.fireworks.ai/inference"),
    # ("Anyscale", "https://api.anyscale.com"),
    # ("Replicate", "https://api.replicate.com/v1"),  # verify base URL / compatibility
    # ("Modal", "https://your-workspace--endpoint.modal.run/v1"),  # per-deployment URL
    # ("RunPod", "https://api.runpod.ai/v2"),  # verify; often per-endpoint
]


def _config_path(ctx):
    """Return the absolute path to localwriter.json."""
    sm = ctx.getServiceManager()
    path_settings = sm.createInstanceWithContext(
        "com.sun.star.util.PathSettings", ctx)
    user_config_path = getattr(path_settings, "UserConfig", "")
    if user_config_path and str(user_config_path).startswith("file://"):
        user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
    return os.path.join(user_config_path, CONFIG_FILENAME)


def user_config_dir(ctx):
    """Return LibreOffice user config directory, or None if unavailable."""
    if ctx is None:
        return None
    try:
        p = _config_path(ctx)
        return os.path.dirname(p) if p else None
    except Exception:
        return None


def get_config(ctx, key, default):
    """Get a config value by key. Returns default if missing or on error."""
    config_file_path = _config_path(ctx)
    if not os.path.exists(config_file_path):
        return default
    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
    except (IOError, json.JSONDecodeError):
        return default
    return config_data.get(key, default)


def get_config_dict(ctx):
    """Return the full config as a dict. Returns {} if missing or on error."""
    config_file_path = _config_path(ctx)
    if not os.path.exists(config_file_path):
        return {}
    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}


def get_current_endpoint(ctx):
    """Return the current endpoint URL from config, normalized (stripped)."""
    return str(get_config(ctx, "endpoint", "")).strip()


def set_config(ctx, key, value):
    """Set a config key to value. Creates file if needed."""
    config_file_path = _config_path(ctx)
    if os.path.exists(config_file_path):
        try:
            with open(config_file_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except (IOError, json.JSONDecodeError):
            config_data = {}
    else:
        config_data = {}
    config_data[key] = value
    try:
        with open(config_file_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
    except IOError as e:
        from plugin.framework.logging import debug_log
        debug_log("Error writing to %s: %s" % (config_file_path, e), context="Config")


def remove_config(ctx, key):
    """Remove a config key. Used e.g. to delete legacy api_key after migration."""
    config_file_path = _config_path(ctx)
    if not os.path.exists(config_file_path):
        return
    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
    except (IOError, json.JSONDecodeError):
        return
    config_data.pop(key, None)
    try:
        with open(config_file_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
    except IOError as e:
        from plugin.framework.logging import debug_log
        debug_log("Error writing to %s: %s" % (config_file_path, e), context="Config")


# Listeners are called when config is changed (e.g. after Settings dialog).
# Sidebar uses weakref in its callback so panels can be GC'd without unregistering.
_config_listeners = []


def add_config_listener(callback):
    """Register a callable(ctx) to be invoked when config changes (e.g. after Settings OK)."""
    _config_listeners.append(callback)


def notify_config_changed(ctx):
    """Call all registered listeners so UI (e.g. sidebar) can refresh from config."""
    for cb in list(_config_listeners):
        try:
            cb(ctx)
        except Exception:
            pass


def as_bool(value):
    """Parse a value as boolean (handles str, int, float)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_provider_from_endpoint(endpoint):
    """Return provider key for DEFAULT_MODELS based on endpoint URL or labels."""
    if not endpoint:
        return None
    url = _normalize_endpoint_url(endpoint).lower()
    if "openrouter.ai" in url:
        return "openrouter"
    if "together.xyz" in url:
        return "together"
    if "localhost:11434" in url or "ollama" in url:
        return "ollama"
    if "api.mistral.ai" in url:
        return "mistral"
    if "api.openai.com" in url:
        return "openai"
    return None


def populate_combobox_with_lru(ctx, ctrl, current_val, lru_key, endpoint, strict=False):
    """Helper to populate a combobox with values from an LRU list in config.
    LRU is scoped to the provided endpoint.
    If LRU is empty, pre-populates with default models for the provider.
    When strict=True, only show models for this endpoint; if current_val is not
    in that list, set selection to first item or empty. Returns the value set."""
    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(ctx, scoped_key, [])
    if not isinstance(lru, list):
        lru = []
    
    # Always try to merge DEFAULT_MODELS into the options so they are always choices
    provider = get_provider_from_endpoint(endpoint)
    if provider and provider in DEFAULT_MODELS:
        model_type = "image" if "image" in lru_key.lower() else "text"
        defaults = DEFAULT_MODELS[provider].get(model_type, [])
        for m in defaults:
            if m["id"] not in lru:
                lru.append(m["id"])

    curr_val_str = str(current_val).strip()
    to_show = list(lru)
    if strict:
        if curr_val_str not in to_show:
            display_val = to_show[0] if to_show else ""
        else:
            display_val = curr_val_str
    else:
        if curr_val_str and curr_val_str not in to_show:
            to_show.insert(0, curr_val_str)
        display_val = curr_val_str if curr_val_str else (to_show[0] if to_show else "")
    
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
        max_items = LRU_MAX_ITEMS
    val_str = str(val).strip()
    if not val_str:
        return

    scoped_key = f"{lru_key}@{endpoint}" if endpoint else lru_key
    lru = get_config(ctx, scoped_key, [])
    if not isinstance(lru, list):
        lru = []

    if val_str in lru:
        lru.remove(val_str)
    lru.insert(0, val_str)
    set_config(ctx, scoped_key, lru[:max_items])


def get_text_model(ctx):
    """Return the text/chat model (stored as text_model, fallback to model)."""
    return str(get_config(ctx, "text_model", "") or get_config(ctx, "model", "")).strip()


def get_endpoint_presets():
    """Return list of (label, url) for endpoint selector, in display order."""
    return list(ENDPOINT_PRESETS)


def _normalize_endpoint_url(url):
    """Strip and rstrip slash for consistent storage."""
    if not url or not isinstance(url, str):
        return ""
    return url.strip().rstrip("/")


def endpoint_from_selector_text(text):
    """Resolve combobox text to endpoint URL. If text is a preset label, return its URL; else return normalized text."""
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    for label, url in ENDPOINT_PRESETS:
        if label == t:
            return _normalize_endpoint_url(url)
    return _normalize_endpoint_url(t)


def endpoint_to_selector_display(current_url):
    """Return string to show in endpoint combobox: preset label if URL matches a preset, else the URL."""
    url = _normalize_endpoint_url(current_url or "")
    if not url:
        return ""
    for label, preset_url in ENDPOINT_PRESETS:
        if _normalize_endpoint_url(preset_url) == url:
            return label
    return url


def populate_endpoint_selector(ctx, ctrl, current_endpoint):
    """Populate endpoint combobox: preset labels first, then endpoint_lru URLs. Combobox text = URL (visible and editable)."""
    if not ctrl:
        return
    current_url = _normalize_endpoint_url(current_endpoint or "")

    preset_labels = [label for label, _ in ENDPOINT_PRESETS]
    lru = get_config(ctx, "endpoint_lru", [])
    if not isinstance(lru, list):
        lru = []

    preset_urls_normalized = {_normalize_endpoint_url(p[1]) for p in ENDPOINT_PRESETS}
    to_show = list(preset_labels)
    for url in lru:
        u = _normalize_endpoint_url(url)
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


def validate_api_config(config):
    """Validate API config dict (from get_api_config). Returns (ok: bool, error_message: str)."""
    endpoint = (config.get("endpoint") or "").strip()
    if not endpoint:
        return (False, "Please set Endpoint in Settings.")
    api_type = (config.get("api_type") or "chat").lower()
    if api_type == "chat":
        model = (config.get("model") or "").strip()
        if not model:
            return (False, "Please set Model in Settings.")
    return (True, "")


def get_image_model(ctx):
    """Return current image model based on provider."""
    image_provider = get_config(ctx, "image_provider", "aihorde")
    if image_provider == "aihorde":
        return str(get_config(ctx, "aihorde_model", "stable_diffusion")).strip()
    return str(get_config(ctx, "image_model", "")).strip()


def set_image_model(ctx, val, update_lru=True):
    """Set image model based on provider and notify listeners."""
    if val is None:
        return
    val_str = str(val).strip()
    if not val_str:
        return

    image_provider = get_config(ctx, "image_provider", "aihorde")
    if image_provider == "aihorde":
        set_config(ctx, "aihorde_model", val_str)
    else:
        set_config(ctx, "image_model", val_str)
        if update_lru:
            update_lru_history(ctx, val_str, "image_model_lru", get_current_endpoint(ctx))
    
    notify_config_changed(ctx)


def _migrate_api_key_to_map_if_needed(ctx):
    """One-time upgrade: move legacy api_key into api_keys_by_endpoint under current endpoint, then delete api_key."""
    data = get_config(ctx, "api_keys_by_endpoint", {})
    if isinstance(data, dict) and data:
        return
    legacy_key = str(get_config(ctx, "api_key", "")).strip()
    current_endpoint = get_current_endpoint(ctx)
    if not legacy_key or not current_endpoint:
        return
    if not isinstance(data, dict):
        data = {}
    normalized = _normalize_endpoint_url(current_endpoint)
    data[normalized] = legacy_key
    set_config(ctx, "api_keys_by_endpoint", data)
    remove_config(ctx, "api_key")


def get_api_key_for_endpoint(ctx, endpoint):
    """Return API key for the given endpoint. Runs migration first; then map lookup with legacy fallback."""
    _migrate_api_key_to_map_if_needed(ctx)
    data = get_config(ctx, "api_keys_by_endpoint", {})
    if not isinstance(data, dict):
        data = {}
    normalized = _normalize_endpoint_url(endpoint or "")
    return data.get(normalized) or str(get_config(ctx, "api_key", ""))


def set_api_key_for_endpoint(ctx, endpoint, key):
    """Store API key for the given endpoint in api_keys_by_endpoint."""
    data = get_config(ctx, "api_keys_by_endpoint", {})
    if not isinstance(data, dict):
        data = {}
    normalized = _normalize_endpoint_url(endpoint or "")
    data[normalized] = str(key)
    set_config(ctx, "api_keys_by_endpoint", data)


def get_api_config(ctx):
    """Build API config dict from ctx for LlmClient. Pass to LlmClient(config, ctx)."""
    endpoint = str(get_config(ctx, "endpoint", "http://127.0.0.1:5000")).rstrip("/")
    is_openwebui = (
        as_bool(get_config(ctx, "is_openwebui", False))
        or "open-webui" in endpoint.lower()
        or "openwebui" in endpoint.lower()
    )
    api_key = get_api_key_for_endpoint(ctx, endpoint)

    is_openrouter = "openrouter.ai" in endpoint.lower()
    return {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": get_text_model(ctx),
        "api_type": str(get_config(ctx, "api_type", "chat")).lower(),
        "is_openwebui": is_openwebui,
        "is_openrouter": is_openrouter,
        "openai_compatibility": as_bool(get_config(ctx, "openai_compatibility", True)),
        "temperature": _safe_float(get_config(ctx, "temperature", 0.5), 0.5),
        "seed": get_config(ctx, "seed", ""),
        "request_timeout": _safe_int(get_config(ctx, "request_timeout", 120), 120),
        "chat_max_tool_rounds": _safe_int(get_config(ctx, "chat_max_tool_rounds", 5), 5),
    }


def populate_image_model_selector(ctx, ctrl, override_endpoint=None):
    """Adaptive population of image model selector (ComboBox) based on provider.
    When image_provider is endpoint, uses override_endpoint if provided else config endpoint;
    uses strict=True so only models for that endpoint are shown. Returns the value set."""
    if not ctrl:
        return ""
    image_provider = get_config(ctx, "image_provider", "aihorde")
    if image_provider == "aihorde":
        current_image_model = get_image_model(ctx)
        from plugin.contrib.aihordeclient import MODELS
        ctrl.removeItems(0, ctrl.getItemCount())
        ctrl.addItems(tuple(MODELS), 0)
        ctrl.setText(current_image_model)
        return current_image_model
    current_image_model = get_image_model(ctx)
    endpoint = override_endpoint if override_endpoint is not None else get_current_endpoint(ctx)
    return populate_combobox_with_lru(ctx, ctrl, current_image_model, "image_model_lru", endpoint, strict=True)


from plugin.framework.service_base import ServiceBase
from plugin.framework.uno_context import get_ctx

class ConfigService(ServiceBase):
    name = "config"

    def initialize(self, ctx):
        pass

    def get(self, key, default=None):
        ctx = get_ctx()
        if key == "mcp_enabled": return as_bool(get_config(ctx, "mcp_enabled", False))
        if key == "enabled": return True
        if key == "port": return int(get_config(ctx, "mcp_port", 8765))
        if key == "host": return "localhost"
        if key == "use_ssl": return False
        if key == "smolagents_enabled": return as_bool(get_config(ctx, "smolagents_enabled", False))
        if key == "fast_model": return str(get_config(ctx, "text_model", ""))
        return get_config(ctx, key, default)

    def set(self, key, value):
        set_config(get_ctx(), key, value)

    def proxy_for(self, name):
        return self

