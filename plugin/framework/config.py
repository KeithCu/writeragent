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
the config path is cached. All other I/O — ``get_config``, ``get_config_dict``,
``save``, ``get_api_config`` — does **not** take ``ctx``; use ``get_ctx()`` only for
UNO operations.

``writeragent.json`` lives under the LibreOffice user profile (Linux:
``~/.config/libreoffice/{4,24}/user/``; macOS: ``~/Library/Application Support/LibreOffice/4/user/``;
Windows: ``%APPDATA%\\LibreOffice\\4\\user\\``). LibrePy shares this same file on
purpose (venv path, session mode, timeouts). One ``.bak`` is kept per
distinct corrupt byte string; a different corrupt body adds one stamped file
and does not overwrite the first ``.bak``. ``json_repair`` fixes small
typos on read.

Writes omit a key that still matches its default and prefix the file with
``//`` comment lines pointing at ``docs/writeragent-config-schema.md`` on
GitHub. Those comments are stripped on read. ``save`` does not validate,
write, or emit ``config:changed`` when the coerced value already matches the
stored value, or the schema default when that key is omitted from the file.
The default itself is not a change. If nothing changed, it does not write or emit.

Concurrency: ``save`` and ``update_config_mapping`` hold ``_config_write_lock``
(an ``RLock``) across the read, merge, validate, and write. Values for
``api_keys_by_endpoint`` or ``reasoning_effort`` are slot patches merged into
the map read under the lock. One ``config:changed`` is emitted **after** the
lock is released. A batch that changes one key emits that key. A batch that
changes more than one emits ``key=""``.

A failed load whose ``(path, st_mtime_ns, st_size)`` is unchanged is not
parsed or warned again, and ``{}`` is not cached. ``get_config`` before
``init_config`` returns schema defaults and logs one warning.
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
from typing import Any, Callable, Dict, TypeVar

_T = TypeVar("_T", int, float)
_S = TypeVar("_S")

from plugin.framework.errors import ConfigError, ConfigValidationError, uno_call
from plugin.framework.event_bus import get_event_bus
from plugin.framework.json_utils import repair_json
from plugin.framework.url_utils import normalize_endpoint_url

from plugin.framework import config_schema as _config_schema

# Comment header written above the JSON object. Not a config key.
CONFIG_SCHEMA_COMMENT = (
    "// Only settings that differ from defaults are stored here.\n"
    "// Full schema: https://github.com/KeithCu/writeragent/blob/master/docs/writeragent-config-schema.md\n"
)

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

_resolved_config_path = None
_config_uninitialized_warned: bool = False
# RLock: save holds this while loading; GET-path persist helpers take it
# too. Same-thread get_config during a nested call must not deadlock.
_config_write_lock = threading.RLock()


def _resolve_config_path_from_ctx(ctx: Any) -> str:
    """Resolve writeragent.json path from a UNO component context."""
    sm = uno_call(ctx.getServiceManager, "Get ServiceManager")
    path_settings = uno_call(lambda: sm.createInstanceWithContext("com.sun.star.util.PathSettings", ctx), "Create PathSettings")
    user_config_path = getattr(path_settings, "UserConfig", "")
    if uno and user_config_path and str(user_config_path).startswith("file://"):
        user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
    if not isinstance(user_config_path, str) or not user_config_path.strip():
        raise ConfigError("Invalid or missing UserConfig path setting", code="CONFIG_PATH_ERROR")
    return os.path.join(user_config_path, CONFIG_FILENAME)


def init_config(ctx: Any | None = None) -> str:
    """Resolve and cache writeragent.json path. Idempotent; call once at bootstrap."""
    global _resolved_config_path
    if ctx is not None:
        from plugin.framework.queue_executor import main

        main.set_context(ctx)
    if _resolved_config_path is not None:
        return _resolved_config_path
    if ctx is None:
        from plugin.framework.queue_executor import main

        if not main.is_current():
            raise ConfigError("UNO context is required to resolve config path on background thread")
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
    if ctx is None:
        raise ConfigError("UNO context is required to resolve config path")
    _resolved_config_path = _resolve_config_path_from_ctx(ctx)
    return _resolved_config_path


