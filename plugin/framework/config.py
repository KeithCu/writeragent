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
the config path is cached. All other I/O — ``get_config``, ``set_config``, typed
getters, ``get_api_config`` — does **not** take ``ctx``; use ``get_ctx()`` only for
UNO operations.

``writeragent.json`` lives under the LibreOffice user profile (Linux:
``~/.config/libreoffice/{4,24}/user/``; macOS: ``~/Library/Application Support/LibreOffice/4/user/``;
Windows: ``%APPDATA%\\LibreOffice\\4\\user\\``). LibrePy shares this same file on
purpose (venv path, session mode, timeouts). Broken JSON is copied to
``.bak`` when possible; ``json_repair`` fixes small typos on read.

Writes omit keys that still match defaults and prefix the file with ``//``
comment lines pointing at ``docs/writeragent-config-schema.md`` on GitHub.
Those comments are stripped on read.

Concurrency: workers and the UI both read and write ``writeragent.json``.
``set_config`` / ``remove_config`` and **GET-path** persists (repairing
broken JSON, coercing out-of-range numbers, upgrading old
``calc_prompt_max_tokens``) share ``_config_write_lock`` (an ``RLock`` so
a helper already holding it can persist). Without that lock a
background ``get_config`` could rewrite an older file over a key the UI
just saved. The ``config:changed`` event is emitted **after** the lock
is released so listeners may call ``get_config`` / ``set_config`` without
deadlocking.

Schema-backed coercion, option canonicalization, and min/max bounds live in
``config_schema.py``. Import those names from there. This module is path,
cache, and JSON I/O only. Do not import this file from ``config_schema.py``.
"""

# crosshair: off
import dataclasses
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Dict

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
CONFIG_SCHEMA_DOC_URL = (
    "https://github.com/KeithCu/writeragent/blob/master/docs/writeragent-config-schema.md"
)
CONFIG_SCHEMA_COMMENT = (
    "// Only settings that differ from defaults are stored here.\n"
    "// Full schema: " + CONFIG_SCHEMA_DOC_URL + "\n"
)

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

# --- Module constants ---

CONFIG_FILENAME = "writeragent.json"
CONFIG_BACKUP_SUFFIX = ".bak"

# Max items for all LRU lists; base names also listed in _LRU_LIST_CONFIG_KEY_PREFIXES for get_config defaults.
LRU_MAX_ITEMS = 10
# Simple AI settings fields that the Tools → Options "AI" page should map
# directly to top-level config keys (endpoint, model, etc.).
AI_SIMPLE_FIELDS = {"endpoint", "text_model", "image_model", "stt_model", "temperature", "chat_max_tokens", "request_timeout", "additional_instructions", "parallel_tool_calls"}

_resolved_config_path = None
# RLock: set_config holds this while loading; GET-path persist helpers take it
# too. Same-thread get_config during a nested call must not deadlock.
_config_write_lock = threading.RLock()


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
    if ctx is not None:
        try:
            from plugin.framework.queue_executor import default_executor

            default_executor.set_context(ctx)
        except Exception:
            pass
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


def parse_config_json_text(text: str) -> dict | None:
    """Parse writeragent.json text, ignoring the optional ``//`` schema header."""
    return _try_parse_config_dict(text)


def _try_parse_config_dict(text: str) -> dict | None:
    try:
        data = json.loads(_strip_config_comment_header(text))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _try_repair_config_dict(text: str) -> dict | None:
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
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return None


def _write_config_file(config_file_path: str, data: dict) -> None:
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
                    # GET-path persist must serialize with set_config (RLock if nested).
                    with _config_write_lock:
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
    from plugin.framework.uno_context import is_libreharper

    if is_libreharper():
        return True

    val = get_config("doc.grammar_proofreader_enabled")
    if isinstance(val, bool):
        return val  # Handle old boolean config
    val_str = str(val).strip().lower()
    return val_str in ("llm", "languagetool", "vale", "harper", "true")


def get_grammar_provider():
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


