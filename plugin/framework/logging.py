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
"""Simple file logging for WriterAgent. Single debug log in the LO user config dir (writeragent_debug.log).

Paths are set via init_logging(ctx). No file logging when the config dir is unavailable.
Also: redaction helpers for debug logs that would otherwise embed large base64 (chat multimodal parts, image API JSON) or long opaque ``signature`` blobs.

Concurrency: ``init_logging`` may run from bootstrap while workers already
log. The debug log path, ``enable_agent_log``, the init lock, the activity dict,
the activity lock, and the watchdog hung_shown / stacks_dumped flags live in
``sys._writeragent_logging_shared``, for the same reason as the debug-file handler,
the exception-hook flag, and the watchdog-started flag (``_writeragent_debug_file_handler``,
``_writeragent_exception_hooks_installed``, ``_writeragent_watchdog_started``):
LibreOffice can import this module twice, and a module global is a different
object in each copy. ``_shared`` installs that dict with ``sys.__dict__.setdefault``
on the first miss so two threads cannot replace each other's box. ``_debug_log_flush_lock`` rate-limits ``flush()`` so every log
line does not fsync. Actual line writes go through Python’s ``logging`` handler lock;
you do not need another mutex around ``log.info``.
"""

from __future__ import annotations

import os
import sys
import json
import time
import traceback
import threading
import logging
from copy import deepcopy
from typing import Any, TypedDict, cast

from plugin.framework.errors import ConfigError, format_error_payload
from plugin.framework.json_utils import safe_json_loads
from plugin.framework import config


class _LoggingShared(TypedDict):
    debug_log_path: str | None
    enable_agent_log: bool
    init_lock: threading.Lock
    activity: dict[str, Any]
    activity_lock: threading.Lock
    hung_shown: bool
    stacks_dumped: bool


def _shared() -> _LoggingShared:
    box = getattr(sys, "_writeragent_logging_shared", None)
    if not isinstance(box, dict):
        fresh: dict[str, Any] = {
            "debug_log_path": None,
            "enable_agent_log": False,
            "init_lock": threading.Lock(),
            "activity": {
                "phase": "",
                "round_num": -1,
                "tool_name": None,
                "last_activity": 0.0,
            },
            "activity_lock": threading.Lock(),
            "hung_shown": False,
            "stacks_dumped": False,
        }
        # What was wrong: the later setattr replaced the box init_logging was filling,
        # so debug_log_path stayed None and the locks diverged.
        # How it happened: check-then-set while workers already log.
        # Why this change fixes it: setdefault is one dict operation; the common path
        # returns the existing dict and does not build locks.
        stored = sys.__dict__.setdefault("_writeragent_logging_shared", fresh)
        if isinstance(stored, dict):
            box = stored
        else:
            setattr(sys, "_writeragent_logging_shared", fresh)
            box = fresh
    raw: Any = box
    return cast("_LoggingShared", raw)


log = logging.getLogger("writeragent")
_agent_logger = logging.getLogger("writeragent.agent")
# Parent level must not gate these records; Logger.debug checks the logger's
# own effective level, and a NOTSET child would inherit WARNING from writeragent.
_agent_logger.setLevel(logging.DEBUG)

# Config log_level strings allowed through getattr(logging, ...); others → WARNING.
_LOG_LEVEL_NAMES = frozenset({"CRITICAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "NOTSET"})


def resolve_log_level(level_str: str | None) -> int:
    """Map a config log_level string to a logging level; unknown values → WARNING."""
    name = str(level_str or "WARN").strip().upper()
    if name == "WARN":
        name = "WARNING"
    if name not in _LOG_LEVEL_NAMES:
        return logging.WARNING
    return int(getattr(logging, name))


_watchdog_interval_sec = 15
_watchdog_threshold_sec = 30
_HUNG_STATUS_PREFIX = "Hung:"

DEBUG_LOG_FILENAME = "writeragent_debug.log"

LOG_REDACT_AUDIO_PLACEHOLDER = "<audio base64 data truncated, length=%d>"
LOG_REDACT_IMAGE_PLACEHOLDER = "<image base64 data truncated, length=%d>"
LOG_REDACT_SIGNATURE_PLACEHOLDER = "<signature truncated, length=%d>"
# Image-model reasoning_details[].signature blobs are thousands of chars; keep short values readable.
LOG_REDACT_SIGNATURE_MIN_LEN = 256
# Exact casefolded names, not substrings (max_tokens must survive).
_SECRET_LOG_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "x-api-key",
        "authorization",
        "bearer",
        "password",
        "token",
        "secret",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "private_key",
        "session_token",
        "apikey",
    }
)
_API_KEYS_BY_ENDPOINT = "api_keys_by_endpoint"
LOG_REDACT_SECRET_PLACEHOLDER = "<redacted>"


