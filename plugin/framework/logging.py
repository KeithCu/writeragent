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
"""Simple file logging for WriterAgent. Single debug log + optional agent log; paths set via init_logging(ctx)."""
import os
import sys
import json
import time
import traceback
import threading
import logging

from plugin.framework.worker_pool import run_in_background

# Globals set by init_logging(ctx); used by debug_log and agent_log so ctx is not passed at write time.
_debug_log_path = None
_agent_log_path = None
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
AGENT_LOG_FILENAME = "writeragent_agent.log"
FALLBACK_DEBUG = os.path.join(os.path.expanduser("~"), "writeragent_debug.log")
FALLBACK_AGENT = os.path.join(os.path.expanduser("~"), "localwriter_agent.log")


def init_logging(ctx):
    """Set global log paths and enable_agent_log from ctx. Idempotent; safe to call from any entry point."""
    global _debug_log_path, _agent_log_path, _enable_agent_log
    with _init_lock:
        first_init = _debug_log_path is None
        _debug_log_path = FALLBACK_DEBUG
        _agent_log_path = FALLBACK_AGENT
        _enable_agent_log = False
        try:
            from plugin.framework import config
            from plugin.framework.errors import ConfigError
            udir = config.user_config_dir(ctx)
            if udir:
                _debug_log_path = os.path.join(udir, DEBUG_LOG_FILENAME)
                _agent_log_path = os.path.join(udir, AGENT_LOG_FILENAME)
                _enable_agent_log = config.as_bool(config.get_config(ctx, "enable_agent_log"))
        except (OSError, ImportError, ValueError, ConfigError):
            pass

        try:
            from plugin.framework import config
            import logging
            level_str = config.get_config(ctx, "log_level")
            numeric_level = getattr(logging, level_str.upper(), logging.WARNING)
            global _log_level_numeric
            _log_level_numeric = numeric_level
            
            logger = log
            logger.setLevel(numeric_level)
            # Ensure unrelated loggers (e.g. logging.getLogger(__name__) inside
            # UNO panel/tool modules) still reach the same debug log.
            #
            # We attach the handler to the root logger as well and then disable
            # propagation from the "writeragent" logger to avoid duplicates.
            
            has_matching_handler = False
            for handler in list(logger.handlers):
                if not isinstance(handler, logging.FileHandler):
                    continue
                if getattr(handler, "baseFilename", "") == _debug_log_path:
                    has_matching_handler = True
                    continue
                try:
                    logger.removeHandler(handler)
                    handler.close()
                except Exception:
                    pass

            if not has_matching_handler:
                handler = logging.FileHandler(_debug_log_path, encoding='utf-8')
                formatter = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s')
                handler.setFormatter(formatter)
                logger.addHandler(handler)
            
            # Attach the same debug file handler to root if needed.
            root_logger = logging.getLogger()
            root_logger.setLevel(numeric_level)
            root_has_matching_handler = False
            for rh in list(root_logger.handlers):
                if not isinstance(rh, logging.FileHandler):
                    continue
                if getattr(rh, "baseFilename", "") == _debug_log_path:
                    root_has_matching_handler = True
                    continue
            if not root_has_matching_handler:
                root_handler = logging.FileHandler(_debug_log_path, encoding='utf-8')
                root_handler.setFormatter(
                    logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s')
                )
                root_logger.addHandler(root_handler)

            # Prevent double-logging for loggers under "writeragent.*" since
            # they are handled by `logger` above.
            logger.propagate = False
        except OSError:
            pass

        if first_init:
            _install_global_exception_hooks()





def _install_global_exception_hooks():
    """Install sys.excepthook and threading.excepthook to log unhandled exceptions. Idempotent."""
    global _exception_hooks_installed
    if _exception_hooks_installed:
        return
    _exception_hooks_installed = True

    _original_excepthook = sys.excepthook

    def _localwriter_excepthook(exc_type, exc_value, exc_tb):
        try:
            tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
            msg = "Unhandled exception:\n" + "".join(tb_lines)
            from plugin.framework.errors import format_error_payload
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

    sys.excepthook = _localwriter_excepthook

    if getattr(threading, "excepthook", None) is not None:
        _original_threading_excepthook = threading.excepthook

        def _localwriter_threading_excepthook(args):
            try:
                msg = "Unhandled exception in thread %s: %s\n%s" % (
                    getattr(args, "thread", None),
                    getattr(args, "exc_type", args),
                    "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
                    if getattr(args, "exc_type", None) else "",
                )
                from plugin.framework.errors import format_error_payload
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

        threading.excepthook = _localwriter_threading_excepthook


def _get_agent_path():
    return _agent_log_path if _agent_log_path else FALLBACK_AGENT


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

    def disable_fallback(self):
        """Disable fallback printing."""
        self._fallback_enabled = False


def safe_log_exception(e, context="general", logger=None):
    """Safely log exceptions with fallback mechanisms."""

    if logger is None:
        logger = log

    try:
        # Try to get detailed error info
        error_info = {
            'type': type(e).__name__,
            'message': str(e),
            'context': context,
            'timestamp': time.time()
        }

        # Add traceback if available
        try:
            import traceback
            error_info['traceback'] = traceback.format_exc()
        except Exception:
            error_info['traceback'] = "<unavailable>"

        # Log with structured data
        if hasattr(logger, 'error'):
            logger.error(
                "Exception occurred: %s" % error_info['message'],
                extra={'error_details': error_info}
            )
        else:
            # Fallback logging
            print(f"ERROR [{context}]: {error_info['message']}")
            print(f"Type: {error_info['type']}")
            print(f"Traceback: {error_info['traceback']}")

    except Exception as logging_error:
        # Final fallback if logging itself fails
        print(f"CRITICAL: Logging failed for exception: {e}")
        print(f"Logging error: {logging_error}")


def log_exception(ex, context="AIHorde"):
    """Log an exception with traceback to the unified debug log. Used by aihordeclient and others."""
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
        from plugin.framework.errors import safe_json_loads
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
        except:
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
    """Write one NDJSON line to agent log if enable_agent_log is True. Uses global path."""
    if not _enable_agent_log:
        return
    payload = {"location": location, "message": message, "timestamp": int(time.time() * 1000)}
    if data is not None:
        payload["data"] = data
    if hypothesis_id is not None:
        payload["hypothesisId"] = hypothesis_id
    if run_id is not None:
        payload["runId"] = run_id
    line = json.dumps(payload) + "\n"
    path = _get_agent_path()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
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
        msg = "WATCHDOG: no activity for %ds; phase=%s round=%s tool=%s" % (
            int(elapsed), phase, round_num, tool_name if tool_name else "")
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
