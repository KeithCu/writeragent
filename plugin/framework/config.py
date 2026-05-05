from plugin.framework.utils import normalize_endpoint_url

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
import ipaddress
import urllib.parse
import json
import logging
import dataclasses
import time
from typing import Any, Callable, Dict, cast

from plugin.framework.utils import get_plugin_dir
from plugin.framework.event_bus import global_event_bus
from plugin.framework.service_base import ServiceBase
from plugin.framework.uno_context import get_ctx
from plugin.framework.default_models import DEFAULT_MODELS, resolve_model_id, get_provider_defaults
from plugin.framework.base_errors import ConfigError, NetworkError
from plugin.framework.errors import safe_call
from plugin.framework.types import ModelCapability
from plugin.framework.i18n import _
from plugin.modules.http.requests import sync_request

try:
    from plugin._manifest import MODULES
except ImportError:
    MODULES = []

_uno_mod: Any
_unohelper_mod: Any
try:
    import uno as _uno_impl
    import unohelper as _unohelper_impl

    _uno_mod = _uno_impl
    _unohelper_mod = _unohelper_impl
except ImportError:
    _uno_mod = None
    _unohelper_mod = None
uno: Any = _uno_mod
unohelper: Any = _unohelper_mod

log = logging.getLogger(__name__)


CONFIG_FILENAME = "writeragent.json"

# MCP server: mcp_enabled (bool, default False), mcp_port (int, default 8765)

# Max items for all LRU lists; base names also listed in _LRU_LIST_CONFIG_KEY_PREFIXES for get_config defaults.
LRU_MAX_ITEMS = 10

# Keys used by populate_combobox_with_lru / update_lru_history (including endpoint-scoped "name@url").
_LRU_LIST_CONFIG_KEY_PREFIXES: frozenset[str] = frozenset({"model_lru", "prompt_lru", "image_model_lru", "audio_model_lru", "endpoint_lru", "image_base_size_lru"})


def _is_lru_list_config_key(key: str) -> bool:
    if key in _LRU_LIST_CONFIG_KEY_PREFIXES:
        return True
    for prefix in _LRU_LIST_CONFIG_KEY_PREFIXES:
        if key.startswith(prefix + "@"):
            return True
    return False


# Endpoint presets: local first, then FOSS-friendly / open-model providers, proprietary last. Base URLs only; api.py adds /v1 (or /api for OpenWebUI).
# Uncomment any FOSS-focused line below once the base URL is verified OpenAI-compatible.
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
        raise ConfigError("UNO context is required to resolve config path")
    try:
        sm = safe_call(ctx.getServiceManager, "Get ServiceManager")
        path_settings = safe_call(sm.createInstanceWithContext, "Create PathSettings", "com.sun.star.util.PathSettings", ctx)
        user_config_path = getattr(path_settings, "UserConfig", "")
        if uno and user_config_path and str(user_config_path).startswith("file://"):
            user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
        return os.path.join(user_config_path, CONFIG_FILENAME)
    except Exception as e:
        raise ConfigError(f"Failed to resolve config path: {e}", "CONFIG_PATH_ERROR") from e


def user_config_dir(ctx):
    """Return LibreOffice user config directory."""
    if ctx is None:
        raise ConfigError("UNO context is required to resolve config dir")
    try:
        p = _config_path(ctx)
        return os.path.dirname(p) if p else None
    except Exception as e:
        raise ConfigError(f"Failed to resolve config dir: {e}", "CONFIG_DIR_ERROR") from e


def _get_schema_default(key):
    """Return default for key from MODULES (module.yaml schema). Supports flat and dotted keys."""
    if not MODULES:
        return None
    # Dotted key (e.g. agent_backend.backend_id)
    if "." in key:
        mod_name, field_name = key.split(".", 1)
        for m in MODULES:
            if m.get("name") == mod_name:
                config = m.get("config", {})
                if isinstance(config, dict):
                    for fname, schema in config.items():
                        if fname == field_name and isinstance(schema, dict) and "default" in schema:
                            return schema["default"]
        return None
    # Flat key: find first module that has this config field
    for m in MODULES:
        config = m.get("config", {})
        if isinstance(config, dict):
            for fname, schema in config.items():
                if fname == key and isinstance(schema, dict) and "default" in schema:
                    return schema["default"]
    return None