def _redact_sensitive_inplace(o: Any) -> None:
    """Strip large base64, long signature blobs, and secret dict values from nested API-shaped JSON."""
    if isinstance(o, dict):
        for key, val in list(o.items()):
            if isinstance(key, str) and key.casefold() == _API_KEYS_BY_ENDPOINT and isinstance(val, dict):
                # api_keys_by_endpoint is a URL→raw key map. An exact-name
                # string check leaves every key in the debug log. Replace
                # each string value; non-strings stay for the walk below.
                for inner_key, inner_val in list(val.items()):
                    if isinstance(inner_val, str):
                        val[inner_key] = LOG_REDACT_SECRET_PLACEHOLDER
            elif isinstance(key, str) and key.casefold() in _SECRET_LOG_KEYS and isinstance(val, str):
                o[key] = LOG_REDACT_SECRET_PLACEHOLDER
        if o.get("type") == "input_audio":
            ia = o.get("input_audio")
            if isinstance(ia, dict) and isinstance(ia.get("data"), str):
                ia["data"] = LOG_REDACT_AUDIO_PLACEHOLDER % len(ia["data"])
        if o.get("type") == "image_url":
            iu = o.get("image_url")
            if isinstance(iu, dict) and isinstance(iu.get("url"), str) and iu["url"].startswith("data:image"):
                iu["url"] = LOG_REDACT_IMAGE_PLACEHOLDER % len(iu["url"])
        iu_top = o.get("image_url")
        if isinstance(iu_top, str) and iu_top.startswith("data:image"):
            o["image_url"] = LOG_REDACT_IMAGE_PLACEHOLDER % len(iu_top)
        bj = o.get("b64_json")
        if isinstance(bj, str):
            o["b64_json"] = LOG_REDACT_IMAGE_PLACEHOLDER % len(bj)
        u = o.get("url")
        if isinstance(u, str) and u.startswith("data:image"):
            o["url"] = LOG_REDACT_IMAGE_PLACEHOLDER % len(u)
        sig = o.get("signature")
        if isinstance(sig, str) and len(sig) >= LOG_REDACT_SIGNATURE_MIN_LEN:
            o["signature"] = LOG_REDACT_SIGNATURE_PLACEHOLDER % len(sig)
        if o.get("type") == "image":
            source = o.get("source")
            if isinstance(source, dict) and source.get("type") == "base64" and isinstance(source.get("data"), str):
                source["data"] = LOG_REDACT_IMAGE_PLACEHOLDER % len(source["data"])
        inline_data = o.get("inlineData")
        if isinstance(inline_data, dict) and isinstance(inline_data.get("data"), str):
            inline_data["data"] = LOG_REDACT_IMAGE_PLACEHOLDER % len(inline_data["data"])
        for v in o.values():
            _redact_sensitive_inplace(v)
    elif isinstance(o, list):
        for item in o:
            _redact_sensitive_inplace(item)


FLUSH_INTERVAL_SEC = 1.0


def _debug_log_max_bytes() -> int:
    # Tip: CI mock runs set WRITERAGENT_DEBUG_LOG_MAX_MB so a long run keeps early tests.
    try:
        mb = int(os.environ.get("WRITERAGENT_DEBUG_LOG_MAX_MB", "") or 5)
    except ValueError:
        mb = 5
    return max(1, mb) * 1024 * 1024


_MAX_DEBUG_LOG_BYTES = _debug_log_max_bytes()
_debug_log_flush_lock = threading.Lock()
_debug_log_last_flush = 0.0
# Import-time binding so tests that patch time.monotonic (e.g. LLM pacing) are not affected by flush rate limiting.
_monotonic = time.monotonic


