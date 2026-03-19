# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Configuration logic for WriterAgent.
Reads/writes writeragent.json in LibreOffice's user config directory.
"""
import os
import json
import logging
try:
    import uno
    import unohelper
except ImportError:
    uno = None
    unohelper = None
from plugin.framework.service_base import ServiceBase
from plugin.framework.uno_context import get_ctx
from plugin.framework.default_models import DEFAULT_MODELS, resolve_model_id

log = logging.getLogger(__name__)


CONFIG_FILENAME = "writeragent.json"

# MCP server: mcp_enabled (bool, default False), mcp_port (int, default 8765)

# Max items for all LRU lists (model_lru, prompt_lru, image_model_lru, endpoint_lru).
LRU_MAX_ITEMS = 10

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

# Simple AI settings fields that the Tools → Options \"AI\" page should map
# directly to top-level config keys (endpoint, model, etc.).
AI_SIMPLE_FIELDS = {
    "endpoint",
    "text_model",
    "image_model",
    "stt_model",
    "temperature",
    "chat_max_tokens",
    "chat_context_length",
    "request_timeout",
    "additional_instructions",
    "aihorde_api_key",
    "image_provider",
    "nsfw",
    "censor_nsfw",
    "max_wait",
}


def _config_path(ctx):
    """Return the absolute path to writeragent.json."""
    if ctx is None:
        return None
    try:
        sm = ctx.getServiceManager()
        path_settings = sm.createInstanceWithContext(
            "com.sun.star.util.PathSettings", ctx)
        user_config_path = getattr(path_settings, "UserConfig", "")
        if user_config_path and str(user_config_path).startswith("file://"):
            user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
        return os.path.join(user_config_path, CONFIG_FILENAME)
    except Exception as e:
        log.debug("_config_path exception: %s", e)
        return None


def user_config_dir(ctx):
    """Return LibreOffice user config directory, or None if unavailable."""
    if ctx is None:
        return None
    try:
        p = _config_path(ctx)
        return os.path.dirname(p) if p else None
    except Exception as e:
        log.debug("user_config_dir exception: %s", e)
        return None


def _get_schema_default(key):
    """Return default for key from MODULES (module.yaml schema). Supports flat and dotted keys."""
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return None
    # Dotted key (e.g. agent_backend.backend_id)
    if "." in key:
        mod_name, field_name = key.split(".", 1)
        for m in MODULES:
            if m.get("name") == mod_name:
                for fname, schema in m.get("config", {}).items():
                    if fname == field_name and "default" in schema:
                        return schema["default"]
        return None
    # Flat key: find first module that has this config field
    for m in MODULES:
        for fname, schema in m.get("config", {}).items():
            if fname == key and "default" in schema:
                return schema["default"]
    return None


def _dotted_fallback_keys(key):
    """Yield dotted key variants for key using MODULES (e.g. extend_selection_max_tokens -> chatbot.extend_selection_max_tokens)."""
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return
    if "." in key:
        return
    for m in MODULES:
        mod_name = m.get("name", "")
        if not mod_name:
            continue
        for fname in m.get("config", {}):
            if fname == key:
                yield f"{mod_name}.{fname}"
                break


# Central fallback for keys not in any module.yaml. Single source of defaults in code.
_CONFIG_DEFAULTS = {
    "log_level": "DEBUG",
    "endpoint": "http://127.0.0.1:5000",
    "text_model": "",
    "model": "",
    "temperature": -1,
    "additional_instructions": "",
    "chat_context_length": 8000,
    "chat_max_tokens": 16384,
    "request_timeout": 120,
    "chat_max_tool_rounds": 5,
    "stt_model": "",
    "api_keys_by_endpoint": {},
    "aihorde_api_key": "",
    "image_base_size": 512,
    "image_default_aspect": "Square",
    "image_cfg_scale": 7.5,
    "image_steps": -1,
    "image_nsfw": False,
    "image_censor_nsfw": True,
    "image_max_wait": 5,
    "image_auto_gallery": True,
    "image_insert_frame": False,
    "image_translate_prompt": True,
    "image_translate_from": "",
    "image_model": "",
    "image_provider": "aihorde",
    "aihorde_model": "stable_diffusion",
    "seed": "",
    "chatbot.show_search_thinking": False,
    "enable_agent_log": False,
    "web_cache_max_mb": 50,
    "web_cache_validity_days": 7,
    "is_openwebui": False,
    "extend_selection_system_prompt": "",
    "edit_selection_system_prompt": "",
    "audio_support_map": {},
    "chat_direct_image": False,
    "calc_prompt_max_tokens": 70,
}


def _resolve_default(key):
    """Resolve default for key: schema first, then central dict. Safe fallbacks for None."""
    if key == "log_level":
        tests_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests")
        return "DEBUG" if os.path.isdir(tests_dir) else "WARN"

    val = _get_schema_default(key)
    if val is not None:
        return val
    val = _CONFIG_DEFAULTS.get(key)
    if val is not None:
        return val
    if "@" in key or key.endswith("_lru"):
        return []
    if "by_endpoint" in key or "_map" in key:
        return {}
    return ""


def get_config(ctx, key):
    """Get a config value by key. JSON overrides; when key is missing, use schema default then central fallback."""
    config_file_path = _config_path(ctx)
    config_data = {}
    if config_file_path and os.path.exists(config_file_path):
        try:
            with open(config_file_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except (IOError, json.JSONDecodeError):
            pass
    if not isinstance(config_data, dict):
        config_data = {}
    if key in config_data:
        return config_data[key]
    for dotted in _dotted_fallback_keys(key):
        if dotted in config_data:
            return config_data[dotted]
    return _resolve_default(key)


def get_config_int(ctx, key, default=0):
    """Get a config value as int. Accepts float or string (e.g. 50.0 or \"50.00\") from JSON/UI; returns int. Use for int settings like web_cache_max_mb, extend_selection_max_tokens."""
    v = get_config(ctx, key)
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default if default is not None else 0


def get_config_dict(ctx):
    """Return the full config as a dict. Returns {} if missing or on error."""
    config_file_path = _config_path(ctx)
    if not config_file_path or not os.path.exists(config_file_path):
        return {}
    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}


