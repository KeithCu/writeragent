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
``writeragent.json`` I/O. ``MODULES`` from ``plugin._manifest`` is the source
of truth; ``set_manifest_modules`` rebuilds the derived tables at import.

Dataclass fields may declare ``min``, ``max``, and ``min_exclusive`` in
``field.metadata``. ``get_config_schema`` copies those onto the schema so
``coerce_config_value(..., strict=True)`` rejects the same out-of-range
numbers ``WriterAgentConfig.validate`` rejects. Validate still runs that
check itself, before coercion: a load repair uses the field fallback
(``chat_max_tokens`` -1 becomes 16384, not the inclusive minimum 0).
"""

# crosshair: off
from __future__ import annotations

import dataclasses
import logging
import os
import textwrap
import typing
from typing import Any, Callable, Dict

from plugin.framework.constants import get_plugin_dir
from plugin.framework.deal_shim import deal
from plugin.framework.errors import ConfigError, ConfigValidationError
from plugin.framework.i18n import _
from plugin.framework.url_utils import normalize_endpoint_url

log = logging.getLogger(__name__)

# Import MODULES only. Generated ``_manifest.py`` always has MODULES (and
# VERSION); CONFIG_DEFAULTS / CONFIG_SCHEMAS / DOTTED_FALLBACKS may be absent.
# Importing those names together was an all-or-nothing trap: one missing
# name raised ImportError, MODULES stayed [], and module.yaml keys never
# reached ``CONFIG_DEFAULTS`` / ``get_config``. Empty fallback is only
# for LibrePy-style trees that omit ``_manifest`` itself.
try:
    from plugin._manifest import MODULES as _imported_modules
except ImportError:
    _imported_modules = []

MODULES: list[dict[str, Any]] = []
CONFIG_DEFAULTS: dict[str, Any] = {}
CONFIG_SCHEMAS: dict[str, Any] = {}
FLAT_TO_MODULE_KEY: dict[str, str] = {}


def set_manifest_modules(modules: list[dict[str, Any]]) -> None:
    """Set manifest modules list and rebuild fast defaults/schemas lookup dictionaries."""
    global MODULES, CONFIG_DEFAULTS, CONFIG_SCHEMAS, FLAT_TO_MODULE_KEY
    MODULES = modules or []
    defaults: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    flat_to_mod: dict[str, str] = {"doc_agent_edit_review_mode": "doc.agent_edit_review_mode"}
    for m in MODULES:
        mod_name = m.get("name", "")
        config = m.get("config", {})
        if isinstance(config, dict) and mod_name:
            for fname, schema in config.items():
                if isinstance(schema, dict):
                    full_key = f"{mod_name}.{fname}"
                    if "default" in schema:
                        defaults[full_key] = schema["default"]
                    schemas[full_key] = schema
                    flat_to_mod[fname] = full_key
    CONFIG_DEFAULTS = defaults
    CONFIG_SCHEMAS = schemas
    FLAT_TO_MODULE_KEY = flat_to_mod


# Bind derived tables from MODULES at import so callers do not need
# ``set_manifest_modules`` (almost nobody called it).
set_manifest_modules(_imported_modules)  # type: ignore[arg-type]


# Keys used by populate_combobox_with_lru / update_lru_history (including endpoint-scoped "name@url").
_LRU_LIST_CONFIG_KEY_PREFIXES: frozenset[str] = frozenset({"model_lru", "prompt_lru", "image_model_lru", "audio_model_lru", "tts_model_lru", "endpoint_lru", "image_base_size_lru", "slash_command_lru"})


@deal.post(lambda result: isinstance(result, bool))
def as_bool(value: Any) -> bool:
    """Parse a value as boolean (handles str, int, float)."""
    if type(value) is bool:
        return value
    if type(value) is str:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if type(value) in (int, float):
        return value != 0
    return False


def _signed_digits(token: str) -> bool:
    body = token[1:] if token[:1] in "+-" else token
    return bool(body) and body.isdigit()


def _normalize_comma_number(s: str) -> str | None:
    """One comma: three-digit suffix is thousands; any other suffix is a decimal comma.

    Returns None when there is not exactly one comma (two commas must not be
    rewritten into a truncated float).
    """
    if s.count(",") != 1:
        return None
    head, tail = s.split(",", 1)
    if len(tail) == 3 and tail.isdigit() and _signed_digits(head):
        return head + tail
    return head + "." + tail


@deal.raises(ValueError)
def parse_int_robust(val: Any) -> int:
    """Robustly parse an integer value from a string, float, or other type,
    handling locale-specific decimal commas (like "8765,0" in German)."""
    import math

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

    s = str(val).strip()
    if not s:
        raise ValueError("Cannot parse empty string as int")

    # Try normal int parsing first
    try:
        return int(s)
    except (ValueError, TypeError):
        pass

    # A single comma is a thousands group only when the suffix is exactly
    # three digits ("1,234"). Otherwise it is a decimal comma ("8765,0",
    # "1,5"). Turning every comma into a dot parses "1,234" as 1.234, then 1.
    normalized = _normalize_comma_number(s)
    if normalized is not None:
        try:
            if "." in normalized:
                f = float(normalized)
                if not math.isfinite(f):
                    raise ValueError(f"Cannot parse non-finite float as int: {val!r}")
                return int(f)
            return int(normalized)
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


def _float_or_value_error(val: Any) -> float:
    """``float(val)``, mapping overflow to ``ValueError``.

    JSON accepts an integer of 309+ digits. CPython's ``float()`` then raises
    ``OverflowError`` past the IEEE range, not ``ValueError``. Callers treat
    ``ValueError`` as "not a usable number", so map the overflow or loading
    ``writeragent.json`` crashes instead of degrading.
    """
    try:
        return float(val)
    except OverflowError as exc:
        raise ValueError(f"Could not robustly parse float from {val!r}") from exc


@deal.post(lambda result: isinstance(result, float))
@deal.raises(ValueError)
def parse_float_robust(val: Any) -> float:
    """Robustly parse a float value from a string, int, or other type,
    handling locale-specific decimal commas (like "1,5" in German)."""
    if isinstance(val, (int, float)):
        return _float_or_value_error(val)
    if val is None:
        raise ValueError("Cannot parse None as float")

    s = str(val).strip()
    if not s:
        raise ValueError("Cannot parse empty string as float")

    try:
        return _float_or_value_error(s)
    except (ValueError, TypeError):
        pass

    # Same comma rule as parse_int_robust: "1,234" is 1234, "1,5" is 1.5.
    normalized = _normalize_comma_number(s)
    if normalized is not None:
        try:
            return _float_or_value_error(normalized)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Could not robustly parse float from {val!r}") from e

    raise ValueError(f"Could not robustly parse float from {val!r}")


_DEFAULT_PYTHON_SCRIPTS = {
    "Prime Numbers": textwrap.dedent("""\
        # Calculate primes, sharing the sieve via sp.primerange().
        low, high = sp.prime(1000), sp.prime(1010)

        result = {
            "title": "Prime Numbers in Range",
            "primes": [
                {"position": i, "prime": p}
                for i, p in zip(range(1000, 1011),
                                list(sp.primerange(low, high + 1)))
            ]
        }"""),
    "Hello WriterAgent": textwrap.dedent("""\
        # A simple hello world script
        result = "Hello from WriterAgent Python script!"
        """).rstrip(),
    "Universal Sample": textwrap.dedent("""\
        import writeragent as wa

        doc_type = wa.get_active_document_type()
        print(f"Detected active document type: {doc_type}")

        # 1. Insert rich HTML
        if doc_type == "writer":
            wa.writer.apply_document_content(content=["<h1>Hello from WriterAgent</h1>", "<p>Rich <b>HTML</b> at the end.</p>"], target="end")
        elif doc_type == "calc":
            wa.calc.insert_cell_html(cell="A1", html="<h1>Hello from WriterAgent</h1><p>Rich <b>HTML</b>.</p>")
        else:
            print("Unsupported document type for rich text insertion.")

        # 2. 24-sided star (sizes in 100ths of a mm; 4000 = 4cm)
        _ = wa.shape.upsert(action="create", shape_type="star24", x=2000, y=5000, width=4000, height=4000, fill_color="blue", text="24-sided Star")
        print("Inserted a 24-sided blue star shape.")
        """).strip(),
}

# Shipped Universal Sample used these tokens; replace the whole script, not a
# substring patch, so Monaco shows the one-line-call version.
# ``if __name__ == "__main__": run()`` is the previous function-wrapped sample —
# Run Python Script already execs at module top-level with ``__name__ == "__main__"``.
_LEGACY_UNIVERSAL_SAMPLE_MARKERS = ('cell_address="A1"', "wa.shape.upsert_shape(", "Hello from Python SDK", 'if __name__ == "__main__":\n    run()')


# Default endpoint normalizer.
_endpoint_normalizer: Callable[[str, bool], str] = normalize_endpoint_url


def set_endpoint_normalizer(fn: Callable[[str, bool], str]) -> None:
    """Register a custom endpoint normalization function.

    Called by ``config.py`` to parse Settings combobox labels when chatbot is present,
    since this schema module cannot import chatbot helpers directly.
    """
    global _endpoint_normalizer
    _endpoint_normalizer = fn


# 1024 maps to vendor "1K". Models dislike 512 / 0.5K — OpenRouter chat
# rejects image_size "0.5K" for some Gemini image models. On-page display
# is capped separately (visual_helpers.GENERATED_IMAGE_MAX_DISPLAY_MM).
DEFAULT_IMAGE_BASE_SIZE = 1024


@dataclasses.dataclass
class WriterAgentConfig:
    """Dataclass schema for WriterAgent configuration."""

    endpoint: str = "http://localhost:11434"
    text_model: str = ""
    # max 1.0; parse failures stay the unset sentinel -1.0.
    temperature: float = dataclasses.field(default=-1.0, metadata={"kind": "float", "max": 1.0, "fallback": 1.0, "parse_fallback": -1.0, "code": "INVALID_TEMPERATURE", "message": "Temperature must be <= 1.0"})
    additional_instructions: str = ""
    chat_max_tokens: int = dataclasses.field(default=16384, metadata={"kind": "int", "min": 0, "fallback": 16384, "parse_fallback": 16384, "code": "INVALID_CHAT_MAX_TOKENS", "message": "Chat max tokens must be >= 0"})
    # Sidebar history auto-compact (plugin/chatbot/compaction.py). False disables
    # both proactive compact and overflow retry.
    chat_compaction_enabled: bool = True
    request_timeout: int = dataclasses.field(default=120, metadata={"kind": "int", "min_exclusive": 0, "fallback": 120, "parse_fallback": 120, "code": "INVALID_REQUEST_TIMEOUT", "message": "Request timeout must be > 0"})
    api_keys_by_endpoint: Dict[str, str] = dataclasses.field(default_factory=dict)
    # Normalized endpoint URL -> effort string. Missing or blank sends no effort field.
    reasoning_effort: Dict[str, str] = dataclasses.field(default_factory=dict)
    image_base_size: int = DEFAULT_IMAGE_BASE_SIZE
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
    # Learned vision yes/no per "endpoint@model", same role as audio_support_map.
    vision_support_map: Dict[str, bool] = dataclasses.field(default_factory=dict)
    calc_prompt_max_tokens: int = 4096
    # When True, treat endpoint as OpenRouter (e.g. custom proxy) even if the URL lacks openrouter.ai.
    is_openrouter: bool = False
    # Wire always sends parallel_tool_calls: false until the tool loop can apply
    # parallel calls. Default matches the wire so Options/file do not lie.
    parallel_tool_calls: bool = False
    # Merged into POST …/chat/completions JSON when OpenRouter is active; see AGENTS.md.
    openrouter_chat_extra: Dict[str, Any] = dataclasses.field(default_factory=dict)
    last_python_script_name_writer: str = "Universal Sample"
    last_python_script_name_calc: str = "Universal Sample"
    last_python_script_name_draw: str = "Universal Sample"

    # Text analytics (sentiment etc.) — see plugin/scripting/text_analytics.py.
    # Only the transformers engine is implemented; this key overrides the Hugging Face model id (read by plugin/scripting/client.py).
    text_analytics_sentiment_model: str = "cardiffnlp/twitter-xlm-roberta-base-sentiment"

    # Persists the last entries for inserting LaTeX math
    last_latex_input: str = r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"
    last_latex_display_block: bool = False

    # Persists multiple user-saved Python scripts (name -> code)
    saved_python_scripts: Dict[str, str] = dataclasses.field(default_factory=lambda: dict(_DEFAULT_PYTHON_SCRIPTS))


_MISSING_VALUE = object()


def _normalize_schema_type(schema_type: Any) -> str | None:
    if schema_type is None:
        return None
    t = str(schema_type).strip().lower()
    if t == "bool":
        return "boolean"
    return t


def _dataclass_field_default(field: "dataclasses.Field[Any]") -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:  # type: ignore[attr-defined]
        return field.default_factory()  # type: ignore[misc]
    return None


def _dataclass_field_type(field: "dataclasses.Field[Any]") -> str | None:
    # from __future__ import annotations stores field.type as a string.
    # get_type_hints resolves it so ``is int`` still matches on 3.9 and 3.13.
    resolved = typing.get_type_hints(WriterAgentConfig).get(field.name, field.type)
    origin = typing.get_origin(resolved) or resolved
    if resolved is int:
        return "int"
    if resolved is float:
        return "float"
    if resolved is bool:
        return "boolean"
    if resolved is str:
        return "string"
    if origin is list or isinstance(_dataclass_field_default(field), list):
        return "list"
    if origin is dict or isinstance(_dataclass_field_default(field), dict):
        return "dict"
    return None


def _module_schema_for_key(key: str) -> dict[str, Any] | None:
    schema = CONFIG_SCHEMAS.get(key)
    if schema is None:
        return None
    return dict(schema)


def _dataclass_schema_for_key(key: str) -> dict[str, Any] | None:
    safe_key = key.replace(".", "_")
    for field in dataclasses.fields(WriterAgentConfig):
        if field.name == "_extra_config" or field.name != safe_key:
            continue
        schema: dict[str, Any] = {"default": _dataclass_field_default(field)}
        field_type = _dataclass_field_type(field)
        if field_type:
            schema["type"] = field_type
        # validate() reads these from field metadata. Callers of
        # get_config_schema / strict coerce (settings compare, save)
        # never saw them, so temperature 5.0 and chat_max_tokens -1 passed.
        for bound in ("min", "max", "min_exclusive", "code", "message", "fallback", "parse_fallback"):
            if bound in field.metadata:
                schema[bound] = field.metadata[bound]
        return schema
    return None


def get_config_schema(key: str) -> dict[str, Any] | None:
    """Return the config schema for a flat or dotted key.

    Module schemas come from ``module.yaml`` via the manifest and take
    precedence over dataclass defaults, matching ``resolve_default``.
    """
    schema = _module_schema_for_key(key) or _dataclass_schema_for_key(key)
    if schema is None and key.split("@", 1)[0] in _LRU_LIST_CONFIG_KEY_PREFIXES:
        return {"type": "list", "default": []}
    # log_level's runtime default is the plugin/tests probe, not the yaml
    # literal. Overlay so get_config_schema["default"] matches get_config.
    if schema is not None and key == "core.log_level":
        schema = dict(schema)
        schema["default"] = resolve_default("core.log_level")
    return schema


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


def _strict_bool_ok(value: Any) -> bool:
    if type(value) in (bool, int, float):
        return True
    if type(value) is str:
        return value.strip().lower() in ("", "0", "1", "true", "false", "yes", "no", "on", "off")
    return False


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Pure config validator.

    Coerce each known key (parsers, as_bool, option labels, endpoint normalizer).
    Raise ConfigValidationError on a wrong type or out-of-range number (min / max /
    min_exclusive and module bounds). Unknown keys pass through. Defaults stay.
    Also, and not raises: PO-header strip, non-dict map reset, calc_prompt_max_tokens < 100
    to 4096, saved-script sample migration.
    """
    out = dict(data)

    # 1. PO-header strip
    for k, v in list(out.items()):
        if isinstance(v, str) and "Project-Id-Version:" in v:
            log.debug("config validate: stripped PO/header from key %r", k)
            out[k] = "-1" if k == "seed" else ""

    # 2. Non-dict map reset
    for dict_key in (
        "openrouter_chat_extra",
        "reasoning_effort",
        "api_keys_by_endpoint",
        "audio_support_map",
        "vision_support_map",
        "saved_python_scripts",
    ):
        if dict_key in out and not isinstance(out[dict_key], dict):
            log.warning("Invalid %s (not a dict), resetting to {}", dict_key)
            out[dict_key] = {}

    # 3. calc_prompt_max_tokens < 100 to 4096
    if "calc_prompt_max_tokens" in out:
        c_val = out["calc_prompt_max_tokens"]
        try:
            c_int = parse_int_robust(c_val)
            if c_int < 100:
                log.info("Upgrading calc_prompt_max_tokens from %s to 4096", c_int)
                out["calc_prompt_max_tokens"] = 4096
            else:
                out["calc_prompt_max_tokens"] = c_int
        except (ValueError, OverflowError):
            pass

    # 4. saved-script sample migration
    saved_scripts = out.get("saved_python_scripts")
    if isinstance(saved_scripts, dict):
        saved_scripts.pop("Sample", None)
        if "Universal Sample" not in saved_scripts:
            saved_scripts["Universal Sample"] = _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]
        elif isinstance(saved_scripts.get("Universal Sample"), str):
            curr = saved_scripts["Universal Sample"]
            if any(marker in curr for marker in _LEGACY_UNIVERSAL_SAMPLE_MARKERS):
                saved_scripts["Universal Sample"] = _DEFAULT_PYTHON_SCRIPTS["Universal Sample"]

    # 5. Endpoint normalization
    if "endpoint" in out:
        ep_str = str(out["endpoint"] or "").strip()
        if ep_str:
            is_openwebui = as_bool(out.get("is_openwebui", False))
            out["endpoint"] = _endpoint_normalizer(ep_str, is_openwebui)
        else:
            out["endpoint"] = ""

    # 6. Coerce and validate known keys
    for key, val in list(out.items()):
        if key in ("endpoint", "saved_python_scripts"):
            continue
        schema = get_config_schema(key)
        if not schema:
            continue

        val = _canonicalize_schema_option_value(schema, val)
        schema_type = _normalize_schema_type(schema.get("type"))

        if schema_type in ("int", "float"):
            parser = parse_int_robust if schema_type == "int" else parse_float_robust
            try:
                coerced_val = parser(val)
            except (ValueError, OverflowError) as exc:
                code = schema.get("code", "CONFIG_INVALID_VALUE")
                msg = schema.get("message")
                details = {"key": key, "value": val, "reason": "parse"}
                if msg:
                    raise ConfigValidationError(_(msg), code=code, details=details) from exc
                raise ConfigValidationError(
                    f"Invalid configuration value for {key}: not an {'integer' if schema_type == 'int' else 'number'}",
                    code="CONFIG_INVALID_VALUE",
                    details=details,
                ) from exc

            out_of_range = False
            try:
                if "min" in schema and coerced_val < parse_float_robust(schema["min"]):
                    out_of_range = True
                if "min_exclusive" in schema and coerced_val <= parse_float_robust(schema["min_exclusive"]):
                    out_of_range = True
                if "max" in schema and coerced_val > parse_float_robust(schema["max"]):
                    out_of_range = True
            except (ValueError, OverflowError):
                pass

            if out_of_range:
                code = schema.get("code", "CONFIG_INVALID_VALUE")
                msg = schema.get("message")
                if msg:
                    raise ConfigValidationError(_(msg), code=code, details={"key": key, "value": val})
                raise ConfigValidationError(
                    f"Invalid configuration value for {key}: out of range",
                    code="CONFIG_INVALID_VALUE",
                    details={"key": key, "value": val},
                )
            out[key] = coerced_val

        elif schema_type == "boolean":
            if not _strict_bool_ok(val):
                raise ConfigValidationError(
                    f"Invalid configuration value for {key}: not a boolean",
                    code="CONFIG_INVALID_VALUE",
                    details={"key": key, "value": val},
                )
            out[key] = as_bool(val)

        elif schema_type == "list":
            if isinstance(val, list):
                out[key] = val
            elif isinstance(val, str) and val.strip():
                out[key] = [val.strip()]
            else:
                raise ConfigValidationError(
                    f"Invalid configuration value for {key}: not a list",
                    code="CONFIG_INVALID_VALUE",
                    details={"key": key, "value": val},
                )

        elif schema_type == "string":
            out[key] = "" if val is None else str(val)

    return out


