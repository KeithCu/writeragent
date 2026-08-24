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
"""Pure config schema and coercion for WriterAgent.

No disk I/O, cache, event bus, ``get_ctx``, or ``init_config``. Import
schema/coercion names from this module. I/O stays on ``plugin.framework.config``.
This file must not import ``config``, chatbot, calc, or uno_context.

Manifest tables (``MODULES``, ``CONFIG_DEFAULTS``, ``CONFIG_SCHEMAS``,
``DOTTED_FALLBACKS``) live here because they are in-memory schema, not
``writeragent.json`` I/O. ``set_manifest_modules`` rebuilds those tables.
"""

# crosshair: off
import dataclasses
import logging
import os
from typing import Any, Dict

from plugin.framework.deal_shim import deal
from plugin.framework.errors import ConfigError, ConfigValidationError
from plugin.framework.i18n import _
from plugin.framework.url_utils import normalize_endpoint_url

log = logging.getLogger(__name__)

# Same path as ``constants.get_plugin_dir`` (plugin/), used only for the
# log_level DEBUG-vs-WARN default when a source checkout has plugin/tests.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from plugin._manifest import (
        MODULES as _DEFAULT_MODULES,
        CONFIG_DEFAULTS as _DEFAULT_CONFIG_DEFAULTS,
        CONFIG_SCHEMAS as _DEFAULT_CONFIG_SCHEMAS,
        DOTTED_FALLBACKS as _DEFAULT_DOTTED_FALLBACKS,
    )
except ImportError:
    _DEFAULT_MODULES = []
    _DEFAULT_CONFIG_DEFAULTS = {}
    _DEFAULT_CONFIG_SCHEMAS = {}
    _DEFAULT_DOTTED_FALLBACKS = {}

MODULES: list[dict[str, Any]] = _DEFAULT_MODULES  # type: ignore[assignment]
CONFIG_DEFAULTS: dict[str, Any] = _DEFAULT_CONFIG_DEFAULTS
CONFIG_SCHEMAS: dict[str, Any] = _DEFAULT_CONFIG_SCHEMAS
DOTTED_FALLBACKS: dict[str, list[str]] = _DEFAULT_DOTTED_FALLBACKS


def set_manifest_modules(modules: list[dict[str, Any]]) -> None:
    """Set manifest modules list and rebuild fast defaults/schemas lookup dictionaries."""
    global MODULES, CONFIG_DEFAULTS, CONFIG_SCHEMAS, DOTTED_FALLBACKS
    MODULES = modules or []
    defaults: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    fallbacks: dict[str, list[str]] = {}
    for m in MODULES:
        mod_name = m.get("name", "")
        config = m.get("config", {})
        if isinstance(config, dict) and mod_name:
            for fname, schema in config.items():
                if isinstance(schema, dict):
                    full_key = f"{mod_name}.{fname}"
                    if "default" in schema:
                        defaults[full_key] = schema["default"]
                        if fname not in defaults:
                            defaults[fname] = schema["default"]
                    schemas[full_key] = schema
                    if fname not in schemas:
                        schemas[fname] = schema
                    fallbacks.setdefault(fname, []).append(full_key)
    CONFIG_DEFAULTS = defaults
    CONFIG_SCHEMAS = schemas
    DOTTED_FALLBACKS = fallbacks


def get_manifest_modules() -> list[dict[str, Any]]:
    """Return active manifest modules list."""
    return MODULES


# Keys used by populate_combobox_with_lru / update_lru_history (including endpoint-scoped "name@url").
_LRU_LIST_CONFIG_KEY_PREFIXES: frozenset[str] = frozenset({"model_lru", "prompt_lru", "image_model_lru", "audio_model_lru", "endpoint_lru", "image_base_size_lru"})


@deal.post(lambda result: isinstance(result, bool))
def as_bool(value):
    """Parse a value as boolean (handles str, int, float)."""
    import sys

    if "crosshair" in sys.modules:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return False

    if type(value) is bool:
        return value
    if type(value) is str:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if type(value) in (int, float):
        return value != 0
    return False