class OptionalFlushFileHandler(logging.FileHandler):
    """FileHandler that rate-limits flush() to reduce disk wear (at most once per FLUSH_INTERVAL_SEC)."""

    # FileHandler assigns stream in __init__ with no annotation basedpyright can see.
    stream: Any
    _writeragent_debug_handler: bool

    def __init__(
        self,
        filename: str | os.PathLike[str],
        mode: str = "a",
        encoding: str | None = None,
        delay: bool = False,
        errors: str | None = None,
    ) -> None:
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay, errors=errors)
        self._writeragent_debug_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        # Flush is rate-limited to at most once per second, and nothing else
        # flushed the tail after a burst. A crash then lost recent warnings.
        # WARNING+ always force-flushes so those records reach disk without
        # waiting for the 1s interval.
        if record.levelno >= logging.WARNING:
            self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        global _debug_log_last_flush
        now = _monotonic()
        with _debug_log_flush_lock:
            if not force and now - _debug_log_last_flush < FLUSH_INTERVAL_SEC:
                return
            _debug_log_last_flush = now
        try:
            super().flush()
            self._rotate_if_large()
        except RecursionError:
            raise
        except Exception:
            # What was wrong: stream.flush and a failed rotate escaped flush.
            # How it happened: emit force-flushes WARNING+ outside StreamHandler.emit's
            # try, and _flush_debug_log calls flush directly, so Handler.handle on
            # Python 3.14 does not catch it.
            # Why this change fixes it: swallow every Exception except RecursionError,
            # which logging re-raises to avoid a logging-error loop.
            return

    def _rotate_if_large(self) -> None:
        """Cap writeragent_debug.log size by rotating to a single .1 backup.

        The live file is capped at _MAX_DEBUG_LOG_BYTES; .1 is the previous live
        file, which one oversized record can make larger than the cap.
        """
        self.acquire()
        try:
            # Recheck size inside the handler lock. If two threads both observed
            # an over-cap size before acquiring the lock, without this check the
            # second thread's os.replace would delete .1 (the real backup) and
            # rename the newly created, small live file onto it.
            try:
                if not os.path.isfile(self.baseFilename):
                    return
                if os.path.getsize(self.baseFilename) <= _MAX_DEBUG_LOG_BYTES:
                    return
            except OSError:
                return

            stream = self.stream
            if stream is not None:
                stream.close()
                # Clear the handle before rename. A failed replace must still
                # reopen below; a closed-but-not-None stream would stay dead.
                self.stream = None
            backup = self.baseFilename + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(self.baseFilename, backup)
            self.stream = self._open()
        except OSError:
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    # What was wrong: a second OSError from _open() replaced the
                    # caught rotation error and left flush.
                    # How it happened: disk full or a bad path during reopen.
                    # Why this change fixes it: leave stream None and return;
                    # the next FileHandler.emit opens or handleErrors.
                    pass
        finally:
            self.release()

    def close(self) -> None:
        try:
            super().flush()
        except Exception:  # noqa: S110  handler teardown; a debug log re-enters this FileHandler
            pass
        # FileHandler.close calls the overridden flush(), which can raise
        # during teardown after the explicit parent flush already ran.
        try:
            super().close()
        except Exception:  # noqa: S110  FileHandler.close during teardown; a debug log re-enters this handler
            pass


# One handler shared by the writeragent and plugin loggers. Two handlers
# on the same path rotate on separate fds: after the first rename, the other fd
# keeps writing the inode that is now writeragent_debug.log.1, and that backup
# is never size-checked, so it grows past the cap. The object lives on sys so
# a second import of this package does not open another fd.


def _debug_file_handler_from_sys() -> OptionalFlushFileHandler | None:
    # Marker first, then isinstance(..., OptionalFlushFileHandler), else None.
    # isinstance against this module's class is false for the handler created by the other copy.
    handler = getattr(sys, "_writeragent_debug_file_handler", None)
    if getattr(handler, "_writeragent_debug_handler", False):
        return cast("OptionalFlushFileHandler", handler)
    if isinstance(handler, OptionalFlushFileHandler):
        return handler
    return None


def _shared_debug_file_handler() -> OptionalFlushFileHandler:
    """Return the process's debug-log handler, creating it when the path changes.

    One handler, stored on sys. A module global opened a second FileHandler
    on the same path when LibreOffice imported this package twice, and the
    two fds fought over rotation.
    """
    path = _shared()["debug_log_path"]
    if not isinstance(path, str) or not path:
        raise RuntimeError("debug log path is not set")
    current = _debug_file_handler_from_sys()
    if current is not None and getattr(current, "baseFilename", "") == path:
        return current
    # A path change must close the previous FileHandler before the replace,
    # or its fd stays alive. A close error must not block the new file.
    if current is not None:
        try:
            current.close()
        except Exception:  # noqa: S110  closing the debug FileHandler being replaced; a log call re-enters it
            pass
    handler = OptionalFlushFileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s"))
    setattr(sys, "_writeragent_debug_file_handler", handler)
    return handler