def reset_config_for_tests() -> None:
    """Clear cached config path and in-memory dict (pytest isolation)."""
    global _resolved_config_path, _config_uninitialized_warned
    _resolved_config_path = None
    _config_uninitialized_warned = False
    _invalidate_config_cache()


def get_config_file_path() -> str:
    """Return the absolute path to writeragent.json."""
    if _resolved_config_path is not None:
        return _resolved_config_path
    return init_config()


def user_config_dir() -> str | None:
    """Return LibreOffice user config directory."""
    return os.path.dirname(get_config_file_path())


def _backup_config_file(config_file_path: str, *, reason: str = "invalid-json") -> str | None:
    """Copy the raw config file before repair or other destructive handling."""
    # Reuses an existing backup if it already matches the source bytes.
    if not config_file_path or not os.path.exists(config_file_path):
        return None
    try:
        with open(config_file_path, "rb") as f:
            src_bytes = f.read()
    except OSError:
        return None

    backup_path = config_file_path + CONFIG_BACKUP_SUFFIX
    backup_dir = os.path.dirname(backup_path) or "."
    backup_base = os.path.basename(backup_path)
    try:
        entries = os.listdir(backup_dir)
    except OSError:
        entries = []
    candidates = [backup_path]
    candidates.extend(
        os.path.join(backup_dir, name)
        for name in sorted(entry for entry in entries if entry.startswith(backup_base + "."))
    )
    for cand_path in candidates:
        try:
            with open(cand_path, "rb") as f:
                if f.read() == src_bytes:
                    return cand_path
        except OSError:
            continue

    dest_path = backup_path
    if os.path.exists(backup_path):
        stamped = backup_path + "." + time.strftime("%Y%m%d%H%M%S")
        if os.path.exists(stamped):
            stamped = stamped + "." + str(time.time_ns())
        dest_path = stamped
    try:
        shutil.copy2(config_file_path, dest_path)
        log.warning("Backed up config %s to %s (%s)", config_file_path, dest_path, reason)
        return dest_path
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
        log.debug("config JSON repair failed", exc_info=True)

    return None