@deal.post(lambda result: isinstance(result, int))
@deal.raises(ValueError)
def parse_int_robust(val) -> int:
    """Robustly parse an integer value from a string, float, or other type,
    handling locale-specific decimal commas (like "8765,0" in German)."""
    import math
    import sys

    if isinstance(val, bool):
        # bool is a subclass of int; keep explicit for clarity under CrossHair.
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # int(inf) raises OverflowError; map non-finite to ValueError for @deal.raises.
        if not math.isfinite(val):
            raise ValueError(f"Cannot parse non-finite float as int: {val!r}")
        return int(val)
    if val is None:
        raise ValueError("Cannot parse None as int")

    if "crosshair" in sys.modules:
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            if not math.isfinite(val):
                raise ValueError(f"Cannot parse non-finite float as int: {val!r}")
            return int(val)
        raise ValueError("Cannot parse symbolic type as int under CrossHair")

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
            f = float(cleaned)
            if not math.isfinite(f):
                raise ValueError(f"Cannot parse non-finite float as int: {val!r}")
            return int(f)
        except (ValueError, TypeError, OverflowError):
            pass

    # Try float parsing and conversion
    try:
        f = float(s)
        if not math.isfinite(f):
            raise ValueError(f"Cannot parse non-finite float as int: {val!r}")
        return int(f)
    except (ValueError, TypeError, OverflowError) as e:
        raise ValueError(f"Could not robustly parse integer from {val!r}") from e


@deal.post(lambda result: isinstance(result, float))
@deal.raises(ValueError)
def parse_float_robust(val) -> float:
    """Robustly parse a float value from a string, int, or other type,
    handling locale-specific decimal commas (like "1,5" in German)."""
    if isinstance(val, (int, float)):
        return float(val)
    if val is None:
        raise ValueError("Cannot parse None as float")

    import sys

    if "crosshair" in sys.modules:
        if isinstance(val, (int, float)):
            return float(val)
        raise ValueError("Cannot parse symbolic type as float under CrossHair")

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


def _is_lru_list_config_key(key: str) -> bool:
    if key in _LRU_LIST_CONFIG_KEY_PREFIXES:
        return True
    for prefix in _LRU_LIST_CONFIG_KEY_PREFIXES:
        if key.startswith(prefix + "@"):
            return True
    return False

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
        "    wa.shape.upsert(\n"
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


# Optional overlay from config.py (Settings combobox labels). Typed Any so
# assigning a different function does not trip the type checker.
_endpoint_normalize_impl: Any = None


def _normalize_configured_endpoint(endpoint_str: str, is_openwebui: bool) -> str:
    """Normalize a stored endpoint URL.

    ``config.py`` sets ``_endpoint_normalize_impl`` to parse Settings combobox
    labels when chatbot is present. This default path is the LibrePy fallback.
    """
    impl = _endpoint_normalize_impl
    if impl is not None:
        return impl(endpoint_str, is_openwebui)
    return normalize_endpoint_url(endpoint_str, is_openwebui=is_openwebui)


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
    # Last extension update.xml check time (unix seconds); see plugin/chatbot/extension_update_check.py
    # Per-product keys so WriterAgent + LibreHarper dual-install do not suppress each other.
    extension_update_check_epoch: float = 0.0
    librepy_update_check_epoch: float = 0.0
    libreharper_update_check_epoch: float = 0.0
    is_openwebui: bool = False
    extend_selection_system_prompt: str = ""
    edit_selection_system_prompt: str = ""
    audio_support_map: Dict[str, bool] = dataclasses.field(default_factory=dict)
    calc_prompt_max_tokens: int = 4096
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
            # WriterAgent overlays selector-label parsing in config.py; LibrePy
            # keeps this url_utils fallback (no chatbot import in this module).
            self.endpoint = _normalize_configured_endpoint(endpoint_str, self.is_openwebui)
        else:
            self.endpoint = ""

        if not isinstance(self.chat_max_tokens, int):
            try:
                self.chat_max_tokens = parse_int_robust(self.chat_max_tokens)
            except ValueError:
                self.chat_max_tokens = 16384
        if self.chat_max_tokens < 0:
            raise ConfigValidationError(_("Chat max tokens must be >= 0"), code="INVALID_CHAT_MAX_TOKENS")

        # Old shipped default was 70; values below 100 are treated as stale and upgraded.
        if not isinstance(self.calc_prompt_max_tokens, int):
            try:
                self.calc_prompt_max_tokens = parse_int_robust(self.calc_prompt_max_tokens)
            except ValueError:
                self.calc_prompt_max_tokens = 4096
        if self.calc_prompt_max_tokens < 100:
            log.info("Upgrading calc_prompt_max_tokens from %s to 4096", self.calc_prompt_max_tokens)
            self.calc_prompt_max_tokens = 4096

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

    def to_dict(self, omit_defaults: bool = True) -> Dict[str, Any]:
        """Convert back to dictionary, expanding _extra_config.

        When omit_defaults is True (default), fields matching ``_resolve_default``
        (schema, then dataclass, including log_level DEBUG/WARN) are excluded so
        defaults are not written to the JSON config file. Extra keys with no
        schema / dataclass / LRU default are dropped (retired or unknown).
        """
        out: Dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name == "_extra_config":
                continue
            val = getattr(self, f.name)
            if omit_defaults and is_default_value(f.name, val):
                continue
            out[f.name] = val

        for k, v in self._extra_config.items():
            if not is_known_config_key(k):
                continue
            if omit_defaults and is_default_value(k, v):
                continue
            out[k] = v

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
    if MODULES is _DEFAULT_MODULES:
        if key in CONFIG_SCHEMAS:
            return dict(CONFIG_SCHEMAS[key])
        for dotted in _dotted_fallback_keys(key):
            if dotted in CONFIG_SCHEMAS:
                return dict(CONFIG_SCHEMAS[dotted])
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