def get_current_endpoint(ctx):
    """Return the current endpoint URL from config, normalized (stripped)."""
    return str(get_config(ctx, "endpoint") or "").strip()


def set_config(ctx, key, value):
    """Set a config key to value. Creates file if needed."""
    config_file_path = _config_path(ctx)
    if not config_file_path:
        return
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
                log.error("Error writing to %s: %s" % (config_file_path, e))


def remove_config(ctx, key):
    """Remove a config key."""
    config_file_path = _config_path(ctx)
    if not config_file_path or not os.path.exists(config_file_path):
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
                log.error("Error writing to %s: %s" % (config_file_path, e))


# Listeners are called when config is changed (e.g. after Settings dialog).
# Sidebar uses weakref in its callback so panels can be GC'd without unregistering.
# FIXME: These duplicated pieces of functionality (add_config_listener / notify_config_changed)
# could be combined into the shared EventBus. This custom pub/sub code can be removed
# and changed to use EventBus. When that happens, update dependent UI code and remove the test code.
_config_listeners = []


def add_config_listener(callback):
    """Register a callable(ctx) to be invoked when config changes (e.g. after Settings OK)."""
    _config_listeners.append(callback)


def notify_config_changed(ctx):
    """Call all registered listeners so UI (e.g. sidebar) can refresh from config."""
    for cb in list(_config_listeners):
        try:
            cb(ctx)
        except Exception as e:
            log.warning("notify_config_changed: listener %s failed: %s", cb, e)


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


def get_model_capability(ctx, model_id, endpoint):
    """Check the model catalog for capabilities (text, image, audio)."""
    provider = get_provider_from_endpoint(endpoint)
    # Check DEFAULT_MODELS for this ID/provider
    for m in DEFAULT_MODELS:
        effective_id = resolve_model_id(m, provider)
        if effective_id == model_id:
            return m.get("capability", "text")
    return ""


