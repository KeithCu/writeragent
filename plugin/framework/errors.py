"""
Centralized exception hierarchy for WriterAgent.

All custom exceptions should inherit from WriterAgentException.
"""


import json


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
        self.message = message
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


class ToolExecutionError(WriterAgentException):
    """Tool invocation and execution failures."""

    def __init__(self, message, code="TOOL_EXECUTION_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class AgentParsingError(WriterAgentException):
    """LLM output / JSON parsing failures."""

    def __init__(self, message, code="PARSE_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


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


def safe_json_loads(text, default=None):
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
    except (json.JSONDecodeError, TypeError, ValueError):
        return default