def _dotted_fallback_keys(key):
    """Yield dotted key variants for key using MODULES (e.g. extend_selection_max_tokens -> chatbot.extend_selection_max_tokens)."""
    if not MODULES:
        return
    if "." in key:
        return
    for m in MODULES:
        mod_name = m.get("name", "")
        if not mod_name:
            continue
        config = m.get("config", {})
        if isinstance(config, dict):
            for fname in config:
                if fname == key:
                    yield f"{mod_name}.{fname}"
                    break


@dataclasses.dataclass
class WriterAgentConfig:
    """Dataclass schema for WriterAgent configuration."""

    log_level: str = "DEBUG"
    endpoint: str = "http://127.0.0.1:5000"
    text_model: str = ""
    model: str = ""
    temperature: float = -1.0
    additional_instructions: str = ""
    chat_context_length: int = 8000
    chat_max_tokens: int = 16384
    request_timeout: int = 120
    chat_max_tool_rounds: int = 25
    stt_model: str = ""
    api_keys_by_endpoint: Dict[str, str] = dataclasses.field(default_factory=dict)
    aihorde_api_key: str = ""
    image_base_size: int = 512
    image_default_aspect: str = "Square"
    image_cfg_scale: float = 7.5
    image_steps: int = -1
    image_nsfw: bool = False
    image_censor_nsfw: bool = True
    image_max_wait: int = 5
    image_auto_gallery: bool = True
    image_insert_frame: bool = False
    image_translate_prompt: bool = True
    image_translate_from: str = ""
    image_model: str = ""
    image_provider: str = "aihorde"
    aihorde_model: str = "stable_diffusion"
    seed: str = ""
    chatbot_show_search_thinking: bool = False
    enable_agent_log: bool = False
    # Last extension update.xml check time (unix seconds); see extension_update_check.py
    extension_update_check_epoch: float = 0.0
    web_cache_max_mb: int = 50
    web_cache_validity_days: int = 7
    is_openwebui: bool = False
    extend_selection_system_prompt: str = ""
    edit_selection_system_prompt: str = ""
    audio_support_map: Dict[str, bool] = dataclasses.field(default_factory=dict)
    chat_direct_image: bool = False
    calc_prompt_max_tokens: int = 70
    extend_selection_max_tokens: int = 1000
    edit_selection_max_new_tokens: int = 1000
    # When True, treat endpoint as OpenRouter (e.g. custom proxy) even if the URL lacks openrouter.ai.
    is_openrouter: bool = False
    # Merged into POST …/chat/completions JSON when OpenRouter is active; see AGENTS.md.
    openrouter_chat_extra: Dict[str, Any] = dataclasses.field(default_factory=dict)

    # Store arbitrary module.yaml config entries
    _extra_config: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def validate(self):
        """Perform validation of config keys and emit warnings or fix values."""
        # Clean up any translated headers that incorrectly made it into config
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            if isinstance(val, str) and "Project-Id-Version:" in val:
                log.debug("config validate: stripped PO/header from dataclass field %r (len=%s)", f.name, len(val))
                # Default seed should be -1, not empty string.
                if f.name == "seed":
                    setattr(self, f.name, "-1")
                else:
                    setattr(self, f.name, "")

        for k, v in list(self._extra_config.items()):
            if isinstance(v, str) and "Project-Id-Version:" in v:
                log.debug("config validate: stripped PO/header from extra key %r (len=%s)", k, len(v))
                self._extra_config[k] = ""

        self.endpoint = normalize_endpoint_url(str(self.endpoint or ""))

        # Normalize localized strings back to internal keys (e.g. image_default_aspect, agent_backend.*)
        # Dotted module keys live in _extra_config; flat keys are dataclass attributes.
        try:
            from plugin.framework.settings_dialog import get_settings_field_specs

            specs = get_settings_field_specs(None)
            for spec in specs:
                if "options" not in spec:
                    continue
                key = spec["name"].replace("__", ".")
                if key in self._extra_config:
                    val = self._extra_config.get(key)
                elif "." not in key and hasattr(self, key):
                    val = getattr(self, key)
                else:
                    continue
                for opt in spec["options"]:
                    if isinstance(opt, dict):
                        lbl = opt.get("label", opt.get("value", ""))
                        if _(lbl) == str(val):
                            canon = opt.get("value", lbl)
                            if key in self._extra_config:
                                self._extra_config[key] = canon
                            else:
                                setattr(self, key, canon)
                            break
        except Exception as e:
            log.warning(f"Failed to normalize config against specs: {e}")

        if not isinstance(self.chat_max_tokens, int) or self.chat_max_tokens < 0:
            log.warning("Invalid chat_max_tokens %s, falling back to 16384", self.chat_max_tokens)
            self.chat_max_tokens = 16384

        if not isinstance(self.request_timeout, int) or self.request_timeout <= 0:
            log.warning("Invalid request_timeout %s, falling back to 120", self.request_timeout)
            self.request_timeout = 120

        _cmtr_def = 25
        r_cmtr = self.chat_max_tool_rounds
        cmtr_ok: int | None = None
        if type(r_cmtr) is bool:
            pass
        elif isinstance(r_cmtr, int) and r_cmtr >= 1:
            cmtr_ok = r_cmtr
        elif isinstance(r_cmtr, str):
            t = r_cmtr.strip()
            if t:
                try:
                    n = int(float(t))
                    if n >= 1:
                        cmtr_ok = n
                except (ValueError, TypeError):
                    pass
        elif r_cmtr is not None and r_cmtr != "":
            try:
                n = int(float(r_cmtr))
                if n >= 1:
                    cmtr_ok = n
            except (ValueError, TypeError):
                pass
        if cmtr_ok is None:
            self.chat_max_tool_rounds = _cmtr_def
            is_blank = r_cmtr == "" or (isinstance(r_cmtr, str) and r_cmtr.strip() == "")
            if is_blank:
                log.debug("chat_max_tool_rounds empty, using default %s", _cmtr_def)
            else:
                log.warning("Invalid chat_max_tool_rounds %r, falling back to %s", r_cmtr, _cmtr_def)
        else:
            self.chat_max_tool_rounds = cmtr_ok

        if not isinstance(self.temperature, (int, float)):
            log.warning("Invalid temperature %s, falling back to -1.0", self.temperature)
            self.temperature = -1.0

        if not isinstance(self.image_cfg_scale, (int, float)) or self.image_cfg_scale < 0:
            log.warning("Invalid image_cfg_scale %s, falling back to 7.5", self.image_cfg_scale)
            self.image_cfg_scale = 7.5

        if not isinstance(self.openrouter_chat_extra, dict):
            log.warning("Invalid openrouter_chat_extra (not a dict), resetting to {}")
            self.openrouter_chat_extra = {}

        return self

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WriterAgentConfig":
        """Load from a dictionary, mapping known fields and pushing others to _extra_config."""
        field_names = {f.name for f in dataclasses.fields(cls) if f.name != "_extra_config"}
        known_kwargs = {}
        extra_kwargs = {}

        for key, value in data.items():
            safe_key = key.replace(".", "_")
            if safe_key in field_names:
                known_kwargs[safe_key] = value
            else:
                extra_kwargs[key] = value

        config = cls(**known_kwargs)
        config._extra_config = extra_kwargs
        return config

    def to_dict(self) -> Dict[str, Any]:
        """Convert back to dictionary, expanding _extra_config."""
        out = {}
        for f in dataclasses.fields(self):
            if f.name == "_extra_config":
                continue
            out[f.name] = getattr(self, f.name)
        out.update(self._extra_config)
        return out


