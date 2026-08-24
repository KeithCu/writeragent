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
"""Centralized exception hierarchy and error formatting for WriterAgent.

All custom exceptions should inherit from WriterAgentException.
"""


# crosshair: off
from __future__ import annotations

import contextlib
import logging
from typing import Any, Literal, TypedDict

from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_json_loads, safe_python_literal_eval

from plugin.framework.deal_shim import DEAL_MAX_TOKEN, ascii_bounded, deal

try:
    from com.sun.star.lang import DisposedException
    from com.sun.star.uno import RuntimeException, Exception as UnoException

    # In test mock environments, UnoException or RuntimeException might be aliased to builtins.Exception.
    # We only include types that are not the root Exception class.
    _raw_uno_exceptions = (DisposedException, RuntimeException, UnoException)
    UNO_DISPOSED_EXCEPTIONS: tuple[type[BaseException], ...] = tuple(
        cls for cls in _raw_uno_exceptions if isinstance(cls, type) and issubclass(cls, BaseException) and cls is not Exception
    )
except (ImportError, AttributeError):
    UNO_DISPOSED_EXCEPTIONS = ()


def is_disposed_exception(exc: BaseException) -> bool:
    """Return True if exc represents a UNO object disposal or runtime teardown exception."""
    if isinstance(exc, DocumentDisposedError):
        return True
    if UNO_DISPOSED_EXCEPTIONS and isinstance(exc, UNO_DISPOSED_EXCEPTIONS):
        return True
    exc_name = type(exc).__name__
    return "DisposedException" in exc_name or "RuntimeException" in exc_name


class suppress_disposed(contextlib.ContextDecorator):
    """Context manager and decorator to safely execute UI / UNO lifecycle actions.

    Disposed UNO objects (and related bridge teardown exceptions) are caught, logged at DEBUG
    level, and suppressed.

    Unexpected non-disposal exceptions are logged (via logger.exception) and,
    if suppress_all is True (default for UI lifecycle blocks), suppressed so they do not crash host UI event loops.
    """

    def __init__(
        self,
        action: str = "action",
        *,
        logger: logging.Logger | None = None,
        log_unexpected: bool = True,
        suppress_all: bool = True,
        exc_info: bool = False,
    ):
        self.action = action
        self.logger = logger
        self.log_unexpected = log_unexpected
        self.suppress_all = suppress_all
        self.exc_info = exc_info

    def __enter__(self) -> suppress_disposed:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_val is None:
            return False

        log_obj = self.logger or logging.getLogger("writeragent.errors")

        if is_disposed_exception(exc_val):
            log_obj.debug(
                "%s skipped (likely disposed): %s",
                self.action,
                exc_val,
                exc_info=self.exc_info,
            )
            return True

        if self.log_unexpected:
            log_obj.exception("Unexpected error during %s: %s", self.action, exc_val)

        return self.suppress_all


ignore_disposed = suppress_disposed


# Status values for tool execution results (cast/docs alias only — not TypedDict fields).
StatusValue = Literal["ok", "error"]


# TypedDict status fields use str, not Literal/StatusValue: CrossHair calls get_type_hints on
# TypedDicts when realizing Any-heap objects; Literal there TypeErrors and flakes check-all on
# importers (e.g. stream_normalizer via plugin.framework.client). Same rule as payload_codec ColumnKind.
class ToolResult(TypedDict, total=False):
    status: str
    code: str
    message: str
    details: dict[str, Any]


# Type for successful tool execution results
class ToolSuccess(TypedDict):
    status: str  # "ok"
    # Other fields are optional in success case


# Type for failed tool execution results
class ToolError(TypedDict):
    status: str  # "error"
    code: str
    message: str
    details: dict[str, Any]


