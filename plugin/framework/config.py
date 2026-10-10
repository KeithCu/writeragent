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
"""Configuration I/O for WriterAgent.

``init_config(ctx)`` runs once at bootstrap (``MainBootstrapJob`` / ``bootstrap()``);
the config path is cached. All other I/O — ``get_config``, ``set_config``,
``set_configs``, typed getters, ``get_api_config`` — does **not** take ``ctx``;
use ``get_ctx()`` only for UNO operations.

``writeragent.json`` lives under the LibreOffice user profile (Linux:
``~/.config/libreoffice/{4,24}/user/``; macOS: ``~/Library/Application Support/LibreOffice/4/user/``;
Windows: ``%APPDATA%\\LibreOffice\\4\\user\\``). LibrePy shares this same file on
purpose (venv path, session mode, timeouts). Broken JSON is copied to
``.bak`` when possible; ``json_repair`` fixes small typos on read.

Writes omit a key that still matches its default and prefix the file with
``//`` comment lines pointing at ``docs/writeragent-config-schema.md`` on
GitHub. Those comments are stripped on read. ``set_config`` /
``update_config`` do not validate, write, or emit ``config:changed`` when
the coerced value already matches the stored value, or the schema default
when that key is omitted from the file. The default itself is not a change.
``set_configs`` applies that same per-key rule, then validates and writes
once. A validation error writes nothing and emits nothing. If nothing
changed, it does not write or emit. A write of one or two real changes
patches only those keys. It does not replace the file with a schema-wide
``to_dict()``.

Concurrency: one ``ConfigStore`` is the only writer. ``update(key, fn)``
(``update_config``) holds ``_config_write_lock`` (an ``RLock``) across the
read, ``fn``, and the patch, so a second writer cannot save a copy it
loaded earlier and drop the first writer's keys. ``set_config`` /
``set_configs`` / ``remove_config`` / ``update_config_mapping`` and
**GET-path** repairs (broken JSON, out-of-range numbers, old
``calc_prompt_max_tokens``) go through that store. A ``set_configs`` value
for ``api_keys_by_endpoint`` is a slot patch: those URL keys are merged
into the map just read under the lock, so a one-slot Settings OK does not
replace the map with a copy taken earlier. One ``config:changed``
is emitted **after** the lock is released, with ``key``, ``value``, and
``old_value``, so listeners may call ``get_config`` / ``set_config``
without deadlocking. Callers that map a settings key onto a stored key
(for example ``ai.endpoint`` → ``endpoint``) pass ``event_key`` so
listeners still see the key that was set. A batch that changes one key
emits that key. A batch that changes more than one emits ``key=""`` —
the bulk save Settings OK listeners already treat as "every module".

Schema-backed coercion, option canonicalization, and min/max bounds live in
``config_schema.py``. Import those names from there. Dataclass
``min`` / ``max`` / ``min_exclusive`` are part of that schema: strict
coerce rejects a value outside them, and ``WriterAgentConfig.validate``
still enforces the same bounds (including load-time fallbacks). This
module is path, cache, and JSON I/O only. Do not import this file from
``config_schema.py``.
"""

# crosshair: off
from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict

from plugin.framework.errors import ConfigError, ConfigValidationError, safe_call
from plugin.framework.event_bus import global_event_bus
from plugin.framework.json_utils import repair_json
from plugin.framework.url_utils import normalize_endpoint_url

from plugin.framework import config_schema as _config_schema


def _normalize_configured_endpoint_with_selector(endpoint_str: str, is_openwebui: bool) -> str:
    """WriterAgent Settings may store a preset label; LibrePy omits chatbot helpers."""
    try:
        from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text

        return endpoint_from_selector_text(endpoint_str)
    except ImportError:
        return normalize_endpoint_url(endpoint_str, is_openwebui=is_openwebui)


# Overlay after schema import so WriterAgentConfig.validate() keeps preset labels
# without config_schema importing chatbot (LibrePy / one-way import).
_config_schema.set_endpoint_normalizer(_normalize_configured_endpoint_with_selector)

# Comment header written above the JSON object. Not a config key.
CONFIG_SCHEMA_DOC_URL = "https://github.com/KeithCu/writeragent/blob/master/docs/writeragent-config-schema.md"
CONFIG_SCHEMA_COMMENT = "// Only settings that differ from defaults are stored here.\n// Full schema: " + CONFIG_SCHEMA_DOC_URL + "\n"

_uno_mod: Any
try:
    import uno as _uno_impl

    _uno_mod = _uno_impl
except ImportError:
    _uno_mod = None
uno: Any = _uno_mod

log = logging.getLogger(__name__)

# --- Module constants ---

CONFIG_FILENAME = "writeragent.json"
CONFIG_BACKUP_SUFFIX = ".bak"

# Max items for all LRU lists; base names also listed in _LRU_LIST_CONFIG_KEY_PREFIXES for get_config defaults.
LRU_MAX_ITEMS = 10
# Simple AI settings fields that the Tools → Options "AI" page should map
# directly to top-level config keys (endpoint, model, etc.).
# ``stt_model`` is the Options field name; Settings saves ``audio.stt_model``.
# parallel_tool_calls is not in the Options page until the tool loop can honor
# it; the wire always sends false (see base_provider_shim).
AI_SIMPLE_FIELDS = {"endpoint", "text_model", "image_model", "stt_model", "temperature", "chat_max_tokens", "request_timeout", "additional_instructions"}