def _resolve_default(key):
    """Resolve default for key: schema first, then central dict. Safe fallbacks for None."""
    if key == "log_level":
        tests_dir = os.path.join(get_plugin_dir(), "tests")
        return "DEBUG" if os.path.isdir(tests_dir) else "WARN"

    val = _get_schema_default(key)
    if val is not None:
        return val

    if _is_lru_list_config_key(key):
        return []

    # Get from default config object
    default_config = WriterAgentConfig()

    # Map dotted keys to flat keys if they match (e.g. chatbot.show_search_thinking)
    safe_key = key.replace(".", "_")
    field_names = {f.name for f in dataclasses.fields(default_config)}
    if safe_key in field_names:
        val = getattr(default_config, safe_key)
        if val is not None:
            return val

    # Strict check: if not in schema and not a recognized dynamic pattern, it's a bug.
    raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", "CONFIG_KEY_NOT_FOUND", details={"key": key})


# In-memory configuration cache so we don't open/parse/validate writeragent.json
# on every single get_config access.
_cached_config_dict = None
_cached_config_mtime = 0
_cached_config_mtime_last_checked = 0.0


def _build_validated_config_export(data: Dict[str, Any], config: "WriterAgentConfig") -> Dict[str, Any]:
    """Merge validated WriterAgentConfig into a dict with the same keys as JSON `data`.

    Known dataclass fields are read from attributes; all other keys (e.g. ``agent_backend.path``)
    must come from ``config._extra_config`` after :meth:`WriterAgentConfig.validate`.
    """
    out: Dict[str, Any] = {}
    field_names = {f.name for f in dataclasses.fields(config) if f.name != "_extra_config"}
    for k, v in data.items():
        safe_key = k.replace(".", "_")
        if safe_key in field_names:
            out[k] = getattr(config, safe_key)
        else:
            merged = config._extra_config.get(k, v)
            if merged != v:
                log.debug("config export: extra key %r merged after validate (raw_len=%s merged_len=%s)", k, len(str(v)), len(str(merged)))
            out[k] = merged
    return out