def _resolve_exception_message(e: Any) -> str:
    """Extract non-empty message string from an exception, resolving UNO Exception Message attributes and causes."""
    msg = getattr(e, "Message", None) or str(e)
    if isinstance(msg, str):
        msg = msg.strip()
    else:
        msg = ""
    if not msg and isinstance(e, Exception):
        cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        if cause is not None:
            msg = getattr(cause, "Message", None) or str(cause)
            if isinstance(msg, str):
                msg = msg.strip()
            else:
                msg = ""
    if not msg:
        msg = type(e).__name__ if isinstance(e, Exception) else "Unknown error"
    return msg


class WriterAgentException(Exception):
    """Base exception for all WriterAgent errors.

    Backwards compatibility: some older code paths use `context=` while
    newer code uses `details=` for the JSON error payload.
    """

    code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: Any,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ):
        # Accept both `details` and legacy `context` (alias).
        if details is None and context is not None:
            details = context

        super().__init__(message)
        import sys

        if "crosshair" in sys.modules:
            self.message = "mock"
        else:
            self.message = _(_resolve_exception_message(message))
        if code is not None:
            self.code = code
        self.details = details or {}
        # Keep legacy attribute name too (some callers reference `.context`).
        self.context = self.details


class ConfigError(WriterAgentException):
    """Configuration, Auth, or Settings issues."""

    code: str = "CONFIG_ERROR"


class ConfigValidationError(ConfigError):
    """Validation issues with configuration keys/values."""

    code: str = "CONFIG_VALIDATION_ERROR"


class NetworkError(WriterAgentException):
    """HTTP/Network related failures."""

    code: str = "NETWORK_ERROR"


class ScriptingError(WriterAgentException):
    """Base exception for user scripting and external Python execution."""

    code: str = "SCRIPTING_ERROR"


class VenvError(ScriptingError):
    """Virtualenv configuration, resolution, or environment issues."""

    code: str = "VENV_ERROR"


class VenvNotFoundError(VenvError):
    """Configured Python virtual environment or interpreter executable not found."""

    code: str = "VENV_NOT_FOUND"


class VenvTimeoutError(ScriptingError):
    """Execution inside virtual environment exceeded configured timeout."""

    code: str = "VENV_TIMEOUT"


class VenvExecutionError(ScriptingError):
    """Python code execution in venv raised an unhandled exception or returned non-zero."""

    code: str = "VENV_EXEC_ERROR"


class WorkerIPCError(ScriptingError):
    """Host ↔ venv worker IPC frame encoding, decoding, or pipe communication failure."""

    code: str = "WORKER_IPC_ERROR"


class CalcError(WriterAgentException):
    """Calc spreadsheet manipulation and calculation failures."""

    code: str = "CALC_ERROR"


class FormulaError(CalcError):
    """Calc formula parsing, rebuilding, or evaluation failures."""

    code: str = "FORMULA_ERROR"


class FormulaSyntaxError(FormulaError):
    """Formula contains invalid Python or Calc syntax."""

    code: str = "FORMULA_SYNTAX_ERROR"


class SpillCollisionError(FormulaError):
    """Dynamic array formula cannot spill because destination cells are not empty."""

    code: str = "SPILL_COLLISION"


class ExcelConversionError(CalcError):
    """Failures during Excel ↔ DAG-style =PY conversion."""

    code: str = "EXCEL_CONVERSION_ERROR"


class SandboxSecurityError(ScriptingError):
    """Script attempted an operation or import forbidden by the sandbox policy."""

    code: str = "SANDBOX_SECURITY_ERROR"


class PayloadCodecError(ScriptingError):
    """Data encoding, decoding, or pickle serialization failure."""

    code: str = "PAYLOAD_CODEC_ERROR"


class DataShapeError(PayloadCodecError):
    """Data dimensions, row/column bounds, or cell count limits exceeded."""

    code: str = "DATA_SHAPE_ERROR"



