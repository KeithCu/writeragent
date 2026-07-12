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
Also: redaction helpers for debug logs that would otherwise embed large base64 (chat multimodal parts, image API JSON).
"""

import os
import sys
import json
import time
import traceback
import threading
import logging
from copy import deepcopy
from typing import Any

from plugin.framework.worker_pool import run_in_background
from plugin.framework.thread_guard import background
from plugin.framework.errors import ConfigError, format_error_payload
from plugin.framework.json_utils import safe_json_loads
from plugin.framework import config

# Globals set by init_logging(ctx); agent_log reads _enable_agent_log so ctx is not passed at write time.
_debug_log_path = None
_enable_agent_log = False
_log_level_numeric = 10  # Default to DEBUG
_init_lock = threading.Lock()
_exception_hooks_installed = False

log = logging.getLogger("writeragent")

# Watchdog: shared state (main thread updates, watchdog reads)
_activity_state = {"phase": "", "round_num": -1, "tool_name": None, "last_activity": 0.0}
_activity_lock = threading.Lock()
_watchdog_started = False
_watchdog_interval_sec = 15
_watchdog_threshold_sec = 30

DEBUG_LOG_FILENAME = "writeragent_debug.log"

LOG_REDACT_AUDIO_PLACEHOLDER = "<audio base64 data truncated, length=%d>"
LOG_REDACT_IMAGE_PLACEHOLDER = "<image base64 data truncated, length=%d>"


def _redact_sensitive_inplace(o: Any) -> None:
    """Strip large base64 from nested API-shaped JSON (chat multimodal parts, image requests/responses)."""
    if isinstance(o, dict):
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
        for v in o.values():
            _redact_sensitive_inplace(v)
    elif isinstance(o, list):
        for item in o:
            _redact_sensitive_inplace(item)


FLUSH_INTERVAL_SEC = 1.0
_debug_log_flush_lock = threading.Lock()
_debug_log_last_flush = 0.0
# Import-time binding so tests that patch time.monotonic (e.g. LLM pacing) are not affected by flush rate limiting.
_monotonic = time.monotonic


class OptionalFlushFileHandler(logging.FileHandler):
    """FileHandler that rate-limits flush() to reduce disk wear (at most once per FLUSH_INTERVAL_SEC)."""

    def flush(self) -> None:
        global _debug_log_last_flush
        now = _monotonic()
        with _debug_log_flush_lock:
            if now - _debug_log_last_flush < FLUSH_INTERVAL_SEC:
                return
            _debug_log_last_flush = now
        super().flush()

    def close(self) -> None:
        try:
            super().flush()
        except Exception:
            pass
        super().close()


def redact_sensitive_payload_for_log(obj: Any) -> Any:
    """Deep copy of a request/response payload with audio and image base64 replaced for safe debug logging."""
    out = deepcopy(obj)
    _redact_sensitive_inplace(out)
    return out


def get_debug_log_path() -> str | None:
    """Return the active writeragent_debug.log path, or None if logging is not initialized."""
    return _debug_log_path


def _is_matching_debug_handler(handler: logging.Handler) -> bool:
    return (
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", "") == _debug_log_path
    )


def _strip_stray_handlers(logger: logging.Logger) -> bool:
    """Remove handlers that are not our debug log FileHandler. Return True if one remains."""
    has_matching = False
    for handler in list(logger.handlers):
        if _is_matching_debug_handler(handler):
            has_matching = True
            continue
        try:
            logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
    return has_matching


def _ensure_debug_file_handler(logger: logging.Logger) -> None:
    if not _debug_log_path:
        return
    if _strip_stray_handlers(logger):
        return
    handler = OptionalFlushFileHandler(_debug_log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)


def init_logging(ctx=None):
    """Set global debug log path (LO user config dir) and enable_agent_log from ctx. Idempotent."""
    global _debug_log_path, _enable_agent_log
    with _init_lock:
        first_init = _debug_log_path is None
        try:
            if ctx is not None:
                config.init_config(ctx)
            udir = config.user_config_dir()
            if udir:
                _debug_log_path = os.path.join(udir, DEBUG_LOG_FILENAME)
                _enable_agent_log = config.get_config_bool("enable_agent_log")
        except (OSError, ImportError, ValueError, ConfigError) as exc:
            if first_init:
                print(f"WriterAgent: init_logging config unavailable: {exc}", file=sys.stderr)
            _debug_log_path = None
            _enable_agent_log = False

        level_str = "WARN"
        try:
            level_str = config.get_config_str("log_level") or "WARN"
            numeric_level = getattr(logging, str(level_str).upper(), logging.WARNING)
            global _log_level_numeric
            _log_level_numeric = numeric_level

            logger = log
            root_logger = logging.getLogger()
            logger.setLevel(numeric_level)

            if _debug_log_path:
                # plugin.* modules use logging.getLogger(__name__); root receives those records.
                # writeragent.* uses the named logger below with propagate=False to avoid duplicates.
                root_logger.setLevel(numeric_level)
                _ensure_debug_file_handler(logger)
                _ensure_debug_file_handler(root_logger)
                logger.propagate = False
                logging.lastResort = None

                if first_init:
                    logger.warning(
                        "Debug log active: %s (level=%s)",
                        _debug_log_path,
                        level_str,
                    )
                    for handler in list(logger.handlers):
                        if isinstance(handler, logging.FileHandler):
                            logging.FileHandler.flush(handler)
        except OSError as exc:
            if first_init:
                print(f"WriterAgent: init_logging file handler failed: {exc}", file=sys.stderr)

        if first_init:
            _install_global_exception_hooks()


def _install_global_exception_hooks():
    """Install sys.excepthook and threading.excepthook to log unhandled exceptions. Idempotent."""
    global _exception_hooks_installed
    if _exception_hooks_installed:
        return
    _exception_hooks_installed = True

    _original_excepthook = sys.excepthook

    def _writeragent_excepthook(exc_type, exc_value, exc_tb):
        try:
            tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
            msg = "Unhandled exception:\n" + "".join(tb_lines)
            try:
                payload = format_error_payload(exc_value)
                msg += f"\nPayload context: {payload.get('details', {})}"
            except Exception:
                pass
            log.error(f"[Excepthook] {msg.strip()}")
        except Exception:
            pass
        try:
            _original_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = _writeragent_excepthook

    if getattr(threading, "excepthook", None) is not None:
        _original_threading_excepthook = threading.excepthook

        def _writeragent_threading_excepthook(args):
            try:
                msg = "Unhandled exception in thread %s: %s\n%s" % (getattr(args, "thread", None), getattr(args, "exc_type", args), "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)) if getattr(args, "exc_type", None) else "")
                try:
                    payload = format_error_payload(args.exc_value)
                    msg += f"\nPayload context: {payload.get('details', {})}"
                except Exception:
                    pass
                log.error(f"[Excepthook] {msg.strip()}")
            except Exception:
                pass
            try:
                if _original_threading_excepthook:
                    _original_threading_excepthook(args)
            except Exception:
                pass

        threading.excepthook = _writeragent_threading_excepthook


class SafeLogger:
    """Logger wrapper with error handling."""

    def __init__(self, logger):
        self._logger = logger
        self._fallback_enabled = True

    def error(self, msg, *args, **kwargs):
        """Safe error logging."""
        try:
            if self._logger:
                self._logger.error(msg, *args, **kwargs)
        except Exception as e:
            if self._fallback_enabled:
                print(f"LOG ERROR FAILED: {msg}")
                print(f"Original error: {e}")

    def warning(self, msg, *args, **kwargs):
        """Safe warning logging."""
        try:
            if self._logger:
                self._logger.warning(msg, *args, **kwargs)
        except Exception as e:
            if self._fallback_enabled:
                print(f"LOG WARNING FAILED: {msg}")
                print(f"Original error: {e}")

    def exception(self, msg, *args, **kwargs):
        """Safe exception logging (includes stacktrace)."""
        try:
            if self._logger:
                self._logger.exception(msg, *args, **kwargs)
        except Exception as e:
            if self._fallback_enabled:
                print(f"LOG EXCEPTION FAILED: {msg}")
                print(f"Original error: {e}")

    def disable_fallback(self):
        """Disable fallback printing."""
        self._fallback_enabled = False


def safe_log_exception(e, context="general", logger=None):
    """Safely log exceptions with fallback mechanisms."""

    if logger is None:
        logger = log

    try:
        # Try to get detailed error info
        error_info = {"type": type(e).__name__, "message": str(e), "context": context, "timestamp": time.time()}

        # Add traceback if available
        try:
            error_info["traceback"] = traceback.format_exc()
        except Exception:
            error_info["traceback"] = "<unavailable>"

        # Log with structured data
        if hasattr(logger, "error"):
            logger.error("Exception occurred: %s" % error_info["message"], extra={"error_details": error_info})
        else:
            # Fallback logging
            print(f"ERROR [{context}]: {error_info['message']}")
            print(f"Type: {error_info['type']}")
            print(f"Traceback: {error_info['traceback']}")

    except Exception as logging_error:
        # Final fallback if logging itself fails
        print(f"CRITICAL: Logging failed for exception: {e}")
        print(f"Logging error: {logging_error}")


def log_exception(ex, context="WriterAgent"):
    """Log an exception with traceback to the unified debug log."""
    try:
        logger = log
        logger.error(f"[{context}] Exception", exc_info=ex)
    except Exception:
        pass


def format_tool_call_for_display(tool, args, method=None):
    """Format an MCP tool call or generic method call for UI display, summarizing long arguments."""
    try:
        if tool:
            args_dict = args or {}
            arg_vals = []
            if isinstance(args_dict, dict):
                for k, v in args_dict.items():
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


def format_tool_result_for_display(tool, result, args=None):
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
            pass

        val_repr = repr(res_str)
        if len(val_repr) > 150:
            if isinstance(res_str, str):
                val_repr = repr(res_str[:150] + "...")
            else:
                val_repr = val_repr[:150] + "..."

        args_str = ""
        if args:
            args_dict = args if isinstance(args, dict) else {}
            arg_vals = []
            for k, v in args_dict.items():
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


def agent_log(location, message, data=None, hypothesis_id=None, run_id=None):
    """Write one structured agent trace line to writeragent_debug.log when enable_agent_log is True."""
    if not _enable_agent_log:
        return
    payload = {"location": location, "message": message, "timestamp": int(time.time() * 1000)}
    if data is not None:
        payload["data"] = data
    if hypothesis_id is not None:
        payload["hypothesisId"] = hypothesis_id
    if run_id is not None:
        payload["runId"] = run_id
    try:
        log.debug("[Agent] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def update_activity_state(phase, round_num=None, tool_name=None):
    """Update shared activity state (call from main thread at phase boundaries).
    Pass phase='' when returning control to LibreOffice so the watchdog stops checking."""
    with _activity_lock:
        _activity_state["phase"] = phase
        _activity_state["last_activity"] = time.monotonic()
        if round_num is not None:
            _activity_state["round_num"] = round_num
        if tool_name is not None:
            _activity_state["tool_name"] = tool_name


# Per-send timing spans (writeragent_debug.log, DEBUG). One active send at a time.
_send_timing_lock = threading.Lock()
_send_timing_origin: float | None = None
_send_timing_log = logging.getLogger("writeragent.send_timing")


def begin_send_timing() -> None:
    """Mark send-click origin for subsequent log_send_timing() lines."""
    global _send_timing_origin
    with _send_timing_lock:
        _send_timing_origin = time.monotonic()


def clear_send_timing() -> None:
    global _send_timing_origin
    with _send_timing_lock:
        _send_timing_origin = None


def log_send_timing(milestone: str, **extra: object) -> None:
    """Log a send-path milestone with +ms since begin_send_timing() (DEBUG)."""
    with _send_timing_lock:
        origin = _send_timing_origin
    if origin is None:
        if extra:
            _send_timing_log.debug("send_timing %s (no origin) %s", milestone, extra)
        else:
            _send_timing_log.debug("send_timing %s (no origin)", milestone)
        return
    delta_ms = (time.monotonic() - origin) * 1000.0
    if extra:
        _send_timing_log.debug("send_timing %s +%.1fms %s", milestone, delta_ms, extra)
    else:
        _send_timing_log.debug("send_timing %s +%.1fms", milestone, delta_ms)


@background
def _watchdog_loop(status_control):
    """Daemon thread: if no activity for threshold, log and set status to Hung: ..."""
    while True:
        time.sleep(_watchdog_interval_sec)
        with _activity_lock:
            phase = _activity_state["phase"]
            round_num = _activity_state["round_num"]
            tool_name = _activity_state["tool_name"]
            last = _activity_state["last_activity"]
        if not phase:
            continue
        last_val = last if isinstance(last, (int, float)) else 0.0
        elapsed = time.monotonic() - last_val
        if elapsed < _watchdog_threshold_sec:
            continue
        msg = "WATCHDOG: no activity for %ds; phase=%s round=%s tool=%s" % (int(elapsed), phase, round_num, tool_name if tool_name else "")
        log.debug(f"[Chat] {msg}")
        if status_control:
            try:
                hung_text = "Hung: %s round %s" % (phase, round_num)
                if tool_name:
                    hung_text += " %s" % tool_name
                status_control.setText(hung_text)
            except Exception:
                pass  # UNO from background thread may be unsafe; ignore


def start_watchdog_thread(ctx, status_control=None):
    """Start the hang-detection watchdog (idempotent). Pass status_control to set Hung: ... in UI."""
    global _watchdog_started
    with _activity_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    run_in_background(_watchdog_loop, status_control, name="watchdog", daemon=True)


# Custom LogRecord Factory for PyUNO safety in Python 3.12+
_log_record_factory_installed = False

def _install_safe_log_record_factory():
    """Install a custom LogRecord factory to prevent TypeError in Python 3.12+
    when logging a single PyUNO proxy object."""
    global _log_record_factory_installed
    if _log_record_factory_installed:
        return
    _log_record_factory_installed = True

    _original_factory = logging.getLogRecordFactory()

    def safe_logRecordFactory(*args, **kwargs):
        # args contains (name, level, fn, lno, msg, args, exc_info, func, sinfo)
        # args is at index 5.
        if len(args) > 5:
            log_args = args[5]
            if isinstance(log_args, tuple):
                if any(type(x).__name__ == "pyuno" for x in log_args):
                    new_args = list(args)
                    new_args[5] = tuple(str(x) if type(x).__name__ == "pyuno" else x for x in log_args)
                    args = tuple(new_args)
        if "args" in kwargs:
            log_args = kwargs["args"]
            if isinstance(log_args, tuple):
                if any(type(x).__name__ == "pyuno" for x in log_args):
                    kwargs["args"] = tuple(str(x) if type(x).__name__ == "pyuno" else x for x in log_args)
        return _original_factory(*args, **kwargs)

    logging.setLogRecordFactory(safe_logRecordFactory)


_install_safe_log_record_factory()

