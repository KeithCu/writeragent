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


import json
import ast
import re
from typing import Any

from plugin.framework.types import ToolResult, ToolError


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


class DocumentDisposedError(UnoObjectError):
    """Document or UNO object was disposed during operation."""

    def __init__(self, message, object_type="Object", context=None, details=None):
        super().__init__(message, code="DISPOSED_OBJECT", context=context, details=details)
        self.object_type = object_type


class ResourceNotFoundError(WriterAgentException):
    """Configuration files, documents, or resources not found."""

    def __init__(self, resource_type, identifier, context=None, details=None):
        from plugin.framework.i18n import _
        message = _("{resource_type} not found: {identifier}").format(
            resource_type=resource_type, identifier=identifier
        )
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
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Catch potential DisposedException and RuntimeException from UNO bridge
                e_name = type(e).__name__
                if "DisposedException" in e_name or "RuntimeException" in e_name:
                    raise DocumentDisposedError(
                        f"UNO object disposed during {func.__name__}",
                        object_type=func.__name__,
                        details={"args": str(args), "kwargs": str(kwargs), "original_error": str(e)}
                    ) from e
                else:
                    raise UnoObjectError(
                        f"UNO call {func.__name__} failed",
                        details={"error": str(e), "type": e_name}
                    ) from e
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
            raise DocumentDisposedError(
                f"UNO object disposed during {context_name}",
                object_type=context_name,
                details={"original_error": str(e)}
            ) from e
        
        # We catch Exception here because pyuno bridge exceptions don't always inherit from Python's standard Exception cleanly in all builds,
        # but catching Exception is the standard way to grab them. We immediately wrap it.
        raise UnoObjectError(f"{context_name} failed: {e}", context={"operation": context_name, "type": e_name}) from e

def format_error_payload(e: Exception) -> dict[str, Any]:
    """Format an exception into the standard JSON error payload schema."""
    if isinstance(e, WriterAgentException):
        payload: dict[str, Any] = {
            "status": "error",
            "code": e.code,
            "message": e.message,
        }
        if e.details:
            payload["details"] = e.details
        return payload

    # For unexpected exceptions
    return {
        "status": "error",
        "code": "INTERNAL_ERROR",
        "message": str(e),
        "details": {
            "type": type(e).__name__,
        },
    }


def repair_json(text: str) -> str:
    """Attempt to repair common JSON syntax errors from LLMs using json-repair.

    Handles:
    1. Truncated JSON (missing closing braces/brackets)
    2. Trailing commas
    3. Unquoted keys
    4. Single quotes vs double quotes
    5. Missing values

    Returns:
        The repaired JSON string.
    """
    if not isinstance(text, str):
        return text

    repaired = text.strip()
    if not repaired:
        return repaired

    import json_repair
    return str(json_repair.repair_json(repaired))


def safe_json_loads(text: Any, default: Any = None, strict: bool = False) -> Any:
    """Safely parse a JSON string into a Python object with optional robust repair logic.

    Attempts:
    1. Standard json.loads
    2. json.loads with strict=False (handles raw control chars, per hermes-agent)
    3. repair_json + json.loads (LLM/Robust mode only)
    4. ast.literal_eval as final fallback (LLM/Robust mode only)

    Args:
        text: The string to parse.
        default: The value to return if parsing fails. Defaults to None.
        strict: If True, only use standard JSON parsing (no repair). Defaults to False.

    Returns:
        The parsed Python object or the default value if an error occurs.
    """
    if not isinstance(text, (str, bytes, bytearray)):
        return default

    # Ensure we are working with a string for repair logic
    raw_text = text.decode("utf-8", errors="replace") if isinstance(text, (bytes, bytearray)) else text
    stripped = raw_text.strip()
    if not stripped:
        return default

    # 1. Standard attempt
    try:
        parsed = json.loads(stripped)
        return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # 2. strict=False attempt (handles bare control characters)
    try:
        parsed = json.loads(stripped, strict=False)
        return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # In strict mode, we stop here.
    if strict:
        return default

    # 3. ast.literal_eval fallback (handles single quotes and Python-isms)
    # Inspired by hermes-agent/environments/tool_call_parsers/qwen3_coder_parser.py
    try:
        # literal_eval handles 'True', 'False', 'None' out of the box.
        # It also handles single quotes and tuple-like syntax.
        parsed = ast.literal_eval(stripped)
        return parsed if parsed is not None else default
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass

    # 4. Repair attempt for truncated or malformed JSON
    try:
        repaired = repair_json(stripped)
        if repaired != stripped:
            parsed = json.loads(repaired, strict=False)
            return parsed if parsed is not None else default
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

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
    # Use strict=True as literal_eval fallback is handled separately below for booleans/strings.
    data = safe_json_loads(stripped, default=None, strict=True)
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
    if (
        isinstance(stripped, str)
        and len(stripped) >= 2
        and stripped[0] == "'"
        and stripped[-1] == "'"
    ):
        inner = stripped[1:-1]
        # Only unquote if it's a simple string (no internal single quotes or backslashes)
        if "'" not in inner and "\\" not in inner:
            return inner

    return default