def get_config(key):
    """Get a config value by key. JSON overrides; when key is missing, use schema default then central fallback."""
    config_data = _get_validated_config_dict()
    if not isinstance(config_data, dict):
        config_data = {}

    if key in config_data:
        return config_data[key]

    for dotted in _config_schema._dotted_fallback_keys(key):
        if dotted in config_data:
            return config_data[dotted]

    return _config_schema._resolve_default(key)


def get_config_int(key) -> int:
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


def get_config_float_safe(key: str) -> float:
    """Safely read a float config value. Unlike get_config_float, this returns the schema default (or 0.0) rather than raising an exception if the key is missing or the value is invalid."""
    try:
        return get_config_float(key)
    except Exception:
        try:
            return _config_schema.parse_float_robust(_config_schema._resolve_default(key))
        except Exception:
            return 0.0


def get_config_float(key) -> float:
    """Get a config value as float. ALL requested keys MUST be in the schema.
    Throws ConfigError if key is not found or value is non-float (use get_config_float_safe to return a default instead)."""
    v = get_config(key)
    if v == "" or v is None:
        v = _config_schema._resolve_default(key)
    if v == "":
        raise ConfigError(f"Missing config key {key!r}: not a WriterAgentConfig field, MODULES default, or LRU pattern.", "CONFIG_KEY_NOT_FOUND", details={"key": key})
    try:
        return _config_schema.parse_float_robust(v)
    except ValueError as e:
        raise ConfigError(f"Config key {key!r} has non-float value: {v!r}", "CONFIG_TYPE_ERROR") from e


def get_config_dict():
    """Return the full config as a dict. Returns {} if missing or on error."""
    return _get_validated_config_dict()


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


def set_config(key, value):
    """Set a config key to value. Creates file if needed. Omits defaults."""
    try:
        config_file_path = _config_path()
    except ConfigError:
        log.warning("set_config skipped: config path could not be resolved")
        return

    if not config_file_path:
        log.warning("set_config skipped: empty config path")
        return
    emit_changed = False
    with _config_write_lock:
        if os.path.exists(config_file_path):
            config_data = _load_config_dict(config_file_path, allow_repair=True, persist_repair=False)
        else:
            config_data = {}
        current_value = _raw_config_value_for_key(config_data, key)
        value = _config_schema.coerce_config_value(key, value, fallback_value=current_value)
        if config_data.get(key) == value:
            return

        test_data = dict(config_data)
        for dotted in _config_schema._dotted_fallback_keys(key):
            test_data.pop(dotted, None)
        if "." in key:
            test_data.pop(key.split(".", 1)[1], None)
        test_data[key] = value

        try:
            test_config = _config_schema.WriterAgentConfig.from_dict(test_data)
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
            emit_changed = True
        except OSError as e:
            log.exception("Error writing to %s", config_file_path)
            raise ConfigError(f"Failed to save config: {e}", "CONFIG_SAVE_ERROR") from e
    # Handlers may get_config/set_config; do not hold the write lock across emit.
    if emit_changed:
        global_event_bus.emit("config:changed", ctx=_emit_config_changed_ctx())