def _get_validated_config_dict(ctx):
    """Return the full validated config as a dict, using an in-memory cache
    keyed off the file modification time."""
    global _cached_config_dict, _cached_config_mtime, _cached_config_mtime_last_checked

    try:
        config_file_path = _config_path(ctx)
    except ConfigError:
        return {}

    if not config_file_path or not os.path.exists(config_file_path):
        return {}

    current_time = time.time()

    # 2-second cache for the mtime check
    if _cached_config_dict is not None and (current_time - _cached_config_mtime_last_checked) < 2.0:
        return _cached_config_dict

    try:
        current_mtime = os.path.getmtime(config_file_path)
    except OSError:
        current_mtime = 0

    _cached_config_mtime_last_checked = current_time

    if _cached_config_dict is not None and current_mtime == _cached_config_mtime and current_mtime != 0:
        return _cached_config_dict

    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ConfigError("Config must be a JSON object", "CONFIG_INVALID_FORMAT")

        # Perform validation when config is loaded
        config = WriterAgentConfig.from_dict(data)
        config.validate()

        out = _build_validated_config_export(data, config)

        _cached_config_dict = out
        _cached_config_mtime = current_mtime
        return out
    except json.JSONDecodeError as e:
        log.error("Invalid JSON in %s: %s", config_file_path, e)
        return {}
    except OSError as e:
        log.error("Error reading %s: %s", config_file_path, e)
        return {}


def get_config(ctx, key):
    """Get a config value by key. JSON overrides; when key is missing, use schema default then central fallback."""
    config_data = _get_validated_config_dict(ctx)
    if not isinstance(config_data, dict):
        config_data = {}

    if key in config_data:
        return config_data[key]

    for dotted in _dotted_fallback_keys(key):
        if dotted in config_data:
            return config_data[dotted]

    return _resolve_default(key)


def get_config_int(ctx, key) -> int:
    """Get a config value as int. All requested keys MUST be in the schema (WriterAgentConfig or MODULES).
    Throws ConfigError if the key is missing or invalid."""
    v = get_config(ctx, key)
    # Empty string or None from JSON/UI: use schema default (same as missing key).
    if v == "" or v is None:
        v = _resolve_default(key)
    # _resolve_default returns "" for unknown keys that slip through without a dataclass default.
    if v == "":
        raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", "CONFIG_KEY_NOT_FOUND", details={"key": key})
    try:
        return int(float(cast("Any", v)))
    except (ValueError, TypeError) as e:
        raise ConfigError(f"Config key {key!r} has non-integer value: {v!r}", "CONFIG_TYPE_ERROR") from e