# Dotted keys whose unsuffixed alias must stay in the file. set_config normally
# drops ``stt_model`` when writing ``audio.stt_model`` (flat name is the alias).
# That would erase the pre-move value; get_stt_model still reads it.
_DUAL_READ_DOTTED_KEYS_KEEP_FLAT = frozenset({"audio.stt_model"})

_resolved_config_path = None
# RLock: set_config holds this while loading; GET-path persist helpers take it
# too. Same-thread get_config during a nested call must not deadlock.
_config_write_lock = threading.RLock()


def _resolve_config_path_from_ctx(ctx: Any) -> str:
    """Resolve writeragent.json path from a UNO component context."""
    try:
        sm = safe_call(ctx.getServiceManager, "Get ServiceManager")
        path_settings = safe_call(sm.createInstanceWithContext, "Create PathSettings", "com.sun.star.util.PathSettings", ctx)
        user_config_path = getattr(path_settings, "UserConfig", "")
        if uno and user_config_path and str(user_config_path).startswith("file://"):
            user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
        if (
            not isinstance(user_config_path, str)
            or not user_config_path.strip()
            or type(user_config_path).__name__ in ("Mock", "MagicMock")
            or hasattr(user_config_path, "_mock_return_value")
            or "MagicMock" in str(user_config_path)
        ):
            raise ConfigError("Invalid or missing UserConfig path setting", "CONFIG_PATH_ERROR")
        return os.path.join(user_config_path, CONFIG_FILENAME)
    except Exception as e:
        raise ConfigError(f"Failed to resolve config path: {e}", "CONFIG_PATH_ERROR") from e


def init_config(ctx: Any | None = None) -> str:
    """Resolve and cache writeragent.json path. Idempotent; call once at bootstrap."""
    global _resolved_config_path
    if ctx is not None:
        try:
            from plugin.framework.queue_executor import default_executor

            default_executor.set_context(ctx)
        except Exception:
            log.exception("init_config: default_executor.set_context failed")
    if _resolved_config_path is not None:
        return _resolved_config_path
    if ctx is None:
        from plugin.framework.thread_guard import on_main_thread

        if not on_main_thread():
            raise ConfigError("UNO context is required to resolve config path on background thread")
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
    if ctx is None:
        raise ConfigError("UNO context is required to resolve config path")
    _resolved_config_path = _resolve_config_path_from_ctx(ctx)
    return _resolved_config_path


def reset_config_for_tests() -> None:
    """Clear cached config path and in-memory dict (pytest isolation)."""
    global _resolved_config_path
    _resolved_config_path = None
    _invalidate_config_cache()


def _config_path() -> str:
    """Return the absolute path to writeragent.json."""
    if _resolved_config_path is not None:
        return _resolved_config_path
    return init_config()


def _emit_config_changed_ctx() -> Any:
    """Return UNO ctx for config:changed listeners when on the main thread."""
    try:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework.uno_context import get_ctx

        return get_ctx() if on_main_thread() else None
    except Exception:
        return None


def user_config_dir() -> str | None:
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
    # A second corruption must not overwrite the only earlier copy.
    if os.path.exists(backup_path):
        stamped = backup_path + "." + time.strftime("%Y%m%d%H%M%S")
        if os.path.exists(stamped):
            stamped = stamped + "." + str(time.time_ns())
        backup_path = stamped
    try:
        shutil.copy2(config_file_path, backup_path)
        log.warning("Backed up config %s to %s (%s)", config_file_path, backup_path, reason)
        return backup_path
    except OSError:
        log.exception("Failed to backup config %s", config_file_path)
        return None


def _strip_config_comment_header(text: str) -> str:
    """Drop leading ``//`` comment lines and blank lines so json.loads can run."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip(" \t")
        if stripped == "" or stripped.startswith("//"):
            i += 1
            continue
        break
    return "".join(lines[i:])


def parse_config_json_text(text: str) -> dict[str, Any] | None:
    """Parse writeragent.json text, ignoring the optional ``//`` schema header."""
    return _try_parse_config_dict(text)


def _try_parse_config_dict(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_strip_config_comment_header(text))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _try_repair_config_dict(text: str) -> dict[str, Any] | None:
    """Config-safe JSON repair: json strict=False and json_repair only (no literal_eval / LaTeX rewrite)."""
    stripped = _strip_config_comment_header(text)
    try:
        data = json.loads(stripped, strict=False)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    try:
        repaired = repair_json(stripped)
        data = json.loads(repaired, strict=False)
        if isinstance(data, dict):
            return data
    except Exception:
        # This try is only the repair attempt. A contract error is an
        # AssertionError, and mypy rejects that class in an except clause.
        # A failure falls through the existing unrepairable path. A
        # successful repair still returns the dict above.
        pass

    return None