@deal.post(lambda result: isinstance(result, dict) and result.get("status") == "error" and "code" in result and "message" in result)
@deal.ensure(
    lambda e, result: isinstance(e, WriterAgentException)
    or (result.get("code") == "INTERNAL_ERROR" and isinstance(result.get("details"), dict) and "type" in result["details"])
)
@deal.ensure(lambda e, result: not isinstance(e, WriterAgentException) or result.get("code") == e.code)
def format_error_payload(e: Exception) -> dict[str, Any]:
    """Format an exception into the standard JSON error payload schema."""
    if isinstance(e, WriterAgentException):
        payload: dict[str, Any] = {"status": "error", "code": e.code, "message": e.message}
        if e.details:
            payload["details"] = e.details
        return payload

    # For unexpected exceptions
    import sys

    if "crosshair" in sys.modules:
        err_type = "ValueError"
        err_msg = "mock"
    else:
        err_type = type(e).__name__
        err_msg = _resolve_exception_message(e)
    return {"status": "error", "code": "INTERNAL_ERROR", "message": err_msg, "details": {"type": err_type}}


# ── Centralized user-friendly error mapping (the single i18n mapper) ─────────
# Previously duplicated logic lived in plugin/framework/client/errors.py as
# format_error_message(). All code (tools, streams, logging, LLM client, HTTP
# requests, etc.) should now go through this one function for turning raw
# exceptions into localized, actionable advice for users.
#
# This is the companion to format_error_payload(): the former produces the
# structured dict used by tools/logs/UI; this one produces the plain friendly
# string used in logs, error messages, and as a fallback in display helpers.
#
# Wire-specific formatting (full HTTP response bodies, audio modality heuristics)
# remains in client/errors.py. Import format_error_message from this module
# (not from client.errors).

@deal.pre(lambda e: isinstance(e, Exception))
@deal.post(lambda result: isinstance(result, str))
def format_error_message(e: Exception) -> str:
    """Map common exceptions to user-friendly, localized advice.

    Keep this function focused on the common cross-cutting cases. Provider-
    specific or wire-format details belong in the LLM client layer.
    """
    import ssl
    import socket
    import http.client
    import urllib.error

    import sys

    if "crosshair" in sys.modules:
        msg = "mock"
    else:
        msg = str(e)
    if isinstance(e, ssl.SSLError):
        return _("TLS/SSL Error: {0}").format(msg)
    if isinstance(e, (urllib.error.HTTPError, http.client.HTTPResponse)):
        code_candidate = getattr(e, "code", None)
        if code_candidate is None:
            code_candidate = getattr(e, "status", None)
        try:
            code = int(code_candidate) if code_candidate is not None else 0
        except (TypeError, ValueError):
            code = 0
        if "crosshair" in sys.modules:
            reason = "mock"
        else:
            reason = str(getattr(e, "reason", "") or "")
        if code == 401:
            return _("Invalid API Key. Please check your settings.")
        if code == 403:
            return _("API access Forbidden. Your key may lack permissions for this model.")
        if code == 404:
            return _("Endpoint not found (404). Check your URL and Model name.")
        if code >= 500:
            return _("Server error ({0}). The AI provider is having issues.").format(code)
        return _("HTTP Error {0}: {1}").format(code, reason)

    if isinstance(e, VenvNotFoundError) or "venv not found" in msg.lower() or "no python executable found" in msg.lower():
        return _("Python venv not found. Open Settings → Python, set the venv path, then Test.")

    if isinstance(e, VenvTimeoutError) or "python timed out" in msg.lower() or "worker failed: timed out" in msg.lower():
        return _("Python execution timed out. Open Settings → Python to raise the timeout.")

    if isinstance(e, SpillCollisionError) or msg.strip() == "#SPILL!":
        return _("Formula spill collision: destination range contains non-empty cells.")

    if isinstance(e, SandboxSecurityError):
        return _("Script execution blocked by sandbox policy: {0}").format(getattr(e, "message", str(e)))

    if isinstance(e, socket.timeout) or "timed out" in msg.lower():
        return _("Request Timed Out. Try increasing 'Request Timeout' in Settings.")

    if isinstance(e, (urllib.error.URLError, OSError)):
        if "crosshair" in sys.modules:
            reason = "mock"
        elif isinstance(e, urllib.error.URLError):
            reason = str(getattr(e, "reason", None) or e)
        else:
            reason = str(e)
        if "Connection refused" in reason or "111" in reason:
            return _("Connection Refused. Is your local AI server (Ollama/LM Studio) running?")
        if "getaddrinfo failed" in reason:
            return _("DNS Error. Could not resolve the endpoint URL.")
        return _("Connection Error: {0}").format(reason)

    if "finish_reason=error" in msg:
        return _("The AI provider reported an error. Try again.")

    return msg




