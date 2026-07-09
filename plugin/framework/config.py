
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
"""Configuration logic for WriterAgent.

``init_config(ctx)`` runs once at bootstrap (``MainBootstrapJob`` / ``bootstrap()``);
the config path is cached. All other I/O — ``get_config``, ``set_config``, typed
getters, ``get_api_config`` — does **not** take ``ctx``; use ``get_ctx()`` only for
UNO operations.

``writeragent.json`` lives under the LibreOffice user profile (Linux:
``~/.config/libreoffice/{4,24}/user/``; macOS: ``~/Library/Application Support/LibreOffice/4/user/``;
Windows: ``%APPDATA%\\LibreOffice\\4\\user\\``). Broken JSON is copied to
``.bak`` when possible; ``json_repair`` fixes small typos on read.

Schema-backed coercion, option canonicalization, and min/max bounds live here
so UI controllers can pass raw dialog values to ``set_config`` without copying
validation rules from ``module.yaml``.
"""
import dataclasses
import json
import logging
import os
import shutil
import time
from typing import Any, Dict

from plugin.framework.constants import get_plugin_dir
from plugin.framework.errors import ConfigError, ConfigValidationError, safe_call
from plugin.framework.event_bus import global_event_bus
from plugin.framework.i18n import _
from plugin.framework.json_utils import repair_json

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
    normalize_endpoint_url,
)

log = logging.getLogger(__name__)


# --- Module constants ---

CONFIG_FILENAME = "writeragent.json"
CONFIG_BACKUP_SUFFIX = ".bak"

# Max items for all LRU lists; base names also listed in _LRU_LIST_CONFIG_KEY_PREFIXES for get_config defaults.
LRU_MAX_ITEMS = 10

# Keys used by populate_combobox_with_lru / update_lru_history (including endpoint-scoped "name@url").
_LRU_LIST_CONFIG_KEY_PREFIXES: frozenset[str] = frozenset({"model_lru", "prompt_lru", "image_model_lru", "audio_model_lru", "endpoint_lru", "image_base_size_lru"})

# Simple AI settings fields that the Tools → Options "AI" page should map
# directly to top-level config keys (endpoint, model, etc.).
AI_SIMPLE_FIELDS = {"endpoint", "text_model", "image_model", "stt_model", "temperature", "chat_max_tokens", "request_timeout", "additional_instructions", "parallel_tool_calls"}


# --- Small helpers ---


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


def parse_int_robust(val) -> int:
    """Robustly parse an integer value from a string, float, or other type,
    handling locale-specific decimal commas (like "8765,0" in German)."""
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if val is None:
        raise ValueError("Cannot parse None as int")

    s = str(val).strip()
    if not s:
        raise ValueError("Cannot parse empty string as int")

    # Try normal int parsing first
    try:
        return int(s)
    except (ValueError, TypeError):
        pass

    # Handle European decimal commas by replacing ',' with '.'
    # but only if there is a single comma and it looks like a decimal separator
    # e.g., "8765,0" -> "8765.0"
    if "," in s:
        cleaned = s.replace(",", ".")
        try:
            return int(float(cleaned))
        except (ValueError, TypeError):
            pass

    # Try float parsing and conversion
    try:
        return int(float(s))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Could not robustly parse integer from {val!r}") from e


def parse_float_robust(val) -> float:
    """Robustly parse a float value from a string, int, or other type,
    handling locale-specific decimal commas (like "1,5" in German)."""
    if isinstance(val, (int, float)):
        return float(val)
    if val is None:
        raise ValueError("Cannot parse None as float")

    s = str(val).strip()
    if not s:
        raise ValueError("Cannot parse empty string as float")

    try:
        return float(s)
    except (ValueError, TypeError):
        pass

    if "," in s:
        cleaned = s.replace(",", ".")
        try:
            return float(cleaned)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Could not robustly parse float from {val!r}") from e

    raise ValueError(f"Could not robustly parse float from {val!r}")


def _get_schema_type(key: str) -> str | None:
    """Return type ('int', 'float', 'boolean', 'string') for key from MODULES."""
    schema = get_config_schema(key)
    if schema:
        t = schema.get("type")
        return _normalize_schema_type(t) if t is not None else None
    return None