def _write_config_file(config_file_path: str, data: dict[str, Any]) -> None:
    """Write config via temp file + ``os.replace`` so a crash cannot truncate the live file."""
    body = json.dumps(data, indent=4)
    if not body.endswith("\n"):
        body += "\n"
    content = CONFIG_SCHEMA_COMMENT + body
    directory = os.path.dirname(config_file_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".writeragent-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_file_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _invalidate_config_cache() -> None:
    _cache.data = None
    _cache.mtime = 0
    _cache.mtime_last_checked = 0.0


def _load_config_dict(config_file_path: str, *, allow_repair: bool = False, persist_repair: bool = False, fail_on_unrepairable: bool = False) -> dict[str, Any]:
    """Load writeragent.json as a dict. Optionally backup, repair, and persist small JSON typos."""
    if not config_file_path or not os.path.exists(config_file_path):
        return {}

    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(f"Failed to read config: {e}", "CONFIG_READ_ERROR", details={"path": config_file_path}) from e

    data = _try_parse_config_dict(text)
    if data is not None:
        return data

    backup_path: str | None = None
    if allow_repair:
        backup_path = _backup_config_file(config_file_path, reason="invalid-json")
        data = _try_repair_config_dict(text)
        if data is not None:
            log.info("Auto-repaired invalid JSON in %s (backup: %s)", config_file_path, backup_path)
            if persist_repair:
                try:
                    # GET-path persist must serialize with set_config (RLock if nested).
                    with _config_write_lock:
                        _write_config_file(config_file_path, data)
                        _invalidate_config_cache()
                except OSError as e:
                    raise ConfigError(f"Failed to write repaired config: {e}", "CONFIG_SAVE_ERROR", details={"path": config_file_path, "backup_path": backup_path}) from e
            return data
        if fail_on_unrepairable:
            # A later set_config used to load this {} and os.replace the file,
            # wiping every other setting. The GET path raises too, so a bad
            # file is not cached as a fresh install with empty API keys.
            log.warning("Invalid JSON in %s could not be auto-repaired (backup: %s).", config_file_path, backup_path or "none")
            raise ConfigError(f"Invalid JSON in {config_file_path} could not be repaired", "CONFIG_INVALID_FORMAT", details={"path": config_file_path, "backup_path": backup_path})
        log.warning("Invalid JSON in %s could not be auto-repaired (backup: %s). Using empty dict for this load.", config_file_path, backup_path or "none")
        return {}

    log.warning("Invalid JSON in %s (repair disabled). Using empty dict for this load.", config_file_path)
    return {}


def is_grammar_enabled() -> bool:
    """True if the grammar checker is enabled on the Doc tab (LLM, LanguageTool, Vale, or Harper)."""
    from plugin.framework.uno_context import is_libreharper

    if is_libreharper():
        return True

    val = get_config("doc.grammar_proofreader_enabled")
    if isinstance(val, bool):
        return val  # Handle old boolean config
    val_str = str(val).strip().lower()
    return val_str in ("llm", "languagetool", "vale", "harper", "true")


def get_grammar_provider() -> str:
    """Return the active grammar provider name ('off', 'llm', 'languagetool', 'vale', or 'harper')."""
    from plugin.framework.uno_context import is_libreharper

    if is_libreharper():
        return "harper"

    val = get_config("doc.grammar_proofreader_enabled")
    if isinstance(val, bool):
        return "llm" if val else "off"
    val_str = str(val).strip().lower()
    if val_str == "true":
        return "llm"
    if val_str in ("llm", "languagetool", "vale", "harper"):
        return val_str

    return "off"


def grammar_checker_identity() -> str:
    """Stable cache/file identity for the active grammar checker.

    Local engines use a sentinel (``harper`` / ``languagetool`` / ``vale``).
    LLM uses ``llm:`` plus the resolved model from ``get_grammar_model()``.
    """
    provider = get_grammar_provider()
    if provider in ("harper", "languagetool", "vale"):
        return provider
    if provider == "off":
        return "off"
    try:
        from plugin.framework.client.model_fetcher import get_grammar_model

        return f"llm:{get_grammar_model() or ''}"
    except (ImportError, ModuleNotFoundError):
        return "llm:unknown"


def get_current_endpoint() -> str:
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

# --- Validated JSON export ---


def _build_validated_config_export(data: Dict[str, Any], config: _config_schema.WriterAgentConfig) -> Dict[str, Any]:
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


# --- Core config I/O ---


def _copy_config_value(value: Any) -> Any:
    """Return a copy of dict/list config values.

    ``_cache.data`` stores the validated file. A shallow copy left nested
    ``openrouter_chat_extra`` and ``api_keys_by_endpoint`` aliased to the
    cache for the two-second mtime window.
    """
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    return value


def get_config(key: str) -> Any:
    """Get a config value by key. JSON overrides; when key is missing, use schema default then central fallback."""
    config_data = _get_validated_config_dict()
    if not isinstance(config_data, dict):
        config_data = {}

    if key in config_data:
        return _copy_config_value(config_data[key])

    for dotted in _config_schema._dotted_fallback_keys(key):
        if dotted in config_data:
            return _copy_config_value(config_data[dotted])

    return _copy_config_value(_config_schema._resolve_default(key))


def get_config_int(key: str) -> int:
    """Get a config value as int. All requested keys MUST be in the schema (WriterAgentConfig or MODULES).
    Throws ConfigError if the key is missing or invalid (use get_config_int_safe to return a default instead)."""
    v = get_config(key)
    # Empty string or None from JSON/UI: use schema default (same as missing key).
    if v == "" or v is None:
        v = _config_schema._resolve_default(key)
    # _resolve_default returns "" for unknown keys that slip through without a dataclass default.
    if v == "":
        raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", "CONFIG_KEY_NOT_FOUND", details={"key": key})
    try:
        return _config_schema.parse_int_robust(v)
    except ValueError as e:
        raise ConfigError(f"Config key {key!r} has non-integer value: {v!r}", "CONFIG_TYPE_ERROR") from e


def get_config_str(key: str) -> str:
    """Get a config value as str.

    A missing value (``None``) returns ``""``. This does not raise.
    ``get_config_int`` is the accessor that raises ``ConfigError`` for a
    missing key.
    """
    v = get_config(key)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def get_config_bool(key: str) -> bool:
    """Get a config value as bool. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found (use get_config_bool_safe to return a default instead)."""
    v = get_config(key)
    return _config_schema.as_bool(v)


def get_config_bool_safe(key: str) -> bool:
    """Safely read a boolean config value. Unlike get_config_bool, this returns the schema default (or False) rather than raising an exception if the key is missing or invalid."""
    try:
        return get_config_bool(key)
    except Exception:
        try:
            return _config_schema.as_bool(_config_schema._resolve_default(key))
        except Exception:
            return False


def get_config_int_safe(key: str) -> int:
    """Safely read an integer config value. Unlike get_config_int, this returns the schema default (or 0) rather than raising an exception if the key is missing or the value is invalid."""
    try:
        return get_config_int(key)
    except Exception:
        try:
            return _config_schema.parse_int_robust(_config_schema._resolve_default(key))
        except Exception:
            return 0


def get_config_float(key: str) -> float:
    """Get a config value as float. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found or value is non-float."""
    v = get_config(key)
    if v == "" or v is None:
        v = _config_schema._resolve_default(key)
    if v == "":
        raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", "CONFIG_KEY_NOT_FOUND", details={"key": key})
    try:
        return _config_schema.parse_float_robust(v)
    except ValueError as e:
        raise ConfigError(f"Config key {key!r} has non-float value: {v!r}", "CONFIG_TYPE_ERROR") from e


def get_config_dict() -> dict[str, Any]:
    """Return the full config as a dict. Returns {} if missing or on error.

    Copies each value the same way ``get_config`` does. Returning ``_cache.data``
    itself let a caller change memory without a write.
    """
    data = _get_validated_config_dict()
    if not isinstance(data, dict):
        return {}
    return {key: _copy_config_value(value) for key, value in data.items()}


def _raw_config_value_for_key(config_data: dict[str, Any], key: str) -> Any:
    if key in config_data:
        return config_data[key]
    for dotted in _config_schema._dotted_fallback_keys(key):
        if dotted in config_data:
            return config_data[dotted]
    if "." in key:
        field_name = key.split(".", 1)[1]
        if field_name in config_data:
            return config_data[field_name]
    return _config_schema._MISSING_VALUE


def _omitted_value_matches_schema_default(key: str, value: Any) -> bool:
    """True when a coerced value is the schema default for a key not on disk.

    Unknown keys have no default (``_resolve_default`` raises). Those still
    go through validate and write.
    """
    try:
        schema_default = _config_schema._resolve_default(key)
    except ConfigError:
        return False
    return value == schema_default


def _stage_config_assignment(config_data: dict[str, Any], key: str, value: Any) -> tuple[bool, Any, Any]:
    """Apply one ``set_config`` assignment to *config_data*. No validate or write.

    Returns ``(changed, coerced, previous)``. An unchanged key (stored value,
    or the schema default when the key is omitted) leaves the dict alone.
    """
    current_value = _raw_config_value_for_key(config_data, key)
    previous = None if current_value is _config_schema._MISSING_VALUE else current_value
    # strict: an unparseable write must not look like success by snapping back
    # to the previous value. Load/repair stays non-strict.
    coerced = _config_schema.coerce_config_value(key, value, fallback_value=current_value, strict=True)
    # Unchanged means the coerced value already matches what is on disk.
    # A present key compares to the stored value. An omitted key is not
    # stored as None: dict.get returns None, None != the schema default,
    # and Settings OK revalidated, rewrote, and emitted config:changed
    # for every omitted default. The on-disk value of an omitted key is
    # the schema default, so a match is not a write.
    if config_data.get(key) == coerced or (
        current_value is _config_schema._MISSING_VALUE
        and _omitted_value_matches_schema_default(key, coerced)
    ):
        return False, coerced, previous

    for dotted in _config_schema._dotted_fallback_keys(key):
        config_data.pop(dotted, None)
    if "." in key and key not in _DUAL_READ_DOTTED_KEYS_KEEP_FLAT:
        config_data.pop(key.split(".", 1)[1], None)
    config_data[key] = coerced
    return True, coerced, previous


def _validated_config_for_write(data: dict[str, Any], keys_label: str) -> _config_schema.WriterAgentConfig:
    """Validate a copy of *data*. The caller's dict is left unchanged.

    Validation still runs (bounds, endpoint ``/v1``), but this does not
    return ``to_dict()``. That dump drops unknown keys and other defaults,
    so a later one-key save would erase keys an earlier writer had stored.
    The file patch is applied separately, and only for keys this call changed.
    """
    try:
        # deepcopy: validate() mutates nested dicts in place (saved scripts).
        # Sharing them with the loaded file would write those mutations
        # into keys this call did not change.
        cfg = _config_schema.WriterAgentConfig.from_dict(copy.deepcopy(data))
        cfg.validate()
        return cfg
    except ConfigValidationError:
        raise
    except Exception as e:
        log.exception("Validation error saving config")
        raise ConfigValidationError(f"Invalid configuration value for {keys_label}: {e}") from e


def _validated_file_value(config: _config_schema.WriterAgentConfig, key: str) -> Any:
    """Value of *key* after ``validate()``, or ``_MISSING_VALUE`` if absent."""
    safe_key = key.replace(".", "_")
    field_names = {f.name for f in dataclasses.fields(config) if f.name != "_extra_config"}
    if safe_key in field_names:
        return getattr(config, safe_key)
    if key in config._extra_config:
        return config._extra_config[key]
    return _config_schema._MISSING_VALUE


def _value_for_update(loaded: dict[str, Any], key: str) -> Any:
    """Current value passed to ``update(key, fn)``.

    Same answer ``get_config`` would give: the stored value, or the schema
    default when the key is omitted. A copy, so ``fn`` cannot alias the
    loaded dict.
    """
    raw = _raw_config_value_for_key(loaded, key)
    if raw is _config_schema._MISSING_VALUE:
        try:
            return _copy_config_value(_config_schema._resolve_default(key))
        except ConfigError:
            return None
    return _copy_config_value(raw)


def _alias_keys_for_write(key: str) -> tuple[str, ...]:
    """Other names ``set_config`` drops when it writes *key*.

    Dotted fallbacks always go. The unsuffixed flat name goes too, except
    ``audio.stt_model``, which must keep a pre-move ``stt_model``.
    """
    aliases: list[str] = list(_config_schema._dotted_fallback_keys(key))
    if "." in key and key not in _DUAL_READ_DOTTED_KEYS_KEEP_FLAT:
        aliases.append(key.split(".", 1)[1])
    return tuple(aliases)


def _patch_changed_keys(loaded: dict[str, Any], staged: dict[str, Any], changed: list[tuple[str, Any, Any]]) -> tuple[dict[str, Any], list[tuple[str, Any, Any]]] | None:
    """Return ``(file, effective changes)`` or None when the file would not differ.

    *staged* is *loaded* plus the keys this call set. Validation can still
    normalize those keys (endpoint ``/v1``). The dict written back is
    *loaded* with only those keys (and their flat aliases) updated. Other
    keys, including ones that still hold a default, stay as they were read.
    """
    keys_label = ", ".join(key for key, _coerced, _prev in changed)
    cfg = _validated_config_for_write(staged, keys_label)
    out = dict(loaded)
    effective: list[tuple[str, Any, Any]] = []
    for key, _coerced, previous in changed:
        for alias in _alias_keys_for_write(key):
            out.pop(alias, None)
        new_val = _validated_file_value(cfg, key)
        if new_val is _config_schema._MISSING_VALUE:
            new_val = _coerced
        if _config_schema.is_default_value(key, new_val):
            out.pop(key, None)
        else:
            out[key] = _copy_config_value(new_val)
        aliases_changed = any(alias in loaded and alias not in out for alias in _alias_keys_for_write(key))
        value_changed = (key in loaded) != (key in out) or (key in out and out[key] != loaded.get(key))
        if value_changed or aliases_changed:
            effective.append((key, new_val, previous))
    if not effective or out == loaded:
        return None
    return out, effective


def _repaired_file_dict(loaded: dict[str, Any], config: _config_schema.WriterAgentConfig) -> dict[str, Any]:
    """File image after a load-time repair, touching only keys validate() changed.

    A GET used to persist ``to_dict()`` of the whole schema. That dropped
    every key the schema does not emit, including keys another writer had
    just saved. Only a value validate() actually changed is patched. A
    stored default that validate() left alone stays in the file.
    """
    out = dict(loaded)
    for key, old in loaded.items():
        new = _validated_file_value(config, key)
        if new is _config_schema._MISSING_VALUE or new == old:
            continue
        if _config_schema.is_default_value(key, new):
            out.pop(key, None)
        else:
            out[key] = _copy_config_value(new)
    # validate() copies a legacy ``model`` into ``text_model`` and clears
    # ``model``. The new key is not in *loaded*, so the loop above cannot
    # see it. Persist that one migration, not a full schema dump.
    migrated = str(config.text_model or "").strip()
    if migrated and not str(loaded.get("text_model") or "").strip() and str(loaded.get("model") or "").strip():
        if not _config_schema.is_default_value("text_model", config.text_model):
            out["text_model"] = config.text_model
    return out


@dataclasses.dataclass
class _PendingConfigEvent:
    """One ``config:changed`` to emit after the write lock is released."""

    key: str
    keys: tuple[str, ...]
    value: Any
    old_value: Any
    batch: bool


class ConfigStore:
    """The only writer of ``writeragent.json``.

    ``update(key, fn)`` reads the current value, calls ``fn``, and patches
    that key under ``_config_write_lock``. Callers do not load the JSON,
    edit a copy, and write the blob back.
    """

    def update(self, key: str, fn: Callable[[Any], Any], *, event_key: str | None = None) -> None:
        """Apply *fn* to one key. One event if the file changes."""
        path = _config_path()
        if not path:
            raise ConfigError("Config path is empty", "CONFIG_PATH_ERROR")
        self.apply(path, [(key, fn)], event_key=event_key, batch=False)

    def update_many(self, values: dict[str, Any]) -> None:
        """Set many keys with one load, one validate, one patch, one event."""
        if not values:
            return
        path = _config_path()
        if not path:
            raise ConfigError("Config path is empty", "CONFIG_PATH_ERROR")

        def _bind(key: str, stored: Any) -> Callable[[Any], Any]:
            def _replace(current: Any) -> Any:
                # Slot patch, not a whole-map replace. See _merge_api_key_slots.
                if key == "api_keys_by_endpoint" and isinstance(stored, dict):
                    return _merge_api_key_slots(current, stored)
                return stored

            return _replace

        self.apply(path, [(key, _bind(key, value)) for key, value in values.items()], batch=True)

    def apply(
        self,
        path: str,
        updates: list[tuple[str, Callable[[Any], Any]]],
        *,
        event_key: str | None = None,
        batch: bool = False,
        emit: bool = True,
        fail_on_unrepairable: bool = True,
    ) -> bool:
        """Patch *updates* into *path*. Return True when the file changed.

        *fn* runs under the lock and must not call back into the store.
        A no-op (value already on disk, or a missing key at its default)
        does not validate, write, or emit.
        """
        pending: _PendingConfigEvent | None = None
        with _config_write_lock:
            if os.path.exists(path):
                loaded = _load_config_dict(path, allow_repair=True, persist_repair=False, fail_on_unrepairable=fail_on_unrepairable)
            else:
                loaded = {}
            # Shallow copy: staging pops and replaces keys. *loaded* stays
            # the bytes we read so an unchanged file is not rewritten.
            staged = dict(loaded)
            changed: list[tuple[str, Any, Any]] = []
            for key, fn in updates:
                proposed = fn(_value_for_update(loaded, key))
                did_change, _coerced, previous = _stage_config_assignment(staged, key, proposed)
                if did_change:
                    changed.append((key, _coerced, previous))
            if not changed:
                return False
            committed = _patch_changed_keys(loaded, staged, changed)
            if committed is None:
                return False
            to_write, effective = committed
            try:
                _write_config_file(path, to_write)
                _invalidate_config_cache()
            except OSError as e:
                log.exception("Error writing to %s", path)
                raise ConfigError(f"Failed to save config: {e}", "CONFIG_SAVE_ERROR") from e
            if emit:
                pending = _event_for_changes(effective, event_key=event_key, batch=batch)
        if pending is not None:
            _emit_pending(pending)
        return True

    def remove(self, path: str, key: str, *, emit: bool = True) -> bool:
        """Drop *key* and its aliases. Leave every other key as it was read."""
        if not path or not os.path.exists(path):
            return False
        pending: _PendingConfigEvent | None = None
        with _config_write_lock:
            try:
                loaded = _load_config_dict(path, allow_repair=True, persist_repair=False, fail_on_unrepairable=True)
            except ConfigError:
                log.exception("remove_config skipped: config file could not be parsed")
                return False
            except OSError:
                log.exception("remove_config skipped: config file could not be read")
                return False
            out = dict(loaded)
            removed = False
            if key in out:
                out.pop(key, None)
                removed = True
            for dotted in _config_schema._dotted_fallback_keys(key):
                if dotted in out:
                    out.pop(dotted, None)
                    removed = True
            # A dotted key's flat alias is the pre-move name (stt_model for
            # audio.stt_model). Removing the dotted key removes that alias too.
            if "." in key:
                field_name = key.split(".", 1)[1]
                if field_name in out:
                    out.pop(field_name, None)
                    removed = True
            if not removed or out == loaded:
                return False
            try:
                _validated_config_for_write(out, key)
            except ConfigValidationError as e:
                log.warning("remove_config skipped write: remaining config is invalid: %s", e)
                return False
            except Exception:
                log.exception("remove_config validation failed; not writing unvalidated dict")
                return False
            try:
                _write_config_file(path, out)
                _invalidate_config_cache()
            except OSError as e:
                log.exception("Error writing to %s", path)
                raise ConfigError(f"Failed to remove config key: {e}", "CONFIG_SAVE_ERROR") from e
            if emit:
                pending = _PendingConfigEvent(key, (key,), None, None, False)
        if pending is not None:
            _emit_pending(pending)
        return True

    def persist_load_repairs(self, path: str, loaded: dict[str, Any], config: _config_schema.WriterAgentConfig) -> dict[str, Any]:
        """Write load-time repairs that changed a value. Return the file image.

        Caller holds ``_config_write_lock`` (RLock). No ``config:changed``:
        a read must not look like a settings save.
        """
        repaired = _repaired_file_dict(loaded, config)
        if repaired == loaded:
            return loaded
        try:
            _write_config_file(path, repaired)
        except OSError as write_err:
            log.warning("Failed to persist coerced config: %s", write_err)
            return loaded
        return repaired


def _event_for_changes(effective: list[tuple[str, Any, Any]], *, event_key: str | None, batch: bool) -> _PendingConfigEvent:
    keys = tuple(key for key, _value, _prev in effective)
    if len(effective) == 1:
        key, value, previous = effective[0]
        return _PendingConfigEvent(event_key or key, keys, value, previous, batch)
    return _PendingConfigEvent("", keys, None, None, True)


def _emit_pending(pending: _PendingConfigEvent) -> None:
    # Handlers may get_config/set_config; the caller already dropped the lock.
    if pending.batch:
        global_event_bus.emit(
            "config:changed",
            key=pending.key,
            keys=pending.keys,
            value=pending.value,
            old_value=pending.old_value,
            ctx=_emit_config_changed_ctx(),
        )
        return
    global_event_bus.emit(
        "config:changed",
        key=pending.key,
        value=pending.value,
        old_value=pending.old_value,
        ctx=_emit_config_changed_ctx(),
    )


_config_store = ConfigStore()


def update_config(key: str, fn: Callable[[Any], Any], *, event_key: str | None = None) -> None:
    """``ConfigStore.update``: one key, one lock, one event.

    *fn* receives the current value (the schema default when the key is
    omitted) and returns the new value. No write and no ``config:changed``
    when that value already matches disk.
    """
    _config_store.update(key, fn, event_key=event_key)


def set_config(key: str, value: Any, *, event_key: str | None = None) -> None:
    """Set a config key to value. Creates file if needed. Omits defaults.

    Returns without validating, writing, or emitting ``config:changed`` when
    the coerced value already matches disk: the stored value if the key is
    present, or the schema default if the key is omitted. A missing key is
    not stored as ``None``.

    ``event_key`` is the key listeners see. It differs from ``key`` when a
    settings field is stored under another name.
    """

    def _replace(_current: Any) -> Any:
        return value

    _config_store.update(key, _replace, event_key=event_key)


def set_configs(values: dict[str, Any]) -> None:
    """Set many keys with one load, one validate, one patch, and one event.

    One load, one validate, one patch, one event. Saving each field on its
    own reloads ``writeragent.json``, writes, and emits ``config:changed``
    per field, and a bad value can leave the earlier keys already saved.
    Writing ``to_dict()`` of the whole file also drops keys that dump omits.
    An event when every coerced value already matches disk refreshes the
    sidebar mode combo for no change.

    Each key is coerced and staged with the same dotted-key and omitted-default
    rules as ``set_config``. The dict is validated once. A validation error
    writes nothing and emits nothing. If no staged value changes the file,
    this returns without writing or emitting. The file patch contains only
    the keys that changed. One changed key emits that key. More than one
    emits ``key=""`` (bulk save) after the lock is released. The event also
    carries ``keys`` (every changed name) so a listener can match one key
    inside that bulk save.
    """
    _config_store.update_many(values)


def remove_config(key: str) -> None:
    """Remove a config key. Other keys in the file are left as they were read."""
    config_file_path = _config_path()
    if not config_file_path:
        raise ConfigError("Config path is empty", "CONFIG_PATH_ERROR")
    _config_store.remove(config_file_path, key)


def _get_validated_config_dict() -> dict[str, Any]:
    """Return the full validated config as a dict, using an in-memory cache
    keyed off the file modification time."""
    try:
        config_file_path = _config_path()
    except ConfigError:
        return {}

    if not config_file_path or not os.path.exists(config_file_path):
        return {}

    current_time = time.time()

    # 2-second cache for the mtime check. Snapshot once: a concurrent
    # set_config can set _cache.data to None between a check and a second read.
    cached = _cache.data
    if cached is not None and (current_time - _cache.mtime_last_checked) < 2.0:
        return cached

    # Load/repair/coerce may persist; serialize with set_config and re-check
    # cache after waiting so we do not rewrite a file another thread just saved.
    with _config_write_lock:
        current_time = time.time()
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
            data = _load_config_dict(config_file_path, allow_repair=True, persist_repair=True, fail_on_unrepairable=True)

            if not isinstance(data, dict):
                raise ConfigError("Config must be a JSON object", "CONFIG_INVALID_FORMAT")

            try:
                current_mtime = os.path.getmtime(config_file_path)
            except OSError:
                current_mtime = 0

            # One out-of-range field used to raise ConfigValidationError, which the
            # ConfigError handler below turned into {} — a later set_config then
            # rewrote the file with only the new key. Coerce and persist so the
            # rest of the file (API keys included) is kept. set_config still
            # validates strictly so the UI can reject a bad new value.
            # Snapshot before validate(). It upgrades calc_prompt_max_tokens < 100
            # in memory and may coerce other fields that alias this dict.
            loaded = copy.deepcopy(data)
            config = _config_schema.WriterAgentConfig.from_dict(data)
            try:
                config.validate()
            except ConfigValidationError as e:
                log.warning("Config has out-of-range values (%s); coercing to in-range defaults", e)
                config.validate(coerce_out_of_range=True)

            # One compare patches only values validate() changed (including
            # calc_prompt_max_tokens below 100 rewritten to 4096) and leaves a
            # file that already matches untouched. A second parse-and-write,
            # or persisting to_dict(), drops every key the schema omits.
            data = _config_store.persist_load_repairs(config_file_path, loaded, config)
            if data is not loaded:
                try:
                    current_mtime = os.path.getmtime(config_file_path)
                except OSError:
                    pass

            out = _build_validated_config_export(data, config)

            _cache.data = out
            _cache.mtime = current_mtime
            return out
        except ConfigError:
            log.exception("Config error reading %s", config_file_path)
            # Keep the last good snapshot. Caching {} made the session look
            # like a fresh install (empty API keys) until the file was fixed.
            if isinstance(_cache.data, dict):
                return _cache.data
            return {}
        except OSError:
            log.exception("Error reading %s", config_file_path)
            return {}


# --- Per-endpoint API keys ---


def _merge_api_key_slots(current: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """Return *current* with only the URL slots in *patch* replaced.

    ``ConfigStore.apply`` calls this on the value just read under
    ``_config_write_lock``. Slots absent from *patch* stay as they were read.
    Callers pass only the slots they are setting. Replacing the map with a
    copy taken before the lock drops a key written for another endpoint
    between that copy and the replace.
    """
    base = dict(current) if isinstance(current, dict) else {}
    for slot, secret in patch.items():
        base[str(slot)] = "" if secret is None else str(secret)
    return base


def update_config_mapping(key: str, mutate: Callable[[dict[str, Any]], None], *, event_key: str | None = None) -> None:
    """Read-modify-write one dict config value through ``ConfigStore.update``.

    ``update_config`` calls ``mutate`` on the value just read, under the same
    lock as the patch. Editing a copy taken outside that lock, then replacing
    the whole value, drops a key another writer stored in between.
    """

    def _apply(current: Any) -> dict[str, Any]:
        data = dict(current) if isinstance(current, dict) else {}
        mutate(data)
        return data

    update_config(key, _apply, event_key=event_key)


def get_api_key_for_endpoint(endpoint: Any) -> str:
    """Return API key for the given endpoint."""
    data = get_config("api_keys_by_endpoint")
    if not isinstance(data, dict):
        data = {}
    normalized = normalize_endpoint_url(endpoint or "")
    return data.get(normalized) or ""


def set_api_key_for_endpoint(endpoint: Any, key: Any, *, event_key: str | None = None) -> None:
    """Store API key for the given endpoint in api_keys_by_endpoint."""

    def _put(data: dict[str, Any]) -> None:
        normalized = normalize_endpoint_url(endpoint or "")
        data[normalized] = str(key)

    update_config_mapping("api_keys_by_endpoint", _put, event_key=event_key)


# --- Bundled API config ---


def get_api_config() -> dict[str, Any]:
    """Build API config dict for LlmClient. Pass to LlmClient(config, ctx)."""
    from plugin.framework.client.model_fetcher import get_text_model

    endpoint = str(get_config("endpoint") or "").rstrip("/")

    # Local import to avoid circular import during early UNO registration
    # (config → client/provider_detection → client/__init__ → llm_client → logging → config)
    from plugin.framework.client.provider_detection import is_openrouter_endpoint, is_openwebui_endpoint

    # Hostname helpers, not a substring of the whole URL. A path that merely
    # contains "openwebui" or "openrouter.ai" is not that product.
    is_openwebui = is_openwebui_endpoint(endpoint, explicit_is_openwebui=_config_schema.as_bool(get_config("is_openwebui")))
    is_openrouter = is_openrouter_endpoint(endpoint, explicit_is_openrouter=_config_schema.as_bool(get_config("is_openrouter")))
    api_key = get_api_key_for_endpoint(endpoint)

    api_config: dict[str, Any] = {
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


def validate_api_config(config: Any) -> tuple[bool, str]:
    """Validate API config dict (from get_api_config). Returns (ok: bool, error_message: str)."""
    from plugin.framework.i18n import _

    endpoint = (config.get("endpoint") or "").strip()
    if not endpoint:
        return (False, _("Please set Endpoint in Settings."))
    model = (config.get("model") or "").strip()
    if not model:
        return (False, _("Please set Model in Settings."))
    try:
        from plugin.chatbot.config_ui_helpers import _is_model_combobox_placeholder
    except ImportError:
        # LibrePy has no chat model combobox; skip placeholder rejection.
        return (True, "")

    if _is_model_combobox_placeholder(model):
        return (False, _("Please select a valid model in Settings (not a placeholder)."))
    return (True, "")