@deal.pre(lambda message, code="TOOL_EXECUTION_ERROR", **details: ascii_bounded(code, DEAL_MAX_TOKEN, min_len=1))
@deal.post(lambda result: isinstance(result, dict) and result.get("status") == "error" and "code" in result and "message" in result)
def make_tool_error(message: str, code: str = "TOOL_EXECUTION_ERROR", **details: Any) -> dict[str, Any]:
    """Central factory for all standardized tool error payloads."""
    return format_error_payload(ToolExecutionError(message, code=code, details=details))


class UnoObjectError(WriterAgentException):
    """LibreOffice UNO interface failures (stale docs, missing properties)."""

    code: str = "UNO_OBJECT_ERROR"


class DocumentDisposedError(UnoObjectError):
    """Document or UNO object was disposed during operation."""

    code: str = "DISPOSED_OBJECT"

    def __init__(
        self,
        message: Any,
        object_type: str = "Object",
        code: str | None = None,
        details: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ):
        super().__init__(message, code=code or self.code, details=details, context=context)
        self.object_type = object_type


class ResourceNotFoundError(WriterAgentException):
    """Configuration files, documents, or resources not found."""

    code: str = "RESOURCE_NOT_FOUND"

    def __init__(
        self,
        resource_type: str,
        identifier: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ):
        message = _("{resource_type} not found: {identifier}").format(resource_type=resource_type, identifier=identifier)
        super().__init__(message, code=code or self.code, details=details, context=context)
        self.resource_type = resource_type
        self.identifier = identifier


class WorkerPoolError(WriterAgentException):
    """Worker pool specific errors."""

    code: str = "WORKER_ERROR"


class ToolExecutionError(WriterAgentException):
    """Tool invocation and execution failures."""

    code: str = "TOOL_EXECUTION_ERROR"


class ToolPermissionError(WriterAgentException):
    """User rejected tool execution or permission denied."""

    code: str = "PERMISSION_DENIED"


class ToolContextError(WriterAgentException):
    """Tool Context lifecycle or service availability errors."""

    code: str = "CONTEXT_ERROR"


class WriterError(WriterAgentException):
    """Writer-specific errors."""

    code: str = "WRITER_ERROR"


class AgentParsingError(WriterAgentException):
    """LLM output / JSON parsing failures."""

    code: str = "PARSE_ERROR"


def check_disposed(model, context_name="Object"):
    """Check if a UNO object is disposed or None. Raises UnoObjectError/DocumentDisposedError if so."""
    if model is None:
        raise UnoObjectError(f"{context_name} is null", code="UNO_NULL_OBJECT")

    # Optional disposal check if the model supports it.
    if hasattr(model, "addEventListener"):
        # This is a crude heuristic; the definitive way is calling a method and catching DisposedException,
        # which safe_call handles, but this acts as an early guard if needed.
        pass


def is_document_disposed(doc: Any) -> bool:
    """Safely check if a UNO document or component is disposed or invalid."""
    if doc is None:
        return True
    if hasattr(doc, "getImplementationName"):
        try:
            _unused = doc.getImplementationName()
            return False
        except Exception:
            return True
    return False