# --- MODULES / manifest schema ---


def _get_schema_default(key):
    """Return default for key from manifest schema. Supports flat and dotted keys."""
    if MODULES is _DEFAULT_MODULES:
        if key in CONFIG_DEFAULTS:
            return CONFIG_DEFAULTS[key]
        for dotted in _dotted_fallback_keys(key):
            if dotted in CONFIG_DEFAULTS:
                return CONFIG_DEFAULTS[dotted]
        return None

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
    for m in MODULES:
        config = m.get("config", {})
        if isinstance(config, dict):
            for fname, schema in config.items():
                if fname == key and isinstance(schema, dict) and "default" in schema:
                    return schema["default"]
    return None


def _dotted_fallback_keys(key):
    """Yield dotted key variants for key using manifest modules (e.g. extend_selection_max_tokens -> chatbot.extend_selection_max_tokens)."""
    if "." in key:
        return
    if MODULES is _DEFAULT_MODULES and key in DOTTED_FALLBACKS:
        for dotted in DOTTED_FALLBACKS[key]:
            yield dotted
        return
    for m in MODULES:
        mod_name = m.get("name", "")
        if not mod_name:
            continue
        config = m.get("config", {})
        if isinstance(config, dict) and key in config:
            yield f"{mod_name}.{key}"


# --- Default resolution ---


def _resolve_default(key):
    """Resolve default for key: schema first, then dataclass. Safe fallbacks for None."""
    if key == "log_level":
        tests_dir = os.path.join(_PLUGIN_DIR, "tests")
        return "DEBUG" if os.path.isdir(tests_dir) else "WARN"

    val = _get_schema_default(key)
    if val is not None:
        return val

    if _is_lru_list_config_key(key):
        return []

    safe_key = key.replace(".", "_")
    for f in dataclasses.fields(WriterAgentConfig):
        if f.name == safe_key:
            return _dataclass_field_default(f)

    # Strict check: if not in schema and not a recognized dynamic pattern, it's a bug.
    raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", "CONFIG_KEY_NOT_FOUND", details={"key": key})


def _is_equal_to_default(key: str, value: Any, default_val: Any) -> bool:
    """Return True if `value` equals `default_val`."""
    if default_val is None:
        return value is None

    if isinstance(default_val, bool):
        return as_bool(value) is default_val

    if isinstance(default_val, (int, float)) and not isinstance(default_val, bool):
        if isinstance(value, bool):
            return False
        try:
            return parse_float_robust(value) == parse_float_robust(default_val)
        except (ValueError, TypeError):
            return False

    if isinstance(default_val, (dict, list)):
        return type(value) is type(default_val) and value == default_val

    if key == "endpoint":
        norm_val = normalize_endpoint_url(str(value or "").strip())
        norm_def = normalize_endpoint_url(str(default_val or "").strip())
        return norm_val == norm_def

    return str(value or "") == str(default_val or "")


def is_known_config_key(key: str) -> bool:
    """True if `key` has a schema, dataclass, or LRU default."""
    try:
        _resolve_default(key)
    except ConfigError:
        return False
    except Exception:
        return False
    return True


def is_default_value(key: str, value: Any) -> bool:
    """Return True if `value` matches the default configuration value for `key`."""
    try:
        default_val = _resolve_default(key)
    except ConfigError:
        return False
    except Exception:
        return False
    return _is_equal_to_default(key, value, default_val)


def prune_default_values(data: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys and values that match schema/dataclass defaults."""
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if is_known_config_key(k) and not is_default_value(k, v)
    }
