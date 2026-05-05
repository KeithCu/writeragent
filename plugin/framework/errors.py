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

from plugin.framework.i18n import _
from plugin.framework.base_errors import ConfigError, NetworkError, WriterAgentException, format_error_payload
from plugin.framework.json_utils import safe_json_loads, safe_python_literal_eval


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
    "format_error_payload",
    "handle_errors",
    "safe_call",
    "safe_json_loads",
    "safe_python_literal_eval",
    "safe_uno_call",
]