def _is_lru_list_config_key(key: str) -> bool:
    if key in _LRU_LIST_CONFIG_KEY_PREFIXES:
        return True
    for prefix in _LRU_LIST_CONFIG_KEY_PREFIXES:
        if key.startswith(prefix + "@"):
            return True
    return False


_resolved_config_path = None


def _resolve_config_path_from_ctx(ctx) -> str:
    """Resolve writeragent.json path from a UNO component context."""
    try:
        sm = safe_call(ctx.getServiceManager, "Get ServiceManager")
        path_settings = safe_call(sm.createInstanceWithContext, "Create PathSettings", "com.sun.star.util.PathSettings", ctx)
        user_config_path = getattr(path_settings, "UserConfig", "")
        if uno and user_config_path and str(user_config_path).startswith("file://"):
            user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
        return os.path.join(user_config_path, CONFIG_FILENAME)
    except Exception as e:
        raise ConfigError(f"Failed to resolve config path: {e}", "CONFIG_PATH_ERROR") from e


def init_config(ctx=None):
    """Resolve and cache writeragent.json path. Idempotent; call once at bootstrap."""
    global _resolved_config_path
    if _resolved_config_path is not None:
        return _resolved_config_path
    if ctx is None:
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
    if ctx is None:
        raise ConfigError("UNO context is required to resolve config path")
    _resolved_config_path = _resolve_config_path_from_ctx(ctx)
    return _resolved_config_path


def reset_config_for_tests():
    """Clear cached config path and in-memory dict (pytest isolation)."""
    global _resolved_config_path
    _resolved_config_path = None
    _invalidate_config_cache()


def _config_path():
    """Return the absolute path to writeragent.json."""
    if _resolved_config_path is not None:
        return _resolved_config_path
    return init_config()


def _emit_config_changed_ctx():
    """Return UNO ctx for config:changed listeners when on the main thread."""
    try:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework.uno_context import get_ctx

        return get_ctx() if on_main_thread() else None
    except Exception:
        return None


def user_config_dir():
    """Return LibreOffice user config directory."""
    try:
        p = _config_path()
        return os.path.dirname(p) if p else None
    except Exception as e:
        raise ConfigError(f"Failed to resolve config dir: {e}", "CONFIG_DIR_ERROR") from e


def _config_backup_path(config_file_path: str) -> str:
    return config_file_path + CONFIG_BACKUP_SUFFIX


def _backup_config_file(config_file_path: str, *, reason: str = "invalid-json") -> str | None:
    """Copy the raw config file before repair or other destructive handling."""
    if not config_file_path or not os.path.exists(config_file_path):
        return None
    backup_path = _config_backup_path(config_file_path)
    try:
        shutil.copy2(config_file_path, backup_path)
        log.warning("Backed up config %s to %s (%s)", config_file_path, backup_path, reason)
        return backup_path
    except OSError as e:
        log.error("Failed to backup config %s: %s", config_file_path, e)
        return None