def has_native_audio(ctx, model_id, endpoint):
    """Determine if a model supports native audio input.
    Uses persistent cache first, then catalog/heuristics.
    Returns: True if supported, False if unsupported, None if unknown.
    """
    model_id = str(model_id).lower()
    endpoint = _normalize_endpoint_url(endpoint)
    
    # 1. Persistent Cache Check
    cache = get_config(ctx, "audio_support_map")
    if isinstance(cache, dict):
        key = f"{endpoint}@{model_id}"
        if key in cache:
            return as_bool(cache[key])

    # 2. Catalog check
    caps = get_model_capability(ctx, model_id, endpoint)
    if "audio" in caps.split(","):
        return True
        
    # 3. Heuristics (Regex/Keywords) for known audio-native families
    # Gemini (Flash/Pro 1.5+)
    if "gemini" in model_id and "1.5" in model_id:
        return True
    # Explicit audio models
    if "audio-preview" in model_id or "multimodal" in model_id:
        return True
        
    return None # Unknown, allow trying native audio


def set_native_audio_support(ctx, model_id, endpoint, supported):
    """Save the audio support status for a model+endpoint pair."""
    model_id = str(model_id).lower()
    endpoint = _normalize_endpoint_url(endpoint)
    key = f"{endpoint}@{model_id}"
    
    cache = get_config(ctx, "audio_support_map")
    if not isinstance(cache, dict):
        cache = {}
    
    cache[key] = bool(supported)
    set_config(ctx, "audio_support_map", cache)


_model_fetch_cache = {}

def fetch_available_models(endpoint):
    """Fetch available models from endpoint/v1/models. Returns list of IDs or None on error."""
    if not endpoint:
        return None
    url = f"{endpoint.rstrip('/')}/v1/models"
    if url in _model_fetch_cache:
        return _model_fetch_cache[url]

    from plugin.modules.http.client import sync_request
    try:
        data = sync_request(url, parse_json=True)
        if data and isinstance(data, dict) and "data" in data:
            models = []
            for m in data["data"]:
                mid = m.get("id")
                if mid:
                    models.append(mid)
            _model_fetch_cache[url] = models
            return models
    except Exception as e:
                log.warning(f"fetch_available_models failed for {url}: {e}")
    _model_fetch_cache[url] = None
    return None

def populate_combobox_with_lru(ctx, ctrl, current_val, lru_key, endpoint):
    """Helper to populate a combobox with values from an LRU list in config.
    LRU is scoped to the provided endpoint.
    Merges relevant default models based on the capability inferred from lru_key.
    Returns the value set."""
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
    fetched_models = None
    if req_cap == "text" and endpoint and (not provider or provider not in massive_providers):
        fetched_models = fetch_available_models(endpoint)

    if fetched_models is not None:
        for mid in fetched_models:
            if mid not in to_show:
                to_show.append(mid)
    else:
        # Merge defaults into the list if no fetching was done or fetching failed
        if provider:
            for m in DEFAULT_MODELS:
                caps = [c.strip() for c in m.get("capability", "text").split(",")]
                if req_cap in caps:
                    effective_id = resolve_model_id(m, provider)
                    if effective_id and effective_id not in to_show:
                        to_show.append(effective_id)

    curr_val_str = str(current_val).strip()
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
    lru = get_config(ctx, scoped_key)
    if not isinstance(lru, list):
        lru = []

    if val_str in lru:
        lru.remove(val_str)
    lru.insert(0, val_str)
    set_config(ctx, scoped_key, lru[:max_items])


def get_text_model(ctx):
    """Return the text/chat model (stored as text_model, fallback to model)."""
    return str(get_config(ctx, "text_model") or get_config(ctx, "model") or "").strip()


def get_stt_model(ctx):
    """Return the configured STT model."""
    from plugin.framework.default_models import get_provider_defaults
    val = get_config(ctx, "stt_model")
    if val is not None and str(val).strip():
        return str(val).strip()
    current_endpoint = get_current_endpoint(ctx)
    provider = get_provider_from_endpoint(current_endpoint)
    defaults = get_provider_defaults(provider)
    return str(defaults.get("stt_model", "") or "").strip()


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
    lru = get_config(ctx, "endpoint_lru")
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