def safe_uno_call(default=None):
    """Decorator to safely call UNO methods with automatic error handling, returning default on failure (disposal exceptions re-raised)."""

    def decorator(func):
        from functools import wraps

        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                e_name = type(e).__name__
                if "DisposedException" in e_name or isinstance(e, DocumentDisposedError):
                    raise DocumentDisposedError(
                        f"UNO object disposed during {func.__name__}",
                        object_type=func.__name__,
                        details={"args": str(args), "kwargs": str(kwargs), "original_error": str(e)},
                    ) from e
                logging.getLogger("writeragent.errors").debug(
                    "safe_uno_call: %s failed (%s), returning default %r",
                    func.__name__,
                    e,
                    default,
                )
                return default

        return wrapper

    return decorator


def handle_errors(context_name):
    """Decorator to catch exceptions and wrap them in WriterAgentException."""

    def decorator(fn):
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except WriterAgentException:
                raise
            except Exception as e:
                # We catch Exception here because pyuno bridge exceptions don't always inherit from Python's standard Exception cleanly in all builds,
                # but catching Exception is the standard way to grab them. We immediately wrap it.
                e_name = type(e).__name__
                if "DisposedException" in e_name or "RuntimeException" in e_name:
                    raise DocumentDisposedError(f"UNO object disposed during {context_name}", object_type=context_name, details={"original_error": str(e)}) from e
                else:
                    raise ToolExecutionError(f"{context_name} failed: {e}", code="INTERNAL_ERROR", details={"error": str(e), "type": e_name}) from e

        return wrapper

    return decorator


def safe_call(fn, context_name, *args, **kwargs):
    """Safely call a UNO method. If it raises any exception (e.g., DisposedException), wrap it in UnoObjectError or DocumentDisposedError."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        # Catch potential DisposedException and RuntimeException from UNO bridge
        e_name = type(e).__name__
        if "DisposedException" in e_name or "RuntimeException" in e_name:
            raise DocumentDisposedError(f"UNO object disposed during {context_name}", object_type=context_name, details={"original_error": str(e)}) from e

        # We catch Exception here because pyuno bridge exceptions don't always inherit from Python's standard Exception cleanly in all builds,
        # but catching Exception is the standard way to grab them. We immediately wrap it.
        raise UnoObjectError(f"{context_name} failed: {e}", details={"operation": context_name, "type": e_name}) from e


# Exception types and error helpers live here. safe_json_loads / safe_python_literal_eval
# are implemented in json_utils and re-exported as stable public imports (intentional).
__all__ = [
    "AgentParsingError",
    "CalcError",
    "ConfigError",
    "ConfigValidationError",
    "DataShapeError",
    "DocumentDisposedError",
    "ExcelConversionError",
    "FormulaError",
    "FormulaSyntaxError",
    "NetworkError",
    "PayloadCodecError",
    "ResourceNotFoundError",
    "SandboxSecurityError",
    "ScriptingError",
    "SpillCollisionError",
    "ToolContextError",
    "ToolExecutionError",
    "ToolPermissionError",
    "UnoObjectError",
    "VenvError",
    "VenvExecutionError",
    "VenvNotFoundError",
    "VenvTimeoutError",
    "WorkerIPCError",
    "WorkerPoolError",
    "WriterAgentException",
    "WriterError",
    "UNO_DISPOSED_EXCEPTIONS",
    "check_disposed",
    "format_error_message",      # The single i18n-friendly mapper (centralized here in 2026 janitor effort)
    "format_error_payload",
    "handle_errors",
    "ignore_disposed",
    "is_disposed_exception",
    "is_document_disposed",
    "make_tool_error",           # Central factory for all tool error dicts
    "safe_call",
    "safe_json_loads",
    "safe_python_literal_eval",
    "safe_uno_call",
    "suppress_disposed",
]