def _try_parse_config_dict(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _try_repair_config_dict(text: str) -> dict | None:
    """Config-safe JSON repair: json strict=False and json_repair only (no literal_eval / LaTeX rewrite)."""
    try:
        data = json.loads(text, strict=False)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    try:
        repaired = repair_json(text)
        data = json.loads(repaired, strict=False)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return None


def _write_config_file(config_file_path: str, data: dict) -> None:
    with open(config_file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def _invalidate_config_cache() -> None:
    _cache.data = None
    _cache.mtime = 0
    _cache.mtime_last_checked = 0.0


def _load_config_dict(
    config_file_path: str,
    *,
    allow_repair: bool = False,
    persist_repair: bool = False,
) -> dict:
    """Load writeragent.json as a dict. Optionally backup, repair, and persist small JSON typos."""
    if not config_file_path or not os.path.exists(config_file_path):
        return {}

    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(
            f"Failed to read config: {e}",
            "CONFIG_READ_ERROR",
            details={"path": config_file_path},
        ) from e

    data = _try_parse_config_dict(text)
    if data is not None:
        return data

    backup_path: str | None = None
    if allow_repair:
        backup_path = _backup_config_file(config_file_path, reason="invalid-json")
        data = _try_repair_config_dict(text)
        if data is not None:
            log.info(
                "Auto-repaired invalid JSON in %s (backup: %s)",
                config_file_path,
                backup_path,
            )
            if persist_repair:
                try:
                    _write_config_file(config_file_path, data)
                    _invalidate_config_cache()
                except OSError as e:
                    raise ConfigError(
                        f"Failed to write repaired config: {e}",
                        "CONFIG_SAVE_ERROR",
                        details={"path": config_file_path, "backup_path": backup_path},
                    ) from e
            return data
        log.warning(
            "Invalid JSON in %s could not be auto-repaired (backup: %s). Using empty dict for this load.",
            config_file_path,
            backup_path or "none",
        )
        return {}

    log.warning("Invalid JSON in %s (repair disabled). Using empty dict for this load.", config_file_path)
    return {}


def is_grammar_enabled():
    """True if the grammar checker is enabled on the Doc tab (LLM, LanguageTool, Vale, or Harper)."""
    val = get_config("doc.grammar_proofreader_enabled")
    if isinstance(val, bool):
        return val  # Handle old boolean config
    val_str = str(val).strip().lower()
    return val_str in ("llm", "languagetool", "vale", "harper", "true")


def get_grammar_provider():
    """Return the active grammar provider name ('off', 'llm', 'languagetool', 'vale', or 'harper')."""
    val = get_config("doc.grammar_proofreader_enabled")
    if isinstance(val, bool):
        return "llm" if val else "off"
    val_str = str(val).strip().lower()
    if val_str == "true":
        return "llm"
    if val_str in ("llm", "languagetool", "vale", "harper"):
        return val_str

    return "off"


def get_current_endpoint():
    """Return the current endpoint URL from config, normalized (stripped)."""
    return str(get_config("endpoint") or "").strip()


# --- Config Cache ---


@dataclasses.dataclass
class ConfigCache:
    """Encapsulates the in-memory configuration cache."""

    data: Dict[str, Any] | None = None
    mtime: float = 0
    mtime_last_checked: float = 0.0


_cache = ConfigCache()


_DEFAULT_PYTHON_SCRIPTS = {
    "Prime Numbers": (
        "# Calculate primes, sharing the sieve via sp.primerange().\n"
        "low, high = sp.prime(1000), sp.prime(1010)\n\n"
        "result = {\n"
        "    \"title\": \"Prime Numbers in Range\",\n"
        "    \"primes\": [\n"
        "        {\"position\": i, \"prime\": p}\n"
        "        for i, p in zip(range(1000, 1011),\n"
        "                        list(sp.primerange(low, high + 1)))\n"
        "    ]\n"
        "}"
    ),
    "Hello WriterAgent": (
        "# A simple hello world script\n"
        "result = \"Hello from WriterAgent Python script!\""
    ),
    "Universal Sample": (
        "import writeragent as wa\n\n"
        "def run():\n"
        "    doc_type = wa.get_active_document_type()\n"
        "    print(f\"Detected active document type: {doc_type}\")\n\n"
        "    # 1. Insert Rich HTML Content\n"
        "    if doc_type == \"writer\":\n"
        "        wa.writer.apply_document_content(\n"
        "            content=[\"<h1>Hello from Python SDK</h1>\", \"<p>Here is some <b>rich HTML content</b> inserted at the end.</p>\"],\n"
        "            target=\"end\"\n"
        "        )\n"
        "    elif doc_type == \"calc\":\n"
        "        wa.calc.insert_cell_html(\n"
        "            cell_address=\"A1\",\n"
        "            html=\"<h1>Hello from Python SDK</h1><p>Here is some <b>rich HTML content</b>.</p>\"\n"
        "        )\n"
        "    else:\n"
        "        print(\"Unsupported document type for rich text insertion.\")\n\n"
        "    # 2. Insert a 24-sided Star Shape\n"
        "    # Width/height are in 100ths of a mm (e.g., 4000 = 4cm)\n"
        "    wa.shape.upsert_shape(\n"
        "        action=\"create\",\n"
        "        shape_type=\"star24\",\n"
        "        x=2000,\n"
        "        y=5000,\n"
        "        width=4000,\n"
        "        height=4000,\n"
        "        fill_color=\"blue\",\n"
        "        text=\"24-sided Star\"\n"
        "    )\n"
        "    print(\"Inserted a 24-sided blue star shape.\")\n\n"
        "if __name__ == \"__main__\":\n"
        "    run()"
    )
}


# --- WriterAgentConfig Schema ---


@dataclasses.dataclass
class WriterAgentConfig:
    """Dataclass schema for WriterAgent configuration."""

    endpoint: str = "http://localhost:11434"
    text_model: str = ""
    model: str = ""
    temperature: float = -1.0
    additional_instructions: str = ""
    chat_max_tokens: int = 16384
    request_timeout: int = 120
    stt_model: str = ""
    api_keys_by_endpoint: Dict[str, str] = dataclasses.field(default_factory=dict)
    image_base_size: int = 512
    image_default_aspect: str = "Square"
    image_steps: int = -1
    image_auto_gallery: bool = True
    image_insert_frame: bool = False
    image_model: str = ""
    # Local sentence-transformers model id (Phase A embeddings); see docs/embeddings.md.
    embedding_provider: str = "local"
    seed: str = ""
    enable_agent_log: bool = False
    # Last extension update.xml check time (unix seconds); see modules/chatbot/extension_update_check.py
    extension_update_check_epoch: float = 0.0
    is_openwebui: bool = False
    extend_selection_system_prompt: str = ""
    edit_selection_system_prompt: str = ""
    audio_support_map: Dict[str, bool] = dataclasses.field(default_factory=dict)
    calc_prompt_max_tokens: int = 70
    # When True, treat endpoint as OpenRouter (e.g. custom proxy) even if the URL lacks openrouter.ai.
    is_openrouter: bool = False
    # When True, the Chat Completions request includes parallel_tool_calls: True to allow multiple tool calls.
    parallel_tool_calls: bool = True
    # Merged into POST \u2026/chat/completions JSON when OpenRouter is active; see AGENTS.md.
    openrouter_chat_extra: Dict[str, Any] = dataclasses.field(default_factory=dict)
    last_python_script_name_writer: str = "Prime Numbers"
    last_python_script_name_calc: str = "Prime Numbers"
    last_python_script_name_draw: str = "Prime Numbers"

    # Text analytics (sentiment etc.) — see plugin/scripting/text_analytics.py.
    # engine is "transformers" for now (good multilingual default); model can be overridden
    # via JSON for a different HF model or future engines.
    text_analytics_sentiment_engine: str = "transformers"
    text_analytics_sentiment_model: str = "cardiffnlp/twitter-xlm-roberta-base-sentiment"

    # Persists the last entries for inserting LaTeX math
    last_latex_input: str = r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"
    last_latex_display_block: bool = False

    # Persists multiple user-saved Python scripts (name -> code)
    saved_python_scripts: Dict[str, str] = dataclasses.field(
        default_factory=lambda: dict(_DEFAULT_PYTHON_SCRIPTS)
    )

    # Store arbitrary module.yaml config entries
    _extra_config: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def validate(self):
        """Perform validation of config keys and emit warnings or fix values."""
        # Clean up any translated headers that incorrectly made it into config
        for f in dataclasses.fields(self):
            if f.name == "_extra_config":
                continue
            val = getattr(self, f.name)
            if isinstance(val, str) and "Project-Id-Version:" in val:
                log.debug("config validate: stripped PO/header from dataclass field %r (len=%s)", f.name, len(val))
                # Default seed should be -1, not empty string.
                if f.name == "seed":
                    setattr(self, f.name, "-1")
                else:
                    setattr(self, f.name, "")

        # Cast standard fields through the central schema validator so dialog
        # controllers do not need to duplicate config type rules.
        for f in dataclasses.fields(self):
            if f.name == "_extra_config":
                continue
            val = getattr(self, f.name)
            setattr(self, f.name, coerce_config_value(f.name, val))

        # Clean up and cast extra keys from module schemas robustly.
        for k, v in list(self._extra_config.items()):
            if isinstance(v, str) and "Project-Id-Version:" in v:
                log.debug("config validate: stripped PO/header from extra key %r (len=%s)", k, len(v))
                self._extra_config[k] = ""
                v = ""
            self._extra_config[k] = coerce_config_value(k, v)

        endpoint_str = str(self.endpoint or "").strip()
        if endpoint_str:
            try:
                from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
                self.endpoint = endpoint_from_selector_text(endpoint_str)
            except Exception:
                self.endpoint = normalize_endpoint_url(endpoint_str, is_openwebui=self.is_openwebui)
        else:
            self.endpoint = ""

        if not isinstance(self.chat_max_tokens, int):
            try:
                self.chat_max_tokens = parse_int_robust(self.chat_max_tokens)
            except ValueError:
                self.chat_max_tokens = 16384
        if self.chat_max_tokens < 0:
            raise ConfigValidationError(_("Chat max tokens must be >= 0"), code="INVALID_CHAT_MAX_TOKENS")

        if not isinstance(self.request_timeout, int):
            try:
                self.request_timeout = parse_int_robust(self.request_timeout)
            except ValueError:
                self.request_timeout = 120
        if self.request_timeout <= 0:
            raise ConfigValidationError(_("Request timeout must be > 0"), code="INVALID_REQUEST_TIMEOUT")

        if not isinstance(self.temperature, (int, float)):
            try:
                self.temperature = parse_float_robust(self.temperature)
            except ValueError:
                self.temperature = -1.0
        if self.temperature > 1.0:
            raise ConfigValidationError(_("Temperature must be <= 1.0"), code="INVALID_TEMPERATURE")

        if not isinstance(self.openrouter_chat_extra, dict):
            log.warning("Invalid openrouter_chat_extra (not a dict), resetting to {}")
            self.openrouter_chat_extra = {}

        if isinstance(self.saved_python_scripts, dict) and "Sample" in self.saved_python_scripts:
            del self.saved_python_scripts["Sample"]

        if not isinstance(self.saved_python_scripts, dict):
            self.saved_python_scripts = {}
        if "Universal Sample" not in self.saved_python_scripts:
            self.saved_python_scripts["Universal Sample"] = _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]

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


_MISSING_VALUE = object()


def _normalize_schema_type(schema_type: Any) -> str | None:
    if schema_type is None:
        return None
    t = str(schema_type).strip().lower()
    if t == "bool":
        return "boolean"
    return t


def _dataclass_field_default(field: dataclasses.Field) -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:  # type: ignore[attr-defined]
        return field.default_factory()  # type: ignore[misc]
    return None


def _dataclass_field_type(field: dataclasses.Field) -> str | None:
    if field.type is int:
        return "int"
    if field.type is float:
        return "float"
    if field.type is bool:
        return "boolean"
    if field.type is str:
        return "string"
    if field.type is list or isinstance(_dataclass_field_default(field), list):
        return "list"
    if field.type is dict or isinstance(_dataclass_field_default(field), dict):
        return "dict"
    return None


def _module_schema_for_key(key: str) -> dict[str, Any] | None:
    if not MODULES:
        return None
    if "." in key:
        mod_name, field_name = key.split(".", 1)
        for module in MODULES:
            if not isinstance(module, dict) or module.get("name") != mod_name:
                continue
            config = module.get("config", {})
            if isinstance(config, dict):
                schema = config.get(field_name)
                if isinstance(schema, dict):
                    return dict(schema)
        return None

    for module in MODULES:
        if not isinstance(module, dict):
            continue
        config = module.get("config", {})
        if isinstance(config, dict):
            schema = config.get(key)
            if isinstance(schema, dict):
                return dict(schema)
    return None


def _dataclass_schema_for_key(key: str) -> dict[str, Any] | None:
    safe_key = key.replace(".", "_")
    for field in dataclasses.fields(WriterAgentConfig):
        if field.name == "_extra_config" or field.name != safe_key:
            continue
        schema: dict[str, Any] = {"default": _dataclass_field_default(field)}
        field_type = _dataclass_field_type(field)
        if field_type:
            schema["type"] = field_type
        return schema
    return None


def get_config_schema(key: str) -> dict[str, Any] | None:
    """Return the config schema for a flat or dotted key.

    Module schemas come from ``module.yaml`` via the manifest and take
    precedence over dataclass defaults, matching ``_resolve_default``.
    """
    return _module_schema_for_key(key) or _dataclass_schema_for_key(key)


def _schema_default_from_schema(schema: dict[str, Any] | None) -> Any:
    if schema and "default" in schema:
        return schema["default"]
    return _MISSING_VALUE


def _fallback_value_for_invalid(key: str, schema: dict[str, Any] | None, fallback_value: Any) -> Any:
    if fallback_value is not _MISSING_VALUE:
        return coerce_config_value(key, fallback_value)
    default_val = _schema_default_from_schema(schema)
    if default_val is not _MISSING_VALUE:
        return default_val
    return _MISSING_VALUE


def _canonicalize_schema_option_value(schema: dict[str, Any] | None, value: Any) -> Any:
    opts = schema.get("options") if schema else None
    if not isinstance(opts, list):
        return value
    value_str = str(value)
    for opt in opts:
        if isinstance(opt, dict):
            opt_value = opt.get("value", opt.get("label", ""))
            opt_label = opt.get("label", opt_value)
            candidates = {str(opt_value), str(opt_label), str(_(str(opt_label)))}
            if value_str in candidates:
                return opt_value
        elif opt is not None and value_str in {str(opt), str(_(str(opt)))}:
            return opt
    return value


def clamp_schema_value(key: str, value: Any) -> Any:
    """Apply module/dataclass schema min/max bounds to an already coerced value."""
    schema = get_config_schema(key)
    if not schema or ("min" not in schema and "max" not in schema):
        return value
    schema_type = _normalize_schema_type(schema.get("type"))
    if schema_type not in {"int", "float"}:
        return value
    try:
        numeric_value = parse_float_robust(value)
        if "min" in schema:
            numeric_value = max(parse_float_robust(schema["min"]), numeric_value)
        if "max" in schema:
            numeric_value = min(parse_float_robust(schema["max"]), numeric_value)
    except ValueError:
        return value
    if schema_type == "int":
        return int(numeric_value)
    return numeric_value


def coerce_config_value(key: str, value: Any, *, fallback_value: Any = _MISSING_VALUE) -> Any:
    """Coerce a config value according to its schema and canonicalize options.

    Invalid numeric/list values use ``fallback_value`` when supplied (used by
    ``set_config`` to preserve the previous saved value), otherwise the schema
    default. Unknown keys are returned unchanged.
    """
    schema = get_config_schema(key)
    if not schema:
        return value

    value = _canonicalize_schema_option_value(schema, value)
    schema_type = _normalize_schema_type(schema.get("type"))

    if schema_type == "int":
        try:
            value = parse_int_robust(value)
        except ValueError:
            fallback = _fallback_value_for_invalid(key, schema, fallback_value)
            return fallback if fallback is not _MISSING_VALUE else value
    elif schema_type == "float":
        try:
            value = parse_float_robust(value)
        except ValueError:
            fallback = _fallback_value_for_invalid(key, schema, fallback_value)
            return fallback if fallback is not _MISSING_VALUE else value
    elif schema_type == "boolean":
        value = as_bool(value)
    elif schema_type == "list":
        if isinstance(value, list):
            pass
        elif isinstance(value, str) and value.strip():
            value = [value.strip()]
        else:
            fallback = _fallback_value_for_invalid(key, schema, fallback_value)
            if fallback is not _MISSING_VALUE:
                value = fallback if isinstance(fallback, list) else [fallback]
            else:
                value = []
    elif schema_type == "string":
        if value is None:
            fallback = _fallback_value_for_invalid(key, schema, fallback_value)
            value = fallback if fallback is not _MISSING_VALUE else ""
        else:
            value = str(value)

    return clamp_schema_value(key, value)


# --- Core config I/O ---


def get_config(key):
    """Get a config value by key. JSON overrides; when key is missing, use schema default then central fallback."""
    config_data = _get_validated_config_dict()
    if not isinstance(config_data, dict):
        config_data = {}

    if key in config_data:
        return config_data[key]

    for dotted in _dotted_fallback_keys(key):
        if dotted in config_data:
            return config_data[dotted]

    return _resolve_default(key)


def get_config_int(key) -> int:
    """Get a config value as int. All requested keys MUST be in the schema (WriterAgentConfig or MODULES).
    Throws ConfigError if the key is missing or invalid."""
    v = get_config(key)
    # Empty string or None from JSON/UI: use schema default (same as missing key).
    if v == "" or v is None:
        v = _resolve_default(key)
    # _resolve_default returns "" for unknown keys that slip through without a dataclass default.
    if v == "":
        raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", "CONFIG_KEY_NOT_FOUND", details={"key": key})
    try:
        return parse_int_robust(v)
    except ValueError as e:
        raise ConfigError(f"Config key {key!r} has non-integer value: {v!r}", "CONFIG_TYPE_ERROR") from e


def get_config_str(key) -> str:
    """Get a config value as str. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found."""
    v = get_config(key)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def get_config_bool(key) -> bool:
    """Get a config value as bool. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found."""
    v = get_config(key)
    return as_bool(v)


def get_config_bool_safe(key: str) -> bool:
    """Safely read a boolean config value, returning schema default on failure."""
    try:
        return get_config_bool(key)
    except Exception:
        try:
            return as_bool(_resolve_default(key))
        except Exception:
            return False


def get_config_int_safe(key: str) -> int:
    """Safely read an integer config value, returning schema default on failure."""
    try:
        return get_config_int(key)
    except Exception:
        try:
            return parse_int_robust(_resolve_default(key))
        except Exception:
            return 0


def get_config_float_safe(key: str) -> float:
    """Safely read a float config value, returning schema default on failure."""
    try:
        return get_config_float(key)
    except Exception:
        try:
            return parse_float_robust(_resolve_default(key))
        except Exception:
            return 0.0


def get_config_float(key) -> float:
    """Get a config value as float. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found."""
    v = get_config(key)
    try:
        return parse_float_robust(v)
    except ValueError as e:
        raise ConfigError(f"Config key {key!r} has non-float value: {v!r}", "CONFIG_TYPE_ERROR") from e


def get_config_dict():
    """Return the full config as a dict. Returns {} if missing or on error."""
    return _get_validated_config_dict()


def _raw_config_value_for_key(config_data: dict[str, Any], key: str) -> Any:
    if key in config_data:
        return config_data[key]
    for dotted in _dotted_fallback_keys(key):
        if dotted in config_data:
            return config_data[dotted]
    if "." in key:
        field_name = key.split(".", 1)[1]
        if field_name in config_data:
            return config_data[field_name]
    return _MISSING_VALUE


def set_config(key, value):
    """Set a config key to value. Creates file if needed."""
    try:
        config_file_path = _config_path()
    except ConfigError:
        return

    if not config_file_path:
        return
    if os.path.exists(config_file_path):
        config_data = _load_config_dict(config_file_path, allow_repair=True, persist_repair=False)
    else:
        config_data = {}
    current_value = _raw_config_value_for_key(config_data, key)
    value = coerce_config_value(key, value, fallback_value=current_value)
    if config_data.get(key) == value:
        return
    test_data = dict(config_data)
    test_data[key] = value
    try:
        test_config = WriterAgentConfig.from_dict(test_data)
        test_config.validate()
        config_data = test_config.to_dict()
    except ConfigValidationError as e:
        raise e
    except Exception as e:
        log.exception("Validation error in set_config")
        raise ConfigValidationError(f"Invalid configuration value for {key}: {e}") from e

    try:
        _write_config_file(config_file_path, config_data)

        _invalidate_config_cache()

        global_event_bus.emit("config:changed", ctx=_emit_config_changed_ctx())

    except OSError as e:
        log.error("Error writing to %s: %s", config_file_path, e)
        raise ConfigError(f"Failed to save config: {e}", "CONFIG_SAVE_ERROR") from e


def remove_config(key):
    """Remove a config key."""
    try:
        config_file_path = _config_path()
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
        _write_config_file(config_file_path, config_data)

        _invalidate_config_cache()

        global_event_bus.emit("config:changed", ctx=_emit_config_changed_ctx())

    except OSError as e:
        log.error("Error writing to %s: %s", config_file_path, e)
        raise ConfigError(f"Failed to remove config key: {e}", "CONFIG_SAVE_ERROR") from e


# --- MODULES / manifest schema ---


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


# --- Default resolution ---


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


# --- Validated JSON cache ---


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


def _get_validated_config_dict():
    """Return the full validated config as a dict, using an in-memory cache
    keyed off the file modification time."""
    try:
        config_file_path = _config_path()
    except ConfigError:
        return {}

    if not config_file_path or not os.path.exists(config_file_path):
        return {}

    current_time = time.time()

    # 2-second cache for the mtime check
    if _cache.data is not None and (current_time - _cache.mtime_last_checked) < 2.0:
        return _cache.data

    try:
        current_mtime = os.path.getmtime(config_file_path)
    except OSError:
        current_mtime = 0

    _cache.mtime_last_checked = current_time

    if _cache.data is not None and current_mtime == _cache.mtime and current_mtime != 0:
        return _cache.data

    try:
        data = _load_config_dict(config_file_path, allow_repair=True, persist_repair=True)

        if not isinstance(data, dict):
            raise ConfigError("Config must be a JSON object", "CONFIG_INVALID_FORMAT")

        try:
            current_mtime = os.path.getmtime(config_file_path)
        except OSError:
            current_mtime = 0

        # Perform validation when config is loaded
        config = WriterAgentConfig.from_dict(data)
        config.validate()

        out = _build_validated_config_export(data, config)

        _cache.data = out
        _cache.mtime = current_mtime
        return out
    except ConfigError as e:
        log.error("Config error reading %s: %s", config_file_path, e)
        return {}
    except OSError as e:
        log.error("Error reading %s: %s", config_file_path, e)
        return {}


# --- Per-endpoint API keys ---


def get_api_key_for_endpoint(endpoint):
    """Return API key for the given endpoint."""
    data = get_config("api_keys_by_endpoint")
    if not isinstance(data, dict):
        data = {}
    normalized = normalize_endpoint_url(endpoint or "")
    return data.get(normalized) or ""


def set_api_key_for_endpoint(endpoint, key):
    """Store API key for the given endpoint in api_keys_by_endpoint."""
    data = get_config("api_keys_by_endpoint")
    if not isinstance(data, dict):
        data = {}
    normalized = normalize_endpoint_url(endpoint or "")
    data[normalized] = str(key)
    set_config("api_keys_by_endpoint", data)


# --- Bundled API config ---


def get_api_config():
    """Build API config dict for LlmClient. Pass to LlmClient(config, ctx)."""
    from plugin.framework.client.model_fetcher import get_text_model

    endpoint = str(get_config("endpoint") or "").rstrip("/")
    is_openwebui = as_bool(get_config("is_openwebui")) or "open-webui" in endpoint.lower() or "openwebui" in endpoint.lower()

    # Local import to avoid circular import during early UNO registration
    # (config → client/provider_detection → client/__init__ → llm_client → logging → config)
    from plugin.framework.client.provider_detection import is_openrouter_endpoint

    # Use the consolidated detection helper (2026 provider heuristic cleanup)
    # so the OpenRouter decision is identical everywhere (auth, model fetcher,
    # error messages, LLM client, etc.).
    is_openrouter = is_openrouter_endpoint(endpoint, explicit_is_openrouter=as_bool(get_config("is_openrouter")))
    api_key = get_api_key_for_endpoint(endpoint)

    api_config = {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": get_text_model(),
        "is_openwebui": is_openwebui,
        "is_openrouter": is_openrouter,
        "seed": get_config_str("seed"),
        "request_timeout": get_config_int("request_timeout"),
        "chat_max_tool_rounds": get_config_int("chatbot.max_tool_rounds"),
    }

    temp = get_config_float("temperature")
    if temp >= 0:
        api_config["temperature"] = temp

    if is_openrouter:
        ore = get_config("openrouter_chat_extra")
        if isinstance(ore, dict) and ore:
            api_config["openrouter_chat_extra"] = ore

    return api_config


def validate_api_config(config):
    """Validate API config dict (from get_api_config). Returns (ok: bool, error_message: str)."""
    from plugin.framework.i18n import _

    endpoint = (config.get("endpoint") or "").strip()
    if not endpoint:
        return (False, _("Please set Endpoint in Settings."))
    model = (config.get("model") or "").strip()
    if not model:
        return (False, _("Please set Model in Settings."))
    from plugin.chatbot.config_ui_helpers import _is_model_combobox_placeholder

    if _is_model_combobox_placeholder(model):
        return (False, _("Please select a valid model in Settings (not a placeholder)."))
    return (True, "")