def get_endpoint_options(services):
    """Options provider for AI endpoint combobox in Tools → Options.

    Returns presets first (value = URL, label = preset label), followed by
    any custom endpoints from endpoint_lru (value/label = URL).
    """
    ctx = get_ctx()
    options = []
    presets = get_endpoint_presets()
    preset_urls = set()
    for label, url in presets:
        url_norm = _normalize_endpoint_url(url)
        preset_urls.add(url_norm)
        options.append({"value": url_norm, "label": label})

    lru = get_config(ctx, "endpoint_lru")
    if not isinstance(lru, list):
        lru = []
    for url in lru:
        u = _normalize_endpoint_url(url)
        if not u or u in preset_urls:
            continue
        options.append({"value": u, "label": u})
    return options


def validate_api_config(config):
    """Validate API config dict (from get_api_config). Returns (ok: bool, error_message: str)."""
    endpoint = (config.get("endpoint") or "").strip()
    if not endpoint:
        return (False, "Please set Endpoint in Settings.")
    model = (config.get("model") or "").strip()
    if not model:
        return (False, "Please set Model in Settings.")
    return (True, "")


def get_image_model(ctx):
    """Return current image model based on provider."""
    image_provider = get_config(ctx, "image_provider")
    if image_provider == "aihorde":
        return str(get_config(ctx, "aihorde_model") or "").strip()
    return str(get_config(ctx, "image_model") or "").strip()


def set_image_model(ctx, val, update_lru=True):
    """Set image model based on provider and notify listeners."""
    if val is None:
        return
    val_str = str(val).strip()
    if not val_str:
        return

    image_provider = get_config(ctx, "image_provider")
    if image_provider == "aihorde":
        set_config(ctx, "aihorde_model", val_str)
    else:
        set_config(ctx, "image_model", val_str)
        if update_lru:
            update_lru_history(ctx, val_str, "image_model_lru", get_current_endpoint(ctx))
    
    notify_config_changed(ctx)


def get_text_model_options(services):
    """Options provider for the simple text model combobox in Tools → Options.

    Uses the per-endpoint model LRU; models are returned as value=ID, label=ID.
    """
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
    """Options provider for the simple image model combobox in Tools → Options.

    Uses the per-endpoint image_model_lru; models are returned as value=ID, label=ID.
    """
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


def get_api_key_for_endpoint(ctx, endpoint):
    """Return API key for the given endpoint."""
    data = get_config(ctx, "api_keys_by_endpoint")
    if not isinstance(data, dict):
        data = {}
    normalized = _normalize_endpoint_url(endpoint or "")
    return data.get(normalized) or ""


def set_api_key_for_endpoint(ctx, endpoint, key):
    """Store API key for the given endpoint in api_keys_by_endpoint."""
    data = get_config(ctx, "api_keys_by_endpoint")
    if not isinstance(data, dict):
        data = {}
    normalized = _normalize_endpoint_url(endpoint or "")
    data[normalized] = str(key)
    set_config(ctx, "api_keys_by_endpoint", data)


def get_api_config(ctx):
    """Build API config dict from ctx for LlmClient. Pass to LlmClient(config, ctx)."""
    endpoint = str(get_config(ctx, "endpoint") or "").rstrip("/")
    is_openwebui = (
        as_bool(get_config(ctx, "is_openwebui"))
        or "open-webui" in endpoint.lower()
        or "openwebui" in endpoint.lower()
    )
    is_openrouter = "openrouter.ai" in endpoint.lower()
    api_key = get_api_key_for_endpoint(ctx, endpoint)

    api_config = {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": get_text_model(ctx),
        "is_openwebui": is_openwebui,
        "is_openrouter": is_openrouter,
        "seed": get_config(ctx, "seed") or "",
        "request_timeout": _safe_int(get_config(ctx, "request_timeout"), 120),
        "chat_max_tool_rounds": _safe_int(get_config(ctx, "chat_max_tool_rounds"), 5),
    }

    temp = _safe_float(get_config(ctx, "temperature"), -1)
    if temp >= 0:
        api_config["temperature"] = temp

    return api_config


def populate_image_model_selector(ctx, ctrl, override_endpoint=None):
    """Adaptive population of image model selector (ComboBox) based on provider.
    When image_provider is endpoint, uses override_endpoint if provided else config endpoint;
    uses strict=True so only models for that endpoint are shown. Returns the value set."""
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
    return populate_combobox_with_lru(ctx, ctrl, current_image_model, "image_model_lru", endpoint)


