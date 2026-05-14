
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

from plugin.framework.service import ServiceBase
from plugin.framework.event_bus import global_event_bus
from plugin.framework.uno_context import get_ctx
from plugin.framework.default_models import DEFAULT_MODELS, resolve_model_id, get_provider_defaults
from plugin.framework.errors import ConfigError, NetworkError, safe_call
from plugin.framework.constants import ModelCapability, get_plugin_dir
from plugin.framework.i18n import _

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


from plugin.framework.url_utils import (
    get_api_version_suffix,
    normalize_endpoint_url,
    get_url_hostname,
    get_url_domain,
    get_url_path,
    get_url_query_dict,
    get_url_path_and_query,
    is_pdf_url,
)

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
    ("Z.ai", "https://api.z.ai/v4"),
]

# Simple AI settings fields that the Tools → Options \"AI\" page should map
# directly to top-level config keys (endpoint, model, etc.).
AI_SIMPLE_FIELDS = {"endpoint", "text_model", "image_model", "stt_model", "temperature", "chat_max_tokens", "chat_context_length", "request_timeout", "additional_instructions", "aihorde_api_key", "image_provider", "nsfw", "censor_nsfw", "max_wait"}


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
    # Last extension update.xml check time (unix seconds); see modules/chatbot/extension_update_check.py
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

        self.endpoint = normalize_endpoint_url(str(self.endpoint or ""), is_openwebui=self.is_openwebui)

        # Normalize localized strings back to internal keys (e.g. image_default_aspect, agent_backend.*)
        # Dotted module keys live in _extra_config; flat keys are dataclass attributes.
        try:
            from plugin.chatbot.settings_dialog import get_settings_field_specs

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


def get_config_bool_safe(ctx: Any, key: str, default: bool = False) -> bool:
    """Safely read a boolean config value, returning default on failure."""
    try:
        return get_config_bool(ctx, key)
    except Exception:
        return default


def get_config_int_safe(ctx: Any, key: str, default: int = 0) -> int:
    """Safely read an integer config value, returning default on failure."""
    try:
        return get_config_int(ctx, key)
    except Exception:
        return default


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
    if "api.z.ai" in url or "z.ai" in url:
        return "zai"
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


def is_grammar_enabled(ctx):
    """True if the AI grammar checker is enabled on the Doc tab."""
    return get_config_bool_safe(ctx, "doc.grammar_proofreader_enabled")


def get_endpoint_presets():
    """Return list of (label, url) for endpoint selector, in display order."""
    return list(ENDPOINT_PRESETS)





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
            from plugin.chatbot.config_ui_helpers import update_lru_history
            update_lru_history(ctx, val_str, "image_model_lru", get_current_endpoint(ctx))





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