def get_config_str(ctx, key) -> str:
    """Get a config value as str. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found."""
    v = get_config(ctx, key)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def get_config_bool(ctx, key) -> bool:
    """Get a config value as bool. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found."""
    v = get_config(ctx, key)
    return as_bool(v)


def get_config_float(ctx, key) -> float:
    """Get a config value as float. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found."""
    v = get_config(ctx, key)
    try:
        return float(cast("Any", v))
    except (ValueError, TypeError) as e:
        raise ConfigError(f"Config key {key!r} has non-float value: {v!r}", "CONFIG_TYPE_ERROR") from e


def get_config_dict(ctx):
    """Return the full config as a dict. Returns {} if missing or on error."""
    return _get_validated_config_dict(ctx)


def get_current_endpoint(ctx):
    """Return the current endpoint URL from config, normalized (stripped)."""
    return str(get_config(ctx, "endpoint") or "").strip()


def set_config(ctx, key, value):
    """Set a config key to value. Creates file if needed."""
    try:
        config_file_path = _config_path(ctx)
    except ConfigError:
        return

    if not config_file_path:
        return
    if os.path.exists(config_file_path):
        try:
            with open(config_file_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            if not isinstance(config_data, dict):
                config_data = {}
        except json.JSONDecodeError as e:
            log.warning("Invalid JSON when updating %s: %s", config_file_path, e)
            config_data = {}
        except OSError as e:
            log.warning("Error reading %s: %s", config_file_path, e)
            config_data = {}
    else:
        config_data = {}
    if config_data.get(key) == value:
        return
    config_data[key] = value
    try:
        with open(config_file_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)

        global _cached_config_dict, _cached_config_mtime, _cached_config_mtime_last_checked
        _cached_config_dict = None
        _cached_config_mtime = 0
        _cached_config_mtime_last_checked = 0.0

        global_event_bus.emit("config:changed", ctx=ctx)

    except OSError as e:
        log.error("Error writing to %s: %s", config_file_path, e)
        raise ConfigError(f"Failed to save config: {e}", "CONFIG_SAVE_ERROR") from e


def remove_config(ctx, key):
    """Remove a config key."""
    try:
        config_file_path = _config_path(ctx)
    except ConfigError:
        return

    if not config_file_path or not os.path.exists(config_file_path):
        return
    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        if not isinstance(config_data, dict):
            return
    except (OSError, json.JSONDecodeError):
        return
    config_data.pop(key, None)
    try:
        with open(config_file_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)

        global _cached_config_dict, _cached_config_mtime, _cached_config_mtime_last_checked
        _cached_config_dict = None
        _cached_config_mtime = 0
        _cached_config_mtime_last_checked = 0.0

        global_event_bus.emit("config:changed", ctx=ctx)

    except OSError as e:
        log.error("Error writing to %s: %s", config_file_path, e)
        raise ConfigError(f"Failed to remove config key: {e}", "CONFIG_SAVE_ERROR") from e


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
    url = normalize_endpoint_url(endpoint).lower()
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
    if "api.groq.com" in url:
        return "groq"
    if "api.cerebras.ai" in url:
        return "cerebras"
    if "api.perplexity.ai" in url:
        return "perplexity"
    if "api.x.ai" in url:
        return "xai"
    if "api.anthropic.com" in url:
        return "anthropic"
    if "generativelanguage.googleapis.com" in url:
        return "google"
    if "localhost:1234" in url:
        return "lmstudio"
    if "localhost:4891" in url:
        return "gpt4all"
    return None


def get_model_capability(ctx, model_id, endpoint):
    """Check the model catalog for capabilities bitmask."""
    provider = get_provider_from_endpoint(endpoint)
    # Check DEFAULT_MODELS for this ID/provider
    for m in DEFAULT_MODELS:
        effective_id = resolve_model_id(m, provider)
        if effective_id == model_id:
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


# GET {base}/v1/models — memoized for the lifetime of this Python process (LibreOffice
# session). Key is normalized URL, or ``url + "\\x1f" + api_key`` when ``ctx`` is passed
# (same host, different keys must not share cache). Value is model id list or None after failure.
_model_fetch_cache: dict[str, list[str] | None] = {}


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
    url = f"{base}/v1/models"
    cache_key = _model_fetch_cache_key(url, ctx, base, api_key_override)
    if cache_key in _model_fetch_cache:
        return _model_fetch_cache[cache_key]

    req_headers: dict[str, str] = {}
    if ctx is not None:
        from plugin.framework.auth import AuthError, build_auth_headers, resolve_auth_for_config

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
            _model_fetch_cache[cache_key] = None
            return None

    try:
        data = sync_request(url, parse_json=True, headers=req_headers if req_headers else None)
        if data and isinstance(data, dict) and "data" in data:
            models = []
            for m in data["data"]:
                mid = m.get("id")
                if mid:
                    models.append(mid)
            _model_fetch_cache[cache_key] = models
            return models
    except (ValueError, TypeError, IOError) as e:
        log.warning("fetch_available_models network/parse error for %s: %s", url, e)
    except Exception as e:
        if isinstance(e, NetworkError):
            log.warning("fetch_available_models NetworkError for %s: %s", url, e)
        else:
            log.warning("fetch_available_models unexpected error for %s: %s", url, type(e).__name__)
    _model_fetch_cache[cache_key] = None
    return None


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
    elif req_cap == "text" and endpoint and (not provider or provider not in massive_providers):
        fetched_models = fetch_available_models(endpoint, ctx)

    if fetched_models is not None:
        for mid in fetched_models:
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
    lru = list(lru)
    if val_str in lru:
        lru.remove(val_str)
    lru.insert(0, val_str)
    new_lru = lru[:max_items]
    old = get_config(ctx, scoped_key)
    if isinstance(old, list) and old == new_lru:
        return
    set_config(ctx, scoped_key, new_lru)


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


def get_endpoint_presets():
    """Return list of (label, url) for endpoint selector, in display order."""
    return list(ENDPOINT_PRESETS)


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
    """Options provider for AI endpoint combobox in Tools → Options.

    Returns presets first (value = URL, label = preset label), followed by
    any custom endpoints from endpoint_lru (value/label = URL).
    """
    ctx = get_ctx()
    options = []
    presets = get_endpoint_presets()
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
            update_lru_history(ctx, val_str, "image_model_lru", get_current_endpoint(ctx))


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
    normalized = normalize_endpoint_url(endpoint or "")
    return data.get(normalized) or ""


def set_api_key_for_endpoint(ctx, endpoint, key):
    """Store API key for the given endpoint in api_keys_by_endpoint."""
    data = get_config(ctx, "api_keys_by_endpoint")
    if not isinstance(data, dict):
        data = {}
    normalized = normalize_endpoint_url(endpoint or "")
    data[normalized] = str(key)
    set_config(ctx, "api_keys_by_endpoint", data)


def get_api_config(ctx):
    """Build API config dict from ctx for LlmClient. Pass to LlmClient(config, ctx)."""
    endpoint = str(get_config(ctx, "endpoint") or "").rstrip("/")
    is_openwebui = as_bool(get_config(ctx, "is_openwebui")) or "open-webui" in endpoint.lower() or "openwebui" in endpoint.lower()
    is_openrouter = "openrouter.ai" in endpoint.lower() or as_bool(get_config(ctx, "is_openrouter"))
    api_key = get_api_key_for_endpoint(ctx, endpoint)

    api_config = {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": get_text_model(ctx),
        "is_openwebui": is_openwebui,
        "is_openrouter": is_openrouter,
        "seed": get_config_str(ctx, "seed"),
        "request_timeout": get_config_int(ctx, "request_timeout"),
        "chat_max_tool_rounds": get_config_int(ctx, "chat_max_tool_rounds"),
    }

    temp = get_config_float(ctx, "temperature")
    if temp >= 0:
        api_config["temperature"] = temp

    if is_openrouter:
        ore = get_config(ctx, "openrouter_chat_extra")
        if isinstance(ore, dict) and ore:
            api_config["openrouter_chat_extra"] = ore

    return api_config


def populate_image_model_selector(ctx, ctrl, override_endpoint=None, *, remote_models: list[str] | None = None, skip_remote_fetch: bool = False):
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
    return populate_combobox_with_lru(ctx, ctrl, current_image_model, "image_model_lru", endpoint, remote_models=remote_models, skip_remote_fetch=skip_remote_fetch)


class ConfigAccessError(ConfigError):
    """Raised when a module tries to access a private config key."""

    def __init__(self, message, code="CONFIG_ACCESS_ERROR", context=None):
        super().__init__(message, code=code, context=context)


def _dummy_impl(name, services=()):
    def decorator(cls):
        return cls

    return decorator


def _uno_service_implementation_decorator() -> Callable[..., Any]:
    """Return UNO's ``unohelper.implementation`` or a no-op when unohelper is mocked.

    Headless pytest loads ``conftest`` before ``plugin.framework.config``; ``unohelper``
    may be a ``MagicMock``. That exposes a fake ``implementation`` callable, which is
    not LibreOffice's registration helper and breaks ``ConfigService()`` if used as a
    class decorator (tests would get nested mocks instead of the real class).
    """
    if not unohelper:
        return _dummy_impl
    impl = getattr(unohelper, "implementation", None)
    if impl is None:
        return _dummy_impl
    try:
        from unittest.mock import Mock

        if isinstance(impl, Mock):
            return _dummy_impl
    except ImportError:
        pass
    if not callable(impl):
        return _dummy_impl
    return cast("Callable[..., Any]", impl)


_implementation: Callable[..., Any] = _uno_service_implementation_decorator()


@_implementation("org.extension.writeragent.ConfigService")
class ConfigService(ServiceBase):
    name = "config"

    def __init__(self):
        self._defaults = {}  # "module.key" -> default_value
        self._manifest = {}  # "module.key" -> field schema
        self._events = None  # EventBus, set after init
        self._config_path = None  # For testing

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
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        raise ConfigError("Config file must be a JSON object")
                    if key in data:
                        return data[key]
            except json.JSONDecodeError as e:
                log.debug("ConfigService.get invalid JSON in %s: %s", self._config_path, e)
            except OSError as e:
                log.debug("ConfigService.get IO error for %s: %s", self._config_path, e)
            except ConfigError as e:
                log.debug("ConfigService.get ConfigError: %s", e)

        ctx = get_ctx()
        try:
            val = get_config(ctx, key)
            if val is not None and val != "":
                return val
        except ConfigError:
            pass

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
                if value != old_value:
                    bus = self._events or global_event_bus
                    bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=ctx)
                return
            elif field == "horde_model":
                ctx = get_ctx()
                set_image_model(ctx, value or "", update_lru=True)
                if value != old_value:
                    bus = self._events or global_event_bus
                    bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=ctx)
                return
            elif field == "horde_api_key":
                ctx = get_ctx()
                set_config(ctx, "aihorde_api_key", value)
                if value != old_value:
                    bus = self._events or global_event_bus
                    bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=ctx)
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

                if value != old_value:
                    bus = self._events or global_event_bus
                    bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=ctx)
                return

        # Test fallback
        if self._config_path:
            data = {}
            if os.path.exists(self._config_path):
                try:
                    with open(self._config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        data = {}
                except (OSError, json.JSONDecodeError) as e:
                    log.debug("ConfigService.set config file load error: %s", e)
            data[key] = value
            try:
                with open(self._config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError as e:
                log.error("ConfigService.set config file save error: %s", e)

            ctx = None  # No UNO context in file-based test mode
        else:
            ctx = get_ctx()
            set_config(ctx, key, value)

        if value != old_value:
            bus = self._events or global_event_bus
            bus.emit("config:changed", key=key, value=value, old_value=old_value, ctx=ctx)

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
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and key in data:
                    del data[key]
                    with open(self._config_path, "w", encoding="utf-8") as f:
                        json.dump(data, f)
            except (OSError, json.JSONDecodeError) as e:
                log.warning("ConfigService.remove config file error for key %s: %s", key, e)
        else:
            remove_config(get_ctx(), key)

    def get_dict(self):
        """Return all config."""
        # This is a simplification for now
        ctx = get_ctx()
        if self._config_path and os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return {}
                return data
            except (OSError, json.JSONDecodeError) as e:
                log.debug("ConfigService.get_dict config file read error: %s", e)
                return {}
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