def redact_sensitive_payload_for_log(obj: Any) -> Any:
    """Deep copy of a request/response payload with audio/image base64 and long signature blobs replaced for safe debug logging."""
    out = deepcopy(obj)
    _redact_sensitive_inplace(out)
    return out


def get_debug_log_path() -> str | None:
    """Return the active writeragent_debug.log path, or None if logging is not initialized."""
    return _shared()["debug_log_path"]


def _is_matching_debug_handler(handler: logging.Handler) -> bool:
    return isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == _shared()["debug_log_path"]


def _strip_stray_handlers(logger: logging.Logger) -> bool:
    """Remove handlers that are not our debug log FileHandler. Return True if one remains."""
    has_matching = False
    for handler in list(logger.handlers):
        if _is_matching_debug_handler(handler):
            has_matching = True
            continue
        if handler.__class__.__name__ == "LogCaptureHandler":
            continue
        try:
            logger.removeHandler(handler)
            handler.close()
        except Exception:  # noqa: S110  stripping a stray handler; logging the close failure can re-enter it
            pass
    return has_matching


def _ensure_debug_file_handler(logger: logging.Logger) -> None:
    path = _shared()["debug_log_path"]
    if not isinstance(path, str) or not path:
        return
    _strip_stray_handlers(logger)
    handler = _shared_debug_file_handler()
    # A previous handler on this path would rotate on its own fd. Keep one.
    for existing in list(logger.handlers):
        if existing is not handler and _is_matching_debug_handler(existing):
            logger.removeHandler(existing)
            try:
                existing.close()
            except Exception:  # noqa: S110  closing a duplicate debug handler; a log call re-enters it
                pass
    if handler not in logger.handlers:
        logger.addHandler(handler)


def init_logging(ctx: Any | None = None) -> None:
    """Set global debug log path (LO user config dir) and enable_agent_log from ctx. Idempotent."""
    shared = _shared()
    with shared["init_lock"]:
        current_path = shared["debug_log_path"]
        had_path = bool(isinstance(current_path, str) and current_path)
        first_init = current_path is None
        try:
            if ctx is not None:
                try:
                    config.init_config(ctx)
                except ConfigError:
                    pass
            udir = config.user_config_dir()
            # Eval uses MagicMock ctx; skip file logging unless the dir is real.
            if udir and os.path.isdir(udir):
                shared["debug_log_path"] = os.path.join(udir, DEBUG_LOG_FILENAME)
                shared["enable_agent_log"] = config.get_config_bool("enable_agent_log")
            else:
                if not had_path:
                    shared["debug_log_path"] = None
        except (OSError, ImportError, ValueError, ConfigError) as exc:
            if first_init:
                print(f"WriterAgent: init_logging config unavailable: {exc}", file=sys.stderr)
            if not had_path:
                shared["debug_log_path"] = None
                shared["enable_agent_log"] = False

        level_str = "WARN"
        try:
            level_str = config.get_config_str("log_level") or "WARN"
            numeric_level = resolve_log_level(level_str)

            logger = log
            plugin_logger = logging.getLogger("plugin")
            logger.setLevel(numeric_level)

            active_path = shared["debug_log_path"]
            if isinstance(active_path, str) and active_path:
                # plugin.* uses logging.getLogger(__name__) and propagates to the
                # plugin logger. writeragent.* uses the named logger below.
                # Both share one file handler so rotation has a single fd.
                # The handler is not on the root logger: at DEBUG that pulled
                # third-party records (full URLs) into writeragent_debug.log.
                plugin_logger.setLevel(numeric_level)
                _ensure_debug_file_handler(logger)
                _ensure_debug_file_handler(plugin_logger)
                # writeragent has its own handler. plugin.* keeps propagating so
                # a root handler (tests, lastResort) still sees those records.
                # The file handler is not on root, so third-party logs are not
                # copied into writeragent_debug.log and plugin lines are not
                # written twice.
                logger.propagate = False

                if first_init:
                    logger.warning("Debug log active: %s (level=%s)", active_path, level_str)
                    for handler in list(logger.handlers):
                        if isinstance(handler, logging.FileHandler):
                            logging.FileHandler.flush(handler)
        except OSError as exc:
            if first_init:
                print(f"WriterAgent: init_logging file handler failed: {exc}", file=sys.stderr)

        if first_init:
            _install_global_exception_hooks()
            try:
                from plugin import audit_plugin_roots

                audit_plugin_roots()
            except Exception:
                log.exception("plugin path audit failed")