class ConfigAccessError(Exception):
    """Raised when a module tries to access a private config key."""
    pass


def _dummy_impl(name, services=()):
    def decorator(cls):
        return cls
    return decorator
_implementation = unohelper.implementation if (unohelper and hasattr(unohelper, "implementation")) else _dummy_impl

@_implementation("org.extension.writeragent.ConfigService")
class ConfigService(ServiceBase):
    name = "config"

    def __init__(self):
        self._defaults = {}   # "module.key" -> default_value
        self._manifest = {}   # "module.key" -> field schema
        self._events = None   # EventBus, set after init
        self._config_path = None # For testing

    def initialize(self, ctx):
        pass

    def set_events(self, events):
        """Wire the event bus."""
        self._events = events

    def set_manifest(self, manifest):
        """Load config schemas from the merged manifest."""
        for mod_name, mod_data in manifest.items():
            for field_name, schema in mod_data.get("config", {}).items():
                full_key = f"{mod_name}.{field_name}"
                self._defaults[full_key] = schema.get("default")
                self._manifest[full_key] = schema

    def register_default(self, key, default):
        """Register a single default value."""
        self._defaults[key] = default

    def get(self, key, default=None, caller_module=None):
        """Get a config value, fallback to defaults."""
        self._check_read_access(key, caller_module)

        # Simple mapping: ai.<field> keys from the AI Options page should read
        # from the corresponding top-level settings so Tools → Options and the
        # legacy Settings dialog stay in sync.
        if key.startswith("ai."):
            field = key.split(".", 1)[1]

            # Internal mappings for missing AI_SIMPLE_FIELDS mapping if needed
            if field == "api_key":
                ctx = get_ctx()
                endpoint = get_current_endpoint(ctx)
                return str(get_api_key_for_endpoint(ctx, endpoint) or "")
            elif field == "horde_model":
                return get_config(get_ctx(), "image_model")

            if field in AI_SIMPLE_FIELDS:
                ctx = get_ctx()
                if field == "endpoint":
                    return str(get_config(ctx, "endpoint") or "").strip()
                
                # Internal mappings
                config_key = field
                if field == "aihorde_api_key":
                    config_key = "aihorde_api_key"
                elif field == "max_wait":
                    config_key = "image_max_wait"
                elif field == "nsfw":
                    config_key = "image_nsfw"
                elif field == "censor_nsfw":
                    config_key = "image_censor_nsfw"

                return get_config(ctx, config_key)

        # Test fallback
        if self._config_path and os.path.exists(self._config_path):
             try:
                 with open(self._config_path, "r") as f:
                     data = json.load(f)
                     if key in data:
                         return data[key]
             except Exception as e:
                 log.debug("ConfigService.get config file read error for key %s: %s", key, e)

        ctx = get_ctx()
        val = get_config(ctx, key)
        if val is not None and val != "":
            return val
        if key not in self._defaults:
            return default
        return self._defaults[key]

    def set(self, key, value, caller_module=None):
        """Set a config value."""
        self._check_write_access(key, caller_module)
        old_value = self.get(key)

        # Simple mapping: ai.<field> keys from the AI Options page should write
        # into the corresponding top-level settings (endpoint, model, etc.).
        if key.startswith("ai."):
            field = key.split(".", 1)[1]
            
            # Internal mappings for keys missing from AI_SIMPLE_FIELDS if they map to methods
            if field == "api_key":
                ctx = get_ctx()
                endpoint = get_current_endpoint(ctx)
                set_api_key_for_endpoint(ctx, endpoint, value or "")
                if self._events and value != old_value:
                    self._events.emit("config:changed", key=key, value=value, old_value=old_value)
                return
            elif field == "horde_model":
                set_image_model(get_ctx(), value or "", update_lru=True)
                if self._events and value != old_value:
                    self._events.emit("config:changed", key=key, value=value, old_value=old_value)
                return
            elif field == "horde_api_key":
                set_config(get_ctx(), "aihorde_api_key", value)
                if self._events and value != old_value:
                    self._events.emit("config:changed", key=key, value=value, old_value=old_value)
                return

            if field in AI_SIMPLE_FIELDS:
                ctx = get_ctx()
                if field == "endpoint":
                    resolved = endpoint_from_selector_text(str(value))
                    if resolved:
                        set_config(ctx, "endpoint", resolved)
                elif field == "image_model":
                    set_image_model(ctx, value or "", update_lru=True)
                elif field == "aihorde_api_key":
                    set_config(ctx, "aihorde_api_key", value)
                elif field == "max_wait":
                    set_config(ctx, "image_max_wait", int(value) if value else 5)
                elif field == "nsfw":
                    set_config(ctx, "image_nsfw", value)
                elif field == "censor_nsfw":
                    set_config(ctx, "image_censor_nsfw", value)
                else:
                    # Direct 1:1 mapping to top-level key.
                    set_config(ctx, field, value)

                if self._events and value != old_value:
                    self._events.emit(
                        "config:changed",
                        key=key,
                        value=value,
                        old_value=old_value,
                    )
                return

        # Test fallback
        if self._config_path:
            data = {}
            if os.path.exists(self._config_path):
                try:
                    with open(self._config_path, "r") as f:
                        data = json.load(f)
                except Exception as e:
                    log.debug("ConfigService.set config file load error: %s", e)
            data[key] = value
            with open(self._config_path, "w") as f:
                json.dump(data, f)
        else:
            set_config(get_ctx(), key, value)

        if self._events and value != old_value:
            self._events.emit("config:changed", key=key, value=value, old_value=old_value)

    def set_batch(self, changes, old_values=None):
        """Set multiple config values at once. Returns dict of changed keys.

        Used by the generic Options handler; delegates to set() so that
        ai.<field> keys are also mapped through the simple AI settings layer.
        """
        diffs = {}
        for key, value in (changes or {}).items():
            before = self.get(key)
            if value == before:
                continue
            self.set(key, value)
            diffs[key] = (before, value)
        return diffs

    def remove(self, key, caller_module=None):
        """Reset a config key."""
        self._check_write_access(key, caller_module)
        if self._config_path and os.path.exists(self._config_path):
             try:
                 with open(self._config_path, "r") as f:
                     data = json.load(f)
                 if key in data:
                     del data[key]
                     with open(self._config_path, "w") as f:
                         json.dump(data, f)
             except Exception as e:
                 log.warning("ConfigService.remove config file modify error for key %s: %s", key, e)
        else:
            remove_config(get_ctx(), key)

    def get_dict(self):
        """Return all config."""
        # This is a simplification for now
        ctx = get_ctx()
        if self._config_path and os.path.exists(self._config_path):
             try:
                 with open(self._config_path, "r") as f:
                     return json.load(f)
             except Exception as e:
                 log.debug("ConfigService.get_dict config file read error: %s", e)
        return get_config_dict(ctx)

    def _check_read_access(self, key, caller_module):
        if caller_module is None or "." not in key:
            return
        module = key.split(".", 1)[0]
        if module == caller_module:
            return
        schema = self._manifest.get(key, {})
        if not schema.get("public", False):
            raise ConfigAccessError(f"Module '{caller_module}' cannot read private config '{key}'")

    def _check_write_access(self, key, caller_module):
        if caller_module is None or "." not in key:
            return
        module = key.split(".", 1)[0]
        if module != caller_module:
            raise ConfigAccessError(f"Module '{caller_module}' cannot write to '{key}'")

    def proxy_for(self, module_name):
        return ModuleConfigProxy(self, module_name)


class ModuleConfigProxy:
    def __init__(self, config_service, module_name):
        self._config = config_service
        self._module = module_name

    def get(self, key, default=None):
        if "." not in key:
            key = f"{self._module}.{key}"
        return self._config.get(key, default, caller_module=self._module)

    def set(self, key, value):
        if "." not in key:
            key = f"{self._module}.{key}"
        self._config.set(key, value, caller_module=self._module)

    def remove(self, key):
        if "." not in key:
            key = f"{self._module}.{key}"
        self._config.remove(key, caller_module=self._module)
