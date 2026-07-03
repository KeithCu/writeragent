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

from __future__ import annotations

from typing import Any, Literal, TypedDict

from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_json_loads, safe_python_literal_eval


# Status values for tool execution results
StatusValue = Literal["ok", "error"]


# Type for tool execution results (base type)
class ToolResult(TypedDict, total=False):
    status: StatusValue
    code: str
    message: str
    details: dict[str, Any]


# Type for successful tool execution results
class ToolSuccess(TypedDict):
    status: Literal["ok"]
    # Other fields are optional in success case


# Type for failed tool execution results
class ToolError(TypedDict):
    status: Literal["error"]
    code: str
    message: str
    details: dict[str, Any]


class WriterAgentException(Exception):
    """Base exception for all WriterAgent errors.

    Backwards compatibility: some older code paths use `context=` while
    newer code uses `details=` for the JSON error payload.
    """

    def __init__(self, message, code="INTERNAL_ERROR", context=None, details=None):
        # Accept both `context` and `details` (alias).
        if details is None and context is not None:
            details = context

        super().__init__(message)
        self.message = _(str(message))
        self.code = code
        self.details = details or {}
        # Keep legacy attribute name too (some callers reference `.context`).
        self.context = self.details


class ConfigError(WriterAgentException):
    """Configuration, Auth, or Settings issues."""

    def __init__(self, message, code="CONFIG_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class ConfigValidationError(ConfigError):
    """Validation issues with configuration keys/values."""

    def __init__(self, message, code="CONFIG_VALIDATION_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)



class NetworkError(WriterAgentException):
    """HTTP/Network related failures."""

    def __init__(self, message, code="NETWORK_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


def format_error_payload(e: Exception) -> dict[str, Any]:
    """Format an exception into the standard JSON error payload schema."""
    if isinstance(e, WriterAgentException):
        payload: dict[str, Any] = {"status": "error", "code": e.code, "message": e.message}
        if e.details:
            payload["details"] = e.details
        return payload

    # For unexpected exceptions
    return {"status": "error", "code": "INTERNAL_ERROR", "message": str(e), "details": {"type": type(e).__name__}}


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
# remains in client/errors.py as a thin adapter + specialized helpers.
# See client/errors.py for the rationale and the thin re-exports.

def format_error_message(e: Exception) -> str:
    """Map common exceptions to user-friendly, localized advice.

    This is the single source of truth for turning raw network/HTTP/SSL/timeout
    errors into messages suitable for end users or logs. It always returns a
    string that has already been passed through gettext _().

    Keep this function focused on the common cross-cutting cases. Provider-
    specific or wire-format details belong in the LLM client layer.
    """
    import ssl
    import socket
    import http.client
    import urllib.error

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

    if isinstance(e, socket.timeout) or "timed out" in msg.lower():
        return _("Request Timed Out. Try increasing 'Request Timeout' in Settings.")

    if isinstance(e, (urllib.error.URLError, OSError)):
        if isinstance(e, urllib.error.URLError):
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


def make_tool_error(message: str, code: str = "TOOL_EXECUTION_ERROR", **details: Any) -> dict[str, Any]:
    """Central factory for all standardized tool error payloads.

    Every path that produces a tool error dict (ToolBase._tool_error,
    ToolBaseDummy._tool_error, ToolRegistry.execute error paths, etc.)
    should go through this helper. This guarantees identical structure,
    consistent use of ToolExecutionError + format_error_payload, and a
    single place to evolve the schema or add logging in the future.

    This was introduced as part of centralizing error formatting (see
    the janitor plan item for error unification).
    """
    return format_error_payload(ToolExecutionError(message, code=code, details=details))


class UnoObjectError(WriterAgentException):
    """LibreOffice UNO interface failures (stale docs, missing properties)."""

    def __init__(self, message, code="UNO_OBJECT_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class DocumentDisposedError(UnoObjectError):
    """Document or UNO object was disposed during operation."""

    def __init__(self, message, object_type="Object", context=None, details=None):
        super().__init__(message, code="DISPOSED_OBJECT", context=context, details=details)
        self.object_type = object_type


class ResourceNotFoundError(WriterAgentException):
    """Configuration files, documents, or resources not found."""

    def __init__(self, resource_type, identifier, context=None, details=None):
        message = _("{resource_type} not found: {identifier}").format(resource_type=resource_type, identifier=identifier)
        super().__init__(message, code="RESOURCE_NOT_FOUND", context=context, details=details)
        self.resource_type = resource_type
        self.identifier = identifier


class WorkerPoolError(WriterAgentException):
    """Worker pool specific errors."""

    def __init__(self, message, code="WORKER_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)
        self.task_context = context


class ToolExecutionError(WriterAgentException):
    """Tool invocation and execution failures."""

    def __init__(self, message, code="TOOL_EXECUTION_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class ToolPermissionError(WriterAgentException):
    """User rejected tool execution or permission denied."""

    def __init__(self, message, code="PERMISSION_DENIED", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class ToolContextError(WriterAgentException):
    """Tool Context lifecycle or service availability errors."""

    def __init__(self, message, code="CONTEXT_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class WriterError(WriterAgentException):
    """Writer-specific errors."""

    def __init__(self, message, code="WRITER_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class AgentParsingError(WriterAgentException):
    """LLM output / JSON parsing failures."""

    def __init__(self, message, code="PARSE_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


def check_disposed(model, context_name="Object"):
    """Check if a UNO object is disposed or None. Raises UnoObjectError/DocumentDisposedError if so."""
    if model is None:
        raise UnoObjectError(f"{context_name} is null", code="UNO_NULL_OBJECT")

    # Optional disposal check if the model supports it.
    if hasattr(model, "addEventListener"):
        # This is a crude heuristic; the definitive way is calling a method and catching DisposedException,
        # which safe_call handles, but this acts as an early guard if needed.
        pass


def safe_uno_call(default=None):
    """Decorator to safely call UNO methods with automatic error handling."""

    def decorator(func):
        from functools import wraps

        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Catch potential DisposedException and RuntimeException from UNO bridge
                e_name = type(e).__name__
                if "DisposedException" in e_name or "RuntimeException" in e_name:
                    raise DocumentDisposedError(f"UNO object disposed during {func.__name__}", object_type=func.__name__, details={"args": str(args), "kwargs": str(kwargs), "original_error": str(e)}) from e
                else:
                    raise UnoObjectError(f"UNO call {func.__name__} failed", details={"error": str(e), "type": e_name}) from e

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
        raise UnoObjectError(f"{context_name} failed: {e}", context={"operation": context_name, "type": e_name}) from e


# Re-export base/json helpers so callers can use `from plugin.framework.errors import ...` (public API).
__all__ = [
    "AgentParsingError",
    "ConfigError",
    "DocumentDisposedError",
    "NetworkError",
    "ResourceNotFoundError",
    "ToolContextError",
    "ToolExecutionError",
    "ToolPermissionError",
    "UnoObjectError",
    "WorkerPoolError",
    "WriterAgentException",
    "WriterError",
    "check_disposed",
    "format_error_message",      # The single i18n-friendly mapper (centralized here in 2026 janitor effort)
    "format_error_payload",
    "handle_errors",
    "make_tool_error",           # Central factory for all tool error dicts
    "safe_call",
    "safe_json_loads",
    "safe_python_literal_eval",
    "safe_uno_call",
]