def _install_global_exception_hooks() -> None:
    """Install sys.excepthook and threading.excepthook to log unhandled exceptions.

    The installed flag lives on sys, and we also return when the current hook
    is already ours. A module global let LibreOffice import this package
    twice and wrap sys.excepthook again.
    """
    current_hook = sys.excepthook
    if getattr(current_hook, "_writeragent_hook", False):
        setattr(sys, "_writeragent_exception_hooks_installed", True)
        return
    if getattr(sys, "_writeragent_exception_hooks_installed", False):
        return
    setattr(sys, "_writeragent_exception_hooks_installed", True)

    _original_excepthook = current_hook

    def _writeragent_excepthook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        try:
            tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
            msg = "Unhandled exception:\n" + "".join(tb_lines)
            try:
                payload = format_error_payload(exc_value)
                msg += f"\nPayload context: {payload.get('details', {})}"
            except Exception:
                log.debug("excepthook: format_error_payload failed", exc_info=True)
            log.error(f"[Excepthook] {msg.strip()}")
        except Exception:  # noqa: S110  excepthook log.error failed; another log call can recurse
            pass
        try:
            _original_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            log.debug("original sys.excepthook failed", exc_info=True)

    setattr(_writeragent_excepthook, "_writeragent_hook", True)
    sys.excepthook = _writeragent_excepthook

    if getattr(threading, "excepthook", None) is None:
        return
    # Same marker as the sys hook. Resetting only sys.excepthook (tests, or
    # a second import that already wrapped sys) must not wrap
    # threading.excepthook again.
    if getattr(threading.excepthook, "_writeragent_hook", False):
        return
    _original_threading_excepthook = threading.excepthook

    def _writeragent_threading_excepthook(args: Any) -> None:
        try:
            msg = "Unhandled exception in thread %s: %s\n%s" % (getattr(args, "thread", None), getattr(args, "exc_type", args), "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)) if getattr(args, "exc_type", None) else "")
            try:
                payload = format_error_payload(args.exc_value)
                msg += f"\nPayload context: {payload.get('details', {})}"
            except Exception:
                log.debug("threading excepthook: format_error_payload failed", exc_info=True)
            log.error(f"[Excepthook] {msg.strip()}")
        except Exception:  # noqa: S110  threading excepthook log.error failed; another log call can recurse
            pass
        try:
            _original_threading_excepthook(args)
        except Exception:
            log.debug("original threading.excepthook failed", exc_info=True)

    setattr(_writeragent_threading_excepthook, "_writeragent_hook", True)
    threading.excepthook = _writeragent_threading_excepthook


def log_exception(ex: BaseException, context: str = "WriterAgent") -> None:
    """Log an exception with traceback to the unified debug log."""
    try:
        logger = log
        logger.error(f"[{context}] Exception", exc_info=ex)
    except Exception:  # noqa: S110  log_exception's own logger.error failed; logging again can recurse
        pass


def _redacted_display_args(args: Any) -> Any:
    """Same secret walk as the debug log, before the display truncates reprs."""
    if isinstance(args, dict):
        return redact_sensitive_payload_for_log(args)
    return args


def format_tool_call_for_display(tool: Any, args: Any, method: Any = None) -> str:
    """Format an MCP tool call or generic method call for UI display, summarizing long arguments."""
    try:
        if tool:
            args_dict = _redacted_display_args(args or {})
            arg_vals = []
            if isinstance(args_dict, dict):
                for k, v in args_dict.items():
                    if isinstance(k, str) and k.casefold() in _SECRET_LOG_KEYS and isinstance(v, str):
                        val_str = repr(LOG_REDACT_SECRET_PLACEHOLDER)
                        arg_vals.append(f"{k}={val_str}")
                        continue
                    val_str = repr(v)
                    if len(val_str) > 100:
                        if isinstance(v, str):
                            val_str = repr(v[:100] + "...")
                        else:
                            val_str = val_str[:100] + "..."
                    arg_vals.append(f"{k}={val_str}")
            args_str = ", ".join(arg_vals)
            return f"{tool}({args_str})"
        else:
            return method or "GET"
    except Exception as e:
        return f"{tool or method} (format error: {e})"


def format_tool_result_for_display(tool: Any, result: Any, args: Any = None) -> str:
    """Format an MCP tool result for UI display, extracting inner text/messages and summarizing length."""
    try:
        res_str = str(result)
        try:
            res_dict = safe_json_loads(result) if isinstance(result, str) else result
            if isinstance(res_dict, dict) and "content" in res_dict and isinstance(res_dict["content"], list):
                parts = []
                for item in res_dict["content"]:
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                if parts:
                    res_str = " ".join(parts)
                    inner_dict = safe_json_loads(res_str)
                    if isinstance(inner_dict, dict) and "message" in inner_dict:
                        res_str = inner_dict["message"]
        except Exception:
            log.debug("format_tool_result_for_display: content parse failed", exc_info=True)

        val_repr = repr(res_str)
        if len(val_repr) > 150:
            if isinstance(res_str, str):
                val_repr = repr(res_str[:150] + "...")
            else:
                val_repr = val_repr[:150] + "..."

        args_str = ""
        if args:
            args_dict = _redacted_display_args(args) if isinstance(args, dict) else {}
            arg_vals = []
            for k, v in args_dict.items():
                if isinstance(k, str) and k.casefold() in _SECRET_LOG_KEYS and isinstance(v, str):
                    arg_vals.append(f"{k}={repr(LOG_REDACT_SECRET_PLACEHOLDER)}")
                    continue
                v_str = repr(v)
                if len(v_str) > 100:
                    if isinstance(v, str):
                        v_str = repr(v[:100] + "...")
                    else:
                        v_str = v_str[:100] + "..."
                arg_vals.append(f"{k}={v_str}")
            args_str = ", ".join(arg_vals)

        if args_str:
            return f"{tool}({args_str}) -> {val_repr}"
        return f"{tool}() -> {val_repr}"
    except Exception as e:
        return f"{tool}() -> (format error: {e})"


def agent_log(location: str, message: str, data: Any = None, hypothesis_id: Any = None, run_id: Any = None) -> None:
    """Write one structured agent trace line to writeragent_debug.log when enable_agent_log is True."""
    if not _shared()["enable_agent_log"]:
        return
    # redact_sensitive_payload_for_log deepcopy's the payload. A Lock (or
    # anything deepcopy rejects) raises TypeError. Redaction and dumps share
    # this try so a bad payload is dropped instead of raised out of
    # best-effort logging.
    try:
        payload = {"location": location, "message": message, "timestamp": int(time.time() * 1000)}
        if data is not None:
            # Copy first so a logged payload cannot mutate the caller's dict.
            if isinstance(data, (dict, list)):
                data = redact_sensitive_payload_for_log(data)
            payload["data"] = data
        if hypothesis_id is not None:
            payload["hypothesisId"] = hypothesis_id
        if run_id is not None:
            payload["runId"] = run_id
        # Bugfix: Release stripping deleted this .debug call, so enable_agent_log
        # wrote nothing in a shipped OXT. Calling Logger.log(logging.DEBUG, ...)
        # fixes this because 'log' is outside LOGGER_STRIP_ATTRS, while the level
        # remains DEBUG so the agent logger's own level still admits the line
        # when the parent log_level is set to WARN.
        _agent_logger.log(logging.DEBUG, "[Agent] %s", json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        log.debug("agent_log dropped payload (%s)", type(exc).__name__)


def note_activity() -> None:
    """Note recent activity to prevent false Hung statuses, without changing phase or logging."""
    shared = _shared()
    with shared["activity_lock"]:
        shared["activity"]["last_activity"] = time.monotonic()


def update_activity_state(phase: str, round_num: Any = None, tool_name: str | None = None, status_control: Any = None) -> None:
    """Update shared activity state (call from main thread at phase boundaries).

    Pass phase='' when returning control to LibreOffice so the watchdog stops checking.
    Omitted tool_name clears the previous tool so a later Hung: line is not attributed to it;
    omitted round_num keeps the last round.
    """
    shared = _shared()
    with shared["activity_lock"]:
        act = shared["activity"]
        act["phase"] = phase
        act["last_activity"] = time.monotonic()
        if round_num is not None:
            act["round_num"] = round_num
        # What was wrong: previously tool_name was only overwritten when not None.
        # How it happened: callers such as tool_loop and exhausted_rounds omit
        # tool_name, causing round 2 to still display round 1's tool.
        # Why this change fixes it: always assign tool_name so omitted tool_name
        # (default None) properly clears it; the only caller that has an active
        # tool already passes tool_name explicitly.
        act["tool_name"] = tool_name
        if status_control is not None:
            act["status_control"] = status_control

        ctrl_to_clear = act.get("status_control")
        if not phase:
            act["status_control"] = None

    if not phase and ctrl_to_clear is not None:
        try:
            from plugin.framework.queue_executor import post_to_main_thread

            post_to_main_thread(_clear_hung_status, ctrl_to_clear)
        except Exception:
            log.debug("watchdog: failed to post Hung-status clear", exc_info=True)


def _clear_hung_status(status_control: Any) -> None:
    """Clear a watchdog Hung: label once work resumes. Leave any newer status."""
    try:
        current = status_control.getText()
    except Exception:
        log.debug("watchdog: could not read status text", exc_info=True)
        return
    if isinstance(current, str) and current.startswith(_HUNG_STATUS_PREFIX):
        status_control.setText("")


def _flush_debug_log() -> None:
    handler = _debug_file_handler_from_sys()
    if handler is not None:
        handler.flush(force=True)


def _dump_thread_stacks() -> None:
    """Log every thread's current Python stack as one DEBUG record.

    Why: a hang log only said "no activity". We suspect the main thread holds
    the SolarMutex in the stream drain while a worker blocks freeing a PyUNO
    proxy; the stacks show where each thread is actually waiting.
    """
    try:
        names = {t.ident: t for t in threading.enumerate()}
        lines = ["[Chat] WATCHDOG: thread stack dump\n"]
        for ident, frame in sys._current_frames().items():
            thread = names.get(ident)
            name = thread.name if thread else "?"
            daemon = thread.daemon if thread else "?"
            lines.append("--- Thread %s (ident=%s, daemon=%s) ---\n" % (name, ident, daemon))
            lines.extend(traceback.format_stack(frame))
        log.debug("".join(lines))
    except Exception:
        log.debug("watchdog: thread stack dump failed", exc_info=True)


def _watchdog_check(status_control: Any) -> None:
    """One watchdog pass. Posts Hung: after the idle threshold, and clears it when activity resumes."""
    # Flush the debug file on each watchdog check. The activity timestamps
    # alone leave the unwritten tail in memory after a burst stops.
    _flush_debug_log()
    shared = _shared()
    with shared["activity_lock"]:
        act = shared["activity"]
        phase = act["phase"]
        round_num = act["round_num"]
        tool_name = act["tool_name"]
        last = act["last_activity"]
        active_status_control: Any = act.get("status_control", status_control)
    if active_status_control is None:
        active_status_control = status_control
    if not phase:
        return
    last_val = last if isinstance(last, (int, float)) else 0.0
    elapsed = time.monotonic() - last_val
    if elapsed < _watchdog_threshold_sec:
        shared["stacks_dumped"] = False
        if shared["hung_shown"] and active_status_control is not None:
            try:
                from plugin.framework.queue_executor import post_to_main_thread

                post_to_main_thread(_clear_hung_status, active_status_control)
                shared["hung_shown"] = False
            except Exception:
                log.debug("watchdog: failed to clear Hung status", exc_info=True)
        return
    msg = "WATCHDOG: no activity for %ds; phase=%s round=%s tool=%s" % (int(elapsed), phase, round_num, tool_name if tool_name else "")
    log.debug(f"[Chat] {msg}")
    # Once per stall episode: later ticks of the same stall would only repeat it.
    if not shared["stacks_dumped"]:
        shared["stacks_dumped"] = True
        _dump_thread_stacks()
        # Write it now; a hang that ends in a crash may not reach the next tick.
        _flush_debug_log()
    if active_status_control:
        hung_text = "%s %s round %s" % (_HUNG_STATUS_PREFIX, phase, round_num)
        if tool_name:
            hung_text += " %s" % tool_name
        try:
            from plugin.framework.queue_executor import post_to_main_thread

            post_to_main_thread(active_status_control.setText, hung_text)
            shared["hung_shown"] = True
        except Exception:
            log.debug("watchdog: failed to post Hung status to main thread", exc_info=True)


def _watchdog_loop(status_control: Any) -> None:
    """Dedicated pool job: if no activity for threshold, log and set status to Hung: ..."""
    while True:
        time.sleep(_watchdog_interval_sec)
        try:
            _watchdog_check(status_control)
        except RecursionError:
            raise
        except Exception:
            # What was wrong: one OSError from _watchdog_check ended the dedicated thread.
            # How it happened: run_in_background's _worker logs the exception and
            # returns; dedicated=True does not respawn.
            # Why this change fixes it: the next sleep still runs the check; the tick
            # log is itself guarded so a broken handler cannot end the thread.
            try:
                log.exception("watchdog tick failed")
            except Exception:  # noqa: S110  watchdog tick logger failed; a nested log can kill this dedicated thread
                pass


def start_watchdog_thread(ctx: Any, status_control: Any = None) -> None:
    """Start the hang-detection watchdog (idempotent). Pass status_control to set Hung: ... in UI.

    The flag is on sys. A module global let a second import start another
    job, and the pool imports ran at module import (LibrePy imports logging
    and never starts the watchdog). The job is a dedicated pool task, not a
    raw thread. ctx is unused; callers still pass the component context.
    """
    del ctx
    with _shared()["activity_lock"]:
        if getattr(sys, "_writeragent_watchdog_started", False):
            return
        setattr(sys, "_writeragent_watchdog_started", True)
    try:
        from plugin.framework.thread_guard import background
        from plugin.framework.worker_pool import run_in_background

        @background
        def _watchdog_job(status: Any) -> None:
            _watchdog_loop(status)

        run_in_background(_watchdog_job, status_control, name="watchdog", daemon=True, dedicated=True)
    except Exception:
        # What was wrong: the flag was set before run_in_background, so an import
        # or start failure left _writeragent_watchdog_started true.
        # How it happened: panel_factory calls this on each sidebar wire and
        # would otherwise no-op for the rest of the process.
        # Why this change fixes it: clear and re-raise so the next wire can start
        # it; the flag is still set before start so two callers cannot both launch.
        with _shared()["activity_lock"]:
            setattr(sys, "_writeragent_watchdog_started", False)
        raise


def _install_safe_log_record_factory() -> None:
    """Install a custom LogRecord factory to prevent TypeError in Python 3.12+
    when logging a single PyUNO proxy object.

    A marker on the factory and a sys flag make a second import a no-op.
    A module global wrapped the factory again.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, "_writeragent_safe_factory", False):
        setattr(sys, "_writeragent_log_record_factory_installed", True)
        return
    if getattr(sys, "_writeragent_log_record_factory_installed", False):
        return

    _original_factory = current

    def safe_logRecordFactory(*args: Any, **kwargs: Any) -> Any:
        def _coerce_logged_arg(x: Any) -> Any:
            if type(x).__name__ == "pyuno":
                try:
                    return str(x)
                except Exception:
                    # What was wrong: str(x) on a disposed proxy raised out of record creation.
                    # How it happened: the factory runs inside the logging call.
                    # Why this change fixes it: fall back to "<pyuno>" so the log call still returns.
                    return "<pyuno>"
            return x

        # args contains (name, level, fn, lno, msg, args, exc_info, func, sinfo)
        # args is at index 5.
        if len(args) > 5:
            log_args = args[5]
            if isinstance(log_args, tuple):
                if any(type(x).__name__ == "pyuno" for x in log_args):
                    new_args = list(args)
                    new_args[5] = tuple(_coerce_logged_arg(x) for x in log_args)
                    args = tuple(new_args)
        if "args" in kwargs:
            log_args = kwargs["args"]
            if isinstance(log_args, tuple):
                if any(type(x).__name__ == "pyuno" for x in log_args):
                    kwargs["args"] = tuple(_coerce_logged_arg(x) for x in log_args)
        return _original_factory(*args, **kwargs)

    setattr(safe_logRecordFactory, "_writeragent_safe_factory", True)
    logging.setLogRecordFactory(safe_logRecordFactory)
    setattr(sys, "_writeragent_log_record_factory_installed", True)


_install_safe_log_record_factory()