# --- Default resolution ---


def resolve_default(key: str) -> Any:
    """Resolve default for key: schema first, then dataclass. Safe fallbacks for None."""
    if key == "core.log_level":
        tests_dir = os.path.join(get_plugin_dir(), "tests")
        return "DEBUG" if os.path.isdir(tests_dir) else "WARN"

    val = CONFIG_DEFAULTS.get(key)
    if val is not None:
        return val

    if key.split("@", 1)[0] in _LRU_LIST_CONFIG_KEY_PREFIXES:
        return []

    safe_key = key.replace(".", "_")
    for f in dataclasses.fields(WriterAgentConfig):
        if f.name == safe_key:
            return _dataclass_field_default(f)

    # Strict check: if not in schema and not a recognized dynamic pattern, it's a bug.
    raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", code="CONFIG_KEY_NOT_FOUND", details={"key": key})


def _is_equal_to_default(key: str, value: Any, default_val: Any) -> bool:
    """Return True if `value` equals `default_val`."""
    if default_val is None:
        return value is None

    if isinstance(default_val, bool):
        return as_bool(value) is default_val

    if isinstance(default_val, (int, float)):
        if isinstance(value, bool):
            return False
        try:
            return parse_float_robust(value) == parse_float_robust(default_val)
        except (ValueError, TypeError, OverflowError):
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
        resolve_default(key)
    except ConfigError:
        return False
    return True


def is_default_value(key: str, value: Any) -> bool:
    """Return True if `value` matches the default configuration value for `key`."""
    try:
        default_val = resolve_default(key)
    except ConfigError:
        return False
    return _is_equal_to_default(key, value, default_val)