def atomic_write_text(path: str, text: str, *, prefix: str = ".tmp-", fsync: bool = False) -> None:
    """Atomically write text to path using a temp file in the same directory and os.replace."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_config_file(config_file_path: str, data: dict[str, Any]) -> None:
    """Write config via temp file + ``os.replace`` so a crash cannot truncate the live file."""
    # Non-serializable values raise ConfigError with CONFIG_SAVE_ERROR.
    try:
        body = json.dumps(data, indent=4)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Config value is not JSON-serializable: {e}", code="CONFIG_SAVE_ERROR") from e
    body += "\n"
    content = CONFIG_SCHEMA_COMMENT + body
    atomic_write_text(config_file_path, content, prefix=".writeragent-", fsync=True)


def _invalidate_config_cache() -> None:
    _cache.data = None
    _cache.mtime = 0
    _cache.mtime_last_checked = 0.0
    _cache.error_logged = None
    _cache.unreadable = None


def _commit_config_write(path: str, data: dict[str, Any], *, failed: str) -> None:
    # Catch OSError only. _write_config_file raises ConfigError for a non-JSON value; wrapping that changes the message.
    try:
        _write_config_file(path, data)
        _invalidate_config_cache()
    except OSError as e:
        log.exception("Error writing to %s", path)
        raise ConfigError(f"{failed}: {e}", code="CONFIG_SAVE_ERROR") from e


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
    error_logged: tuple[str, int, int] | tuple[str, None, None] | None = None
    # Skips repeated re-reads of an unreadable file until mtime or size changes.
    unreadable: tuple[str, int, int] | None = None


_cache = ConfigCache()


def _config_stat_id(path: str) -> tuple[str, int, int] | None:
    try:
        st = os.stat(path)
        return (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _remember_config_read_failure(path: str, stat_id: tuple[str, int, int] | None) -> None:
    if stat_id is not None:
        _cache.unreadable = stat_id
        log_id: tuple[str, int, int] | tuple[str, None, None] = stat_id
    else:
        log_id = (path, None, None)
    if _cache.error_logged != log_id:
        log.exception("Config error reading %s", path)
        _cache.error_logged = log_id


def _copy_config_value(value: Any) -> Any:
    """Return a copy of dict/list config values."""
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    return value


def _load(config_file_path: str | None = None, *, fail_on_unrepairable: bool = False) -> dict[str, Any]:
    """Load writeragent.json, rename legacy keys, repair, persist if changed, and cache."""
    global _config_uninitialized_warned
    if config_file_path is None:
        try:
            config_file_path = get_config_file_path()
        except ConfigError as exc:
            if not _config_uninitialized_warned:
                log.warning("Config read before init_config (%s); using defaults", exc)
                _config_uninitialized_warned = True
            return {}

    if not config_file_path or not os.path.exists(config_file_path):
        return {}

    current_time = time.time()
    cached = _cache.data
    if cached is not None and (current_time - _cache.mtime_last_checked) < 2.0:
        return cached

    with _config_write_lock:
        current_time = time.time()
        if _cache.data is not None and (current_time - _cache.mtime_last_checked) < 2.0:
            return _cache.data

        stat_id = _config_stat_id(config_file_path)
        try:
            current_mtime = os.path.getmtime(config_file_path)
        except OSError:
            current_mtime = 0

        _cache.mtime_last_checked = current_time

        if stat_id is not None and stat_id == _cache.unreadable:
            if fail_on_unrepairable:
                raise ConfigError(
                    f"Invalid JSON in {config_file_path} could not be repaired",
                    code="CONFIG_INVALID_FORMAT",
                    details={"path": config_file_path},
                )
            return _cache.data if isinstance(_cache.data, dict) else {}

        if _cache.data is not None and current_mtime == _cache.mtime and current_mtime != 0:
            return _cache.data

        try:
            with open(config_file_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            _remember_config_read_failure(config_file_path, stat_id)
            return _cache.data if isinstance(_cache.data, dict) else {}

        data = parse_config_json_text(text)
        repaired_json = False
        if data is None:
            backup_path = _backup_config_file(config_file_path, reason="invalid-json")
            data = _try_repair_config_dict(text)
            if data is None:
                _remember_config_read_failure(config_file_path, stat_id)
                if fail_on_unrepairable:
                    raise ConfigError(
                        f"Invalid JSON in {config_file_path} could not be repaired",
                        code="CONFIG_INVALID_FORMAT",
                        details={"path": config_file_path, "backup_path": backup_path},
                    )
                log.warning("Invalid JSON in %s could not be auto-repaired (backup: %s)", config_file_path, backup_path or "none")
                return _cache.data if isinstance(_cache.data, dict) else {}
            log.info("Auto-repaired invalid JSON in %s (backup: %s)", config_file_path, backup_path)
            repaired_json = True

        raw_loaded = copy.deepcopy(data)

        # Renames, once, in load
        if "model" in data:
            m_val = str(data.pop("model") or "").strip()
            if m_val and not str(data.get("text_model") or "").strip():
                data["text_model"] = m_val

        if "stt_model" in data:
            flat_stt = data.pop("stt_model")
            dotted_stt = data.get("audio.stt_model")
            if not (isinstance(dotted_stt, str) and dotted_stt.strip()):
                if flat_stt:
                    data["audio.stt_model"] = flat_stt

        for flat_key, full_key in _config_schema.FLAT_TO_MODULE_KEY.items():
            if flat_key in ("stt_model", "model"):
                continue
            if flat_key in data:
                flat_v = data.pop(flat_key)
                if full_key not in data:
                    data[full_key] = flat_v

        # Repair: validate each known key; on error use field fallback or default
        repaired: dict[str, Any] = {}
        for k, v in data.items():
            if _config_schema.is_known_config_key(k):
                try:
                    single = _config_schema.validate({k: v, "is_openwebui": data.get("is_openwebui", False)})
                    repaired[k] = single[k]
                except ConfigValidationError as exc:
                    schema = _config_schema.get_config_schema(k) or {}
                    fallback = None
                    if getattr(exc, "details", {}).get("reason") == "parse":
                        fallback = schema.get("parse_fallback")
                    if fallback is None:
                        fallback = schema.get("fallback")
                    if fallback is None:
                        fallback = schema.get("default")
                    log.warning("%s %r out of range or invalid; using %r (%s)", k, v, fallback, exc)
                    repaired[k] = _copy_config_value(fallback)
            else:
                repaired[k] = v

        validated = _config_schema.validate(repaired)

        to_persist: dict[str, Any] = {}
        for k, v in validated.items():
            if not _config_schema.is_default_value(k, v):
                to_persist[k] = v

        if repaired_json or to_persist != raw_loaded:
            try:
                _write_config_file(config_file_path, to_persist)
                try:
                    current_mtime = os.path.getmtime(config_file_path)
                except OSError:
                    pass
            except (OSError, ConfigError) as write_err:
                log.warning("Failed to persist coerced config: %s", write_err)

        _cache.data = to_persist
        _cache.mtime = current_mtime
        _cache.error_logged = None
        _cache.unreadable = None
        return to_persist


def get_config(key: str) -> Any:
    """Get a config value by key. Reads cache or schema default. Unknown key raises."""
    cached = _load()
    if key in cached:
        return _copy_config_value(cached[key])
    default = _config_schema._resolve_default(key)
    return _copy_config_value(default)


def get_config_dict() -> dict[str, Any]:
    """Return a deep copy of the sparse coerced file."""
    cached = _load()
    return {k: _copy_config_value(v) for k, v in cached.items()}


def _emit_pending(
    effective: list[tuple[str, Any, Any]],
    *,
    event_key: str | None = None,
) -> None:
    """Emit one config:changed. Caller has already released the write lock."""
    from plugin.framework.queue_executor import main
    from plugin.framework.uno_context import get_ctx

    ctx = get_ctx() if main.is_current() else None
    keys = tuple(k for k, _new, _old in effective)
    if len(effective) == 1:
        changed, value, previous = effective[0]
        emit_key = event_key or changed
        payload: dict[str, Any] = {
            "key": emit_key,
            "value": value,
            "old_value": previous,
            "ctx": ctx,
            "keys": (emit_key,),
        }
    else:
        payload = {
            "key": "",
            "keys": keys,
            "value": None,
            "old_value": None,
            "ctx": ctx,
        }
    get_event_bus().emit("config:changed", **payload)


def save(patch: dict[str, Any], *, event_key: str | None = None) -> None:
    """Save a config patch. Validate, drop defaults, write, invalidate, emit."""
    if not patch:
        return
    path = get_config_file_path()
    if not path:
        raise ConfigError("Config path is empty", code="CONFIG_PATH_ERROR")

    effective: list[tuple[str, Any, Any]] = []
    with _config_write_lock:
        loaded = _load(path, fail_on_unrepairable=True)
        merged = dict(loaded)
        for k, v in patch.items():
            if k == "api_keys_by_endpoint" and isinstance(v, dict):
                merged[k] = _merge_api_key_slots(merged.get(k, {}), v)
            elif k == "reasoning_effort" and isinstance(v, dict):
                merged[k] = _merge_reasoning_effort_slots(merged.get(k, {}), v)
            else:
                merged[k] = v

        validated = _config_schema.validate(merged)

        to_write: dict[str, Any] = {}
        for k, v in validated.items():
            if not _config_schema.is_default_value(k, v):
                to_write[k] = v

        for k in patch:
            old_val = loaded[k] if k in loaded else (_config_schema._resolve_default(k) if _config_schema.is_known_config_key(k) else None)
            new_val = to_write[k] if k in to_write else (_config_schema._resolve_default(k) if _config_schema.is_known_config_key(k) else None)
            if old_val != new_val or (k in loaded) != (k in to_write):
                effective.append((k, new_val, old_val))

        if not effective or to_write == loaded:
            return

        _commit_config_write(path, to_write, failed="Failed to save config")

    _emit_pending(effective, event_key=event_key)


# --- Per-endpoint API keys ---


def _merge_api_key_slots(current: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """Return *current* with only the URL slots in *patch* replaced."""
    base = dict(current) if isinstance(current, dict) else {}
    for slot, secret in patch.items():
        base[str(slot)] = "" if secret is None else str(secret)
    return base


def _merge_reasoning_effort_slots(current: Any, patch: dict[str, Any]) -> dict[str, Any]:
    base = dict(current) if isinstance(current, dict) else {}
    for slot, raw in patch.items():
        text = "" if raw is None else str(raw).strip()
        key = str(slot)
        if not text:
            base.pop(key, None)
        else:
            base[key] = text
    return base


def update_config_mapping(key: str, mutate: Callable[[Any], Any], *, event_key: str | None = None) -> None:
    """Read-modify-write one dict config value under the write lock."""
    with _config_write_lock:
        current = get_config(key)
        data = dict(current) if isinstance(current, dict) else {}
        result = mutate(data)
        if isinstance(result, dict):
            data = result
        save({key: data}, event_key=event_key)


def get_api_key_for_endpoint(endpoint: Any) -> str:
    """Return API key for the given endpoint."""
    data = get_config("api_keys_by_endpoint")
    if not isinstance(data, dict):
        data = {}
    normalized = normalize_endpoint_url(endpoint or "")
    return data.get(normalized) or ""


def set_api_key_for_endpoint(endpoint: Any, key: Any, *, event_key: str | None = None) -> None:
    """Store API key for the given endpoint in api_keys_by_endpoint."""
    normalized = normalize_endpoint_url(endpoint or "")
    save({"api_keys_by_endpoint": {normalized: str(key)}}, event_key=event_key)


def get_reasoning_effort_for_endpoint(endpoint: Any) -> str:
    data = get_config("reasoning_effort")
    if not isinstance(data, dict):
        return ""
    raw = data.get(normalize_endpoint_url(endpoint or ""))
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def set_reasoning_effort_for_endpoint(endpoint: Any, effort: Any, *, event_key: str | None = None) -> None:
    normalized = normalize_endpoint_url(endpoint or "")
    text = "" if effort is None else str(effort).strip()
    save({"reasoning_effort": {normalized: text}}, event_key=event_key)


# --- Bundled API config ---


def get_api_config() -> dict[str, Any]:
    """Build API config dict for LlmClient. Pass to LlmClient(config, ctx)."""
    from plugin.framework.client.model_fetcher import get_text_model

    endpoint = str(get_config("endpoint") or "").rstrip("/")

    from plugin.framework.client.provider_detection import is_openrouter_endpoint, is_openwebui_endpoint

    is_openwebui = is_openwebui_endpoint(endpoint, explicit_is_openwebui=_config_schema.as_bool(get_config("is_openwebui")))
    is_openrouter = is_openrouter_endpoint(endpoint, explicit_is_openrouter=_config_schema.as_bool(get_config("is_openrouter")))
    api_key = get_api_key_for_endpoint(endpoint)
    effort = get_reasoning_effort_for_endpoint(endpoint)

    api_config: dict[str, Any] = {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": get_text_model(),
        "is_openwebui": is_openwebui,
        "is_openrouter": is_openrouter,
        "seed": get_config("seed"),
        "request_timeout": get_config("request_timeout"),
        "chat_max_tool_rounds": get_config("chatbot.max_tool_rounds"),
    }
    if effort:
        api_config["reasoning_effort"] = effort

    temp = float(get_config("temperature"))
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
        from plugin.framework.model_lru import _is_model_combobox_placeholder
    except ImportError:
        return (True, "")

    if _is_model_combobox_placeholder(model):
        return (False, _("Please select a valid model in Settings (not a placeholder)."))
    return (True, "")
