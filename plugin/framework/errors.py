"""
Centralized exception hierarchy for WriterAgent.

All custom exceptions should inherit from WriterAgentException.
"""


import json
from typing import Any


class WriterAgentException(Exception):
    """Base exception for all WriterAgent errors.

    Backwards compatibility: some older code paths use `context=` while
    newer code uses `details=` for the JSON error payload.
    """

    def __init__(self, message, code="INTERNAL_ERROR", context=None, details=None):
        # Accept both `context` and `details` (alias).
        if details is None and context is not None:
            details = context

        from plugin.framework.i18n import _
        super().__init__(message)
        self.message = _(message)
        self.code = code
        self.details = details or {}
        # Keep legacy attribute name too (some callers reference `.context`).
        self.context = self.details


class ConfigError(WriterAgentException):
    """Configuration, Auth, or Settings issues."""

    def __init__(self, message, code="CONFIG_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class NetworkError(WriterAgentException):
    """HTTP/Network related failures."""

    def __init__(self, message, code="NETWORK_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class UnoObjectError(WriterAgentException):
    """LibreOffice UNO interface failures (stale docs, missing properties)."""

    def __init__(self, message, code="UNO_OBJECT_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class WorkerPoolError(WriterAgentException):
    """Worker pool specific errors."""

    def __init__(self, message, code="WORKER_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)
        self.task_context = context


class ToolExecutionError(WriterAgentException):
    """Tool invocation and execution failures."""

    def __init__(self, message, code="TOOL_EXECUTION_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class AgentParsingError(WriterAgentException):
    """LLM output / JSON parsing failures."""

    def __init__(self, message, code="PARSE_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


def check_disposed(model, context_name="Object"):
    """Check if a UNO object is disposed or None. Raises UnoObjectError if so."""
    if model is None:
        raise UnoObjectError(f"{context_name} is null", code="UNO_NULL_OBJECT")

    # Optional disposal check if the model supports it.
    if hasattr(model, "addEventListener"):
        # This is a crude heuristic; the definitive way is calling a method and catching DisposedException,
        # which safe_call handles, but this acts as an early guard if needed.
        pass

def safe_call(fn, context_name, *args, **kwargs):
    """Safely call a UNO method. If it raises any exception (e.g., DisposedException), wrap it in UnoObjectError."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        # We catch Exception here because pyuno bridge exceptions don't always inherit from Python's standard Exception cleanly in all builds,
        # but catching Exception is the standard way to grab them. We immediately wrap it.
        raise UnoObjectError(f"{context_name} failed: {e}", context={"operation": context_name, "type": type(e).__name__}) from e

def format_error_payload(e: Exception) -> dict:
    """Format an exception into the standard JSON error payload schema."""
    if isinstance(e, WriterAgentException):
        payload = {"status": "error", "code": e.code, "message": e.message}
        if e.details:
            payload["details"] = e.details
        return payload

    return {
        "status": "error",
        "code": "INTERNAL_ERROR",
        "message": str(e),
        "details": {"type": type(e).__name__},
    }


def safe_json_loads(text: Any, default: Any = None) -> Any:
    """Safely parse a JSON string into a Python object.

    Args:
        text: The string to parse.
        default: The value to return if parsing fails. Defaults to None.

    Returns:
        The parsed Python object or the default value if an error occurs.
    """
    if not isinstance(text, (str, bytes, bytearray)):
        return default
    try:
        parsed = json.loads(text)
        return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        # Catch RecursionError to prevent DoS from deeply nested structures
        return default


def safe_python_literal_eval(text: Any, default: Any = None) -> Any:
    """Safely parse a Python-style literal (e.g. from an LLM) without using ast.literal_eval.
    Supports scalars (bool, None, number, string) and simple JSON-compatible lists/dicts.
    Returns the default value if it doesn't look like a simple literal.

    Args:
        text: The string to parse.
        default: The value to return if parsing fails. Defaults to None.

    Returns:
        The parsed Python object or the default value if an error occurs.
    """
    if not isinstance(text, (str, bytes, bytearray)):
        return default

    stripped = text.strip()
    if not stripped:
        return default

    # 1. Try standard JSON first (handles numbers, double-quoted strings, bools, null)
    data = safe_json_loads(stripped, default=None)
    if data is not None:
        return data

    # 2. Handle Python-style booleans and None (which JSON calls true/false/null)
    # Case-insensitive checks to handle various LLM formatting quirks robustly
    lower = stripped.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("none", "null"):
        return None

    # 3. Handle simple single-quoted string unquoting: 'abc' -> abc
    # This avoids ast.literal_eval for basic string normalization.
    if len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
        inner = stripped[1:-1]
        # Only unquote if it's a simple string (no internal single quotes or backslashes)
        if "'" not in inner and "\\" not in inner:
            return inner

    return default