def remove_config(key):
    """Remove a config key."""
    try:
        config_file_path = _config_path()
    except ConfigError:
        log.warning("remove_config skipped: config path could not be resolved")
        return

    if not config_file_path:
        log.warning("remove_config skipped: empty config path")
        return
    if not os.path.exists(config_file_path):
        return
    emit_changed = False
    with _config_write_lock:
        try:
            with open(config_file_path, "r", encoding="utf-8") as f:
                config_data = parse_config_json_text(f.read())
            if not isinstance(config_data, dict):
                return
        except OSError:
            return
        removed = False
        if key in config_data:
            config_data.pop(key, None)
            removed = True
        for dotted in list(_config_schema._dotted_fallback_keys(key)):
            if dotted in config_data:
                config_data.pop(dotted, None)
                removed = True
        if "." in key:
            field_name = key.split(".", 1)[1]
            if field_name in config_data:
                config_data.pop(field_name, None)
                removed = True
        if not removed:
            return

        try:
            test_config = _config_schema.WriterAgentConfig.from_dict(config_data)
            test_config.validate()
            config_data = test_config.to_dict()
        except ConfigValidationError as e:
            log.warning("remove_config skipped write: remaining config is invalid: %s", e)
            return
        except Exception:
            log.exception("remove_config validation failed; not writing unvalidated dict")
            return

        try:
            _write_config_file(config_file_path, config_data)
            _invalidate_config_cache()
            emit_changed = True
        except OSError as e:
            log.exception("Error writing to %s", config_file_path)
            raise ConfigError(f"Failed to remove config key: {e}", "CONFIG_SAVE_ERROR") from e
    if emit_changed:
        global_event_bus.emit("config:changed", ctx=_emit_config_changed_ctx())


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
            data = _load_config_dict(config_file_path, allow_repair=True, persist_repair=True)

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
            config = _config_schema.WriterAgentConfig.from_dict(data)
            try:
                config.validate()
            except ConfigValidationError as e:
                log.warning("Config has out-of-range values (%s); coercing to in-range defaults", e)
                config.validate(coerce_out_of_range=True)
                try:
                    repaired = config.to_dict()
                    _write_config_file(config_file_path, repaired)
                    data = repaired
                    try:
                        current_mtime = os.path.getmtime(config_file_path)
                    except OSError:
                        pass
                except OSError as write_err:
                    log.warning("Failed to persist coerced config: %s", write_err)

            out = _build_validated_config_export(data, config)

            # Persist stale calc_prompt_max_tokens upgrade (old default 70 → 4096).
            raw_prompt_tokens = data.get("calc_prompt_max_tokens")
            try:
                raw_int = _config_schema.parse_int_robust(raw_prompt_tokens) if raw_prompt_tokens is not None and raw_prompt_tokens != "" else None
            except ValueError:
                raw_int = None
            if raw_int is not None and raw_int < 100:
                file_data = dict(data)
                file_data.pop("calc_prompt_max_tokens", None)
                cleaned_config = _config_schema.WriterAgentConfig.from_dict(file_data)
                cleaned_config.validate()
                cleaned_file_data = cleaned_config.to_dict()
                try:
                    _write_config_file(config_file_path, cleaned_file_data)
                    try:
                        current_mtime = os.path.getmtime(config_file_path)
                    except OSError:
                        pass
                    log.info("Persisted calc_prompt_max_tokens upgrade (%s → default 4096)", raw_int)
                except OSError as e:
                    log.warning("Failed to persist calc_prompt_max_tokens upgrade: %s", e)

            _cache.data = out
            _cache.mtime = current_mtime
            return out
        except ConfigError:
            log.exception("Config error reading %s", config_file_path)
            return {}
        except OSError:
            log.exception("Error reading %s", config_file_path)
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
    else:
        # Copy: get_config returns the live cache; mutating it would leak an
        # unpersisted key into memory if the write below fails.
        data = dict(data)
    normalized = normalize_endpoint_url(endpoint or "")
    data[normalized] = str(key)
    set_config("api_keys_by_endpoint", data)


# --- Bundled API config ---


def get_api_config():
    """Build API config dict for LlmClient. Pass to LlmClient(config, ctx)."""
    from plugin.framework.client.model_fetcher import get_text_model

    endpoint = str(get_config("endpoint") or "").rstrip("/")
    is_openwebui = _config_schema.as_bool(get_config("is_openwebui")) or "open-webui" in endpoint.lower() or "openwebui" in endpoint.lower()

    # Local import to avoid circular import during early UNO registration
    # (config → client/provider_detection → client/__init__ → llm_client → logging → config)
    from plugin.framework.client.provider_detection import is_openrouter_endpoint

    # Use the consolidated detection helper (2026 provider heuristic cleanup)
    # so the OpenRouter decision is identical everywhere (auth, model fetcher,
    # error messages, LLM client, etc.).
    is_openrouter = is_openrouter_endpoint(endpoint, explicit_is_openrouter=_config_schema.as_bool(get_config("is_openrouter")))
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
    try:
        from plugin.chatbot.config_ui_helpers import _is_model_combobox_placeholder
    except ImportError:
        # LibrePy has no chat model combobox; skip placeholder rejection.
        return (True, "")

    if _is_model_combobox_placeholder(model):
        return (False, _("Please select a valid model in Settings (not a placeholder)."))
    return (True, "")

