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
import difflib
import errno
import logging
from typing import TYPE_CHECKING, Any, Callable, NoReturn, NotRequired, TypeVar, TypedDict, overload

if TYPE_CHECKING:
    from collections.abc import Mapping

from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_python_literal_eval  # noqa: F401  # pyright: ignore[reportUnusedImport]

from plugin.framework.deal_shim import DEAL_MAX_MSGID, deal

_FILESYSTEM_ERRNOS: frozenset[int] = frozenset(
    val
    for name in ("ENOSPC", "ENOTEMPTY", "EROFS", "EDQUOT", "EFBIG", "ENAMETOOLONG", "EBUSY")
    if (val := getattr(errno, name, None)) is not None
)
_TRANSPORT_ERRNOS: frozenset[int] = frozenset(
    val
    for name in ("EHOSTUNREACH", "ENETUNREACH", "EINTR")
    if (val := getattr(errno, name, None)) is not None
)
_FILESYSTEM_CLASSES: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    PermissionError,
    IsADirectoryError,
    NotADirectoryError,
    FileExistsError,
    BlockingIOError,
)


def similar_names(wanted: str, names: list[str]) -> list[str]:
    """Up to five close name matches for tool miss hints (difflib cutoff 0.4)."""
    if not wanted:
        return []
    return difflib.get_close_matches(wanted, names, n=5, cutoff=0.4)


def name_miss_details(wanted: str, names: list[str], list_tool: str) -> dict[str, Any]:
    """``details`` payload for a named-object miss: always ``hint``, optional ``similar``."""
    out: dict[str, Any] = {"hint": "Call %s to list names." % list_tool}
    similar = similar_names(wanted, names)
    if similar:
        out["similar"] = similar
    return out


def available_miss_details(wanted: str, available: list[str]) -> dict[str, Any]:
    """Miss hints when the legal set is already in ``details.available``."""
    out: dict[str, Any] = {"hint": "Valid names are in details.available."}
    similar = similar_names(wanted, [str(x) for x in available])
    if similar:
        out["similar"] = similar
    return out


class SuggestedCall(TypedDict):
    """Complete tool call recommendation that a model can send unchanged."""

    tool: str
    arguments: dict[str, Any]


class Diagnostic(TypedDict):
    """Structured diagnostic reported on edit failures or partial applications.

    Carries a machine-readable code, a human-readable message, an optional
    suggested_call so the model can send it as its next tool call unchanged,
    and optional flat context fields (declare any new context keys here).
    """

    code: str
    message: str
    suggested_call: NotRequired[SuggestedCall]
    matches_skipped: NotRequired[int]
    matches_total: NotRequired[int]
    link_target: NotRequired[str]


def suggest_call(tool: str, **arguments: Any) -> SuggestedCall:
    """Build a suggested_call dictionary from a tool name and arguments.

    Constructs a complete invocation payload matching the internal tool execution
    convention so the model can send it as its next tool call unchanged.
    """
    return {"tool": tool, "arguments": arguments}


def is_disposal(exc: BaseException) -> bool:
    """Return True if exc represents a UNO object disposal."""
    return isinstance(exc, DocumentDisposedError) or "DisposedException" in type(exc).__name__


def _looks_like_teardown(exc: BaseException) -> bool:
    """Return True if exc looks like bridge teardown or disposal."""
    return is_disposal(exc) or "RuntimeException" in type(exc).__name__


_T = TypeVar("_T")


@overload
def best_effort(fn: Callable[[], _T], what: str) -> _T | None: ...
@overload
def best_effort(fn: Callable[[], _T], what: str, default: _T) -> _T: ...
def best_effort(fn: Callable[[], _T], what: str, default: _T | None = None) -> _T | None:
    """Return ``fn()``, or *default* after a DEBUG log of *what* with the traceback.

    The call form of ``except Exception: log.debug(what, exc_info=True)``. It
    intentionally swallows every ``Exception``, disposal included: use it only
    for cosmetic or cleanup steps whose failure must not fail the caller. A
    UNO read that should report a closed document is :func:`uno_read`.
    """
    try:
        return fn()
    except Exception:
        logging.getLogger("writeragent.errors").debug("%s", what, exc_info=True)
        return default


class suppress_disposed(contextlib.ContextDecorator):
    """Context manager and decorator to safely execute UI / UNO lifecycle actions.

    Disposed UNO objects (and related bridge teardown exceptions) are caught, logged at DEBUG
    level, and suppressed.

    Unexpected non-disposal exceptions are logged (via logger.exception) and,
    if suppress_all is True (default for UI lifecycle blocks), suppressed so they do not crash host UI event loops.
    KeyboardInterrupt, SystemExit, and GeneratorExit always propagate.
    """

    action: str
    logger: logging.Logger | None
    suppress_all: bool
    exc_info: bool

    def __init__(self, action: str = "action", *, logger: logging.Logger | None = None, suppress_all: bool = True, exc_info: bool = False) -> None:
        self.action = action
        self.logger = logger
        self.suppress_all = suppress_all
        self.exc_info = exc_info

    def __enter__(self) -> suppress_disposed:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any) -> bool:
        if exc_val is None:
            return False

        # suppress_all (default True) covers Exception only. KeyboardInterrupt,
        # SystemExit, and GeneratorExit must still propagate.
        if exc_type is not None and not issubclass(exc_type, Exception):
            return False

        log_obj = self.logger or logging.getLogger("writeragent.errors")

        if _looks_like_teardown(exc_val):
            log_obj.debug("%s skipped (likely disposed): %s", self.action, exc_val, exc_info=self.exc_info)
            return True

        log_obj.exception("Unexpected error during %s: %s", self.action, exc_val)

        return self.suppress_all


def _exception_text(obj: Any) -> str:
    """Non-empty Message, else str. Either read can throw on a hostile UNO proxy."""
    try:
        raw_msg = getattr(obj, "Message", None)
        if isinstance(raw_msg, str) and raw_msg.strip():
            return raw_msg.strip()
    except Exception as exc:
        logging.getLogger("writeragent.errors").debug("resolve_exception_message: Message unreadable (%s)", type(exc).__name__)

    # str() on a UNO struct walks PyUNO fields and takes SolarMutex (see logging._logged_uno_label).
    # When Message is empty, skip str() to avoid deadlocks on background threads holding locks.
    if getattr(type(obj), "__pyunostruct__", None) is not None:
        return ""

    try:
        raw_str = str(obj)
        if isinstance(raw_str, str) and raw_str.strip():
            return raw_str.strip()
    except Exception as exc:
        logging.getLogger("writeragent.errors").debug("resolve_exception_message: str(exception) failed (%s)", type(exc).__name__)
    return ""


def resolve_exception_message(e: Any) -> str:
    """Extract non-empty message string from an exception, resolving UNO Exception Message attributes and causes."""
    msg = _exception_text(e)
    if not msg:
        cause = None
        try:
            cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        except Exception as exc:
            logging.getLogger("writeragent.errors").debug("resolve_exception_message: cause lookup failed (%s)", type(exc).__name__)
        if cause is not None and cause is not e:
            msg = _exception_text(cause)
    if not msg:
        if isinstance(e, BaseException):
            msg = type(e).__name__.strip() or "BaseException"
        else:
            msg = "Unknown error"
    return msg


class BlockingWaitStopped(Exception):
    """Raised by llm_request_lane when the captured send scope is already cancelled while waiting for the LLM lock.

    Callers turn it into a Stopped queue item; intentionally not a WriterAgentException.
    """


class WriterAgentException(Exception):
    """Base exception for all WriterAgent errors."""

    code: str = "INTERNAL_ERROR"
    message: str
    details: dict[str, Any]

    def __init__(self, message: Any, *, code: str | None = None, details: dict[str, Any] | None = None) -> None:
        # Runtime / interpolated strings are not in the gettext catalog; _() is a no-op
        # unless the exact source string was extracted. Long runtime text skips _()
        # to avoid deal.PreContractError (past DEAL_MAX_MSGID, not in catalog).
        text = resolve_exception_message(message)
        if len(text) <= DEAL_MAX_MSGID:
            text = _(text)
        super().__init__(text)
        self.message = text
        if code is not None:
            self.code = code
        self.details = details or {}


class ConfigError(WriterAgentException):
    """Configuration, Auth, or Settings issues."""

    code: str = "CONFIG_ERROR"


class ConfigValidationError(ConfigError):
    """Validation issues with configuration keys/values."""

    code: str = "CONFIG_VALIDATION_ERROR"


class NetworkError(WriterAgentException):
    """HTTP/Network related failures."""

    code: str = "NETWORK_ERROR"


class CalcError(WriterAgentException):
    """Calc spreadsheet manipulation and calculation failures."""

    code: str = "CALC_ERROR"


@deal.post(lambda result: isinstance(result, dict) and result.get("status") == "error" and "code" in result and "message" in result)
@deal.ensure(lambda e, result, suggested_call=None: isinstance(e, WriterAgentException) or (result.get("code") == "INTERNAL_ERROR" and isinstance(result.get("details"), dict) and "type" in result["details"]))
@deal.ensure(lambda e, result, suggested_call=None: not isinstance(e, WriterAgentException) or result.get("code") == e.code)
def format_error_payload(e: BaseException, *, suggested_call: SuggestedCall | None = None) -> dict[str, Any]:
    """Format an exception into the standard JSON error payload schema.

    When a suggested_call is provided, it is attached at the top level alongside
    code and message so the model can send it as its next tool call unchanged.
    """
    if isinstance(e, WriterAgentException):
        payload: dict[str, Any] = {"status": "error", "code": e.code, "message": e.message}
        if e.details:
            payload["details"] = e.details
    else:
        # For unexpected exceptions, capture the Python exception type in details
        # so callers can distinguish unhandled crashes from intended error codes.
        err_type = type(e).__name__
        err_msg = resolve_exception_message(e)
        payload = {"status": "error", "code": "INTERNAL_ERROR", "message": err_msg, "details": {"type": err_type}}

    if suggested_call is not None:
        payload["suggested_call"] = suggested_call
    return payload


# i18n mapper for raw exceptions. format_error_payload is the structured dict.
# Wire-specific HTTP bodies and audio/image heuristics stay in client/errors.py.


def _is_connection_refused(reason_obj: BaseException | None, reason: str) -> bool:
    """True for a refused TCP connect, not for the digits 111 in an unrelated message."""
    if isinstance(reason_obj, ConnectionRefusedError) or getattr(reason_obj, "errno", None) == errno.ECONNREFUSED:
        return True
    return "connection refused" in reason.lower()


def http_status_message(status: int) -> str | None:
    """Return common user-friendly advice for HTTP status codes, or None if unmapped."""
    if status == 401:
        return _("Invalid API Key. Please check your settings.")
    if status == 403:
        return _("API access Forbidden. Your key may lack permissions for this model.")
    if status == 404:
        return _("Endpoint not found (404). Check your URL and Model name.")
    if status == 429:
        return _("Rate limited (429). Wait a moment and try again.")
    return None


@deal.post(lambda result: isinstance(result, str))
def format_error_message(e: BaseException) -> str:
    """Map common exceptions to user-friendly, localized advice.

    Keep this function focused on the common cross-cutting cases. Provider-
    specific or wire-format details belong in the LLM client layer.
    """
    import ssl
    import socket
    import urllib.error

    msg = resolve_exception_message(e)
    if isinstance(e, ssl.SSLError):
        return _("TLS/SSL Error: {0}").format(msg)
    # Only HTTPError carries a status. RemoteDisconnected, BadStatusLine,
    # and IncompleteRead have no .code/.status/.reason; treating every
    # HTTPException like HTTPError discarded str(e) and reported "HTTP Error 0".
    # Those fall through to the connection/OSError path or the final str(e).
    if isinstance(e, urllib.error.HTTPError):
        try:
            code = int(e.code)
        except (TypeError, ValueError, AttributeError):
            code = 0
        mapped = http_status_message(code)
        if mapped is not None:
            return mapped
        if code >= 500:
            return _("Server error ({0}). The AI provider is having issues.").format(code)
        reason = str(getattr(e, "reason", "") or "")
        return _("HTTP Error {0}: {1}").format(code, reason)

    # Typed exceptions first. Substring checks below are last-resort for
    # stdlib/HTTP-library strings we do not own a subclass for.
    if isinstance(e, ConfigError) and e.code == "missing_api_key":
        provider = str(e.details.get("provider") or "").strip()
        if provider and provider != "custom":
            return _("No API key configured for {0}. Open Settings and add a key.").format(provider)
        return _("No API key configured. Open Settings and add a key.")

    # Filesystem errors are not a down local server. Match connection-shaped
    # transport errors, not filesystem classes or errnos.
    reason_obj: BaseException | None
    if isinstance(e, urllib.error.URLError):
        raw_reason = getattr(e, "reason", None)
        reason_obj = raw_reason if isinstance(raw_reason, BaseException) else None
    else:
        reason_obj = e

    if reason_obj is not None:
        reason = resolve_exception_message(reason_obj)
    else:
        reason = str(getattr(e, "reason", None) or e)

    is_fs = isinstance(e, _FILESYSTEM_CLASSES) or (getattr(e, "errno", None) in _FILESYSTEM_ERRNOS)
    if isinstance(e, (urllib.error.URLError, OSError)) and not is_fs:
        rl = reason.lower()
        # URLError is not itself a socket.timeout; the timeout is e.reason.
        # Check that before the connection-error sentence, or a wrapped
        # timeout tells the user the server is down.
        if isinstance(reason_obj, (socket.timeout, TimeoutError)):
            phrase = reason.strip().lower()
            if phrase == "timed out" or "the read operation timed out" in phrase:
                return _("Request Timed Out. Try increasing 'Request Timeout' in Settings.")
            return reason
        # urllib.request wraps a certificate failure as URLError(reason=SSLError).
        if isinstance(reason_obj, ssl.SSLError):
            return _("TLS/SSL Error: {0}").format(reason)
        if "timed out" in rl and "formula" not in rl:
            return _("Request Timed Out. Try increasing 'Request Timeout' in Settings.")
        # Errno text and unrelated messages both contain "111" (port 1111).
        # Match the errno or the words, not that substring.
        if _is_connection_refused(reason_obj, reason):
            return _("Connection Refused. Is your local AI server (Ollama/LM Studio) running?")
        if "getaddrinfo failed" in rl:
            return _("DNS Error. Could not resolve the endpoint URL.")
        if (
            isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, socket.gaierror, socket.herror))
            or getattr(e, "errno", None) in _TRANSPORT_ERRNOS
        ):
            return _("Connection Error: {0}").format(reason)

    lower = msg.lower()
    if "venv not found" in lower or "no python executable found" in lower:
        return _("Python venv not found. Open Settings → Python, set the venv path, then Test.")
    if "python timed out" in lower or "python execution timed out" in lower or "worker failed: timed out" in lower:
        return _("Python execution timed out. Open Settings → Python to raise the timeout.")
    if msg.strip() == "#SPILL!":
        return _("Formula spill collision: destination range contains non-empty cells.")
    if "finish_reason=error" in msg:
        return _("The AI provider reported an error. Try again.")

    return msg


def format_error_for_display(e: Any) -> str:
    """Return user-friendly error string for display in cells or dialogs."""
    # Drain loop ERROR items are format_error_payload dicts, not Exception.
    # format_error_payload(dict) would wrap the whole mapping as INTERNAL_ERROR.
    if isinstance(e, dict):
        msg = e.get("message") or e.get("code") or str(e)
        return _("Error: {0}").format(msg)
    if isinstance(e, WriterAgentException):
        message = e.message
    elif isinstance(e, BaseException):
        message = format_error_message(e)
    else:
        message = resolve_exception_message(e)
    return _("Error: {0}").format(message)


_RESERVED_TOOL_ERROR_KEYS: frozenset[str] = frozenset(
    {"status", "code", "message", "details", "suggested_call"}
)


@deal.post(lambda result: isinstance(result, dict) and result.get("status") == "error" and "code" in result and "message" in result)
def make_tool_error(
    message: Any,
    code: str = "TOOL_EXECUTION_ERROR",
    *,
    suggested_call: SuggestedCall | None = None,
    top_level: Mapping[str, Any] | None = None,
    **details: Any,
) -> dict[str, Any]:
    """Central factory for all standardized tool error payloads.

    A long UNO or provider string is a runtime message. Construction goes
    through ``WriterAgentException``, which does not apply the gettext msgid
    length contract to that text. Extra keyword arguments nest under details.
    Optional top_level fields sit beside code and message for clients that
    branch on one key; use it instead of writing into the returned dict.
    """
    if not isinstance(code, str) or not code:
        code = "TOOL_EXECUTION_ERROR"
    if top_level is not None:
        forbidden = set(top_level).intersection(_RESERVED_TOOL_ERROR_KEYS)
        if forbidden:
            raise ValueError(f"top_level cannot override reserved error keys: {sorted(forbidden)}")
    payload = format_error_payload(
        ToolExecutionError(message, code=code, details=details),
        suggested_call=suggested_call,
    )
    if top_level:
        payload.update(top_level)
    return payload


def user_stopped_payload(message: str = "Document read stopped by user.") -> dict[str, Any]:
    return {"status": "error", "code": "USER_STOPPED", "message": message}


class UnoObjectError(WriterAgentException):
    """LibreOffice UNO interface failures (stale docs, missing properties)."""

    code: str = "UNO_OBJECT_ERROR"


class DocumentDisposedError(WriterAgentException):
    """Document or UNO object was disposed during operation."""

    # Same code as execute_safe / tool DOCUMENT_DISPOSED so callers comparing
    # codes do not miss one of the two historical spellings.
    code: str = "DOCUMENT_DISPOSED"
    object_type: str

    def __init__(self, message: Any, object_type: str = "Object", code: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=code or self.code, details=details)
        self.object_type = object_type


class WorkerPoolError(WriterAgentException):
    """Worker pool specific errors."""

    code: str = "WORKER_ERROR"


class ToolExecutionError(WriterAgentException):
    """Tool invocation and execution failures."""

    code: str = "TOOL_EXECUTION_ERROR"


class AgentParsingError(WriterAgentException):
    """LLM output / JSON parsing failures."""

    code: str = "PARSE_ERROR"


def document_is_disposed(doc: Any) -> bool:
    """Safely check if a UNO document or component is disposed or invalid."""
    if doc is None:
        return True
    try:
        impl_fn = getattr(doc, "getImplementationName", None)
        if callable(impl_fn):
            impl_fn()
        return False
    except Exception as exc:
        return is_disposal(exc)


def _uno_disposal_message(exc: BaseException) -> str:  # pyright: ignore[reportUnusedFunction]  # cross-module helper; imported by uno_context.py and scratch_writer.py
    """Resolve exception text, falling back to standard UNO disposal sentence."""
    msg = resolve_exception_message(exc)
    if msg == type(exc).__name__:
        return "UNO object was disposed"
    return msg


def reraise_if_disposed(
    exc: BaseException,
    message: str = "Document disposed during operation",
    *,
    object_type: str = "document",
) -> None:
    """Disposed UNO is a closed document or component, not a generic tool failure."""
    if isinstance(exc, DocumentDisposedError):
        raise exc
    if is_disposal(exc):
        raise DocumentDisposedError(message, object_type=object_type) from exc


def uno_read(fn: Callable[[], _T], default: _T, *, object_type: str = "document") -> _T:
    """Return fn(), or default when it raises an ordinary Exception."""
    try:
        return fn()
    except Exception as exc:
        reraise_if_disposed(exc, object_type=object_type)
        return default


def uno_write(fn: Callable[[], Any]) -> bool:
    """One UNO write. Re-raise the original disposal exception; False on any other Exception.

    Does not log: callers surface failure via return value (e.g. failed property names to the model);
    the underlying exception is intentionally dropped, not traced at DEBUG.
    """
    # Re-raise disposal as-is: reraise_if_disposed would wrap in DocumentDisposedError.
    try:
        fn()
        return True
    except Exception as exc:
        if is_disposal(exc):
            raise
        return False


def _raise_disposed_during(name: str, exc: BaseException) -> NoReturn:
    raise DocumentDisposedError(
        f"UNO object disposed during {name}",
        object_type=name,
        details={"original_error": resolve_exception_message(exc)},
    ) from exc


def uno_call(fn: Callable[[], _T], what: str) -> _T:
    """Safely call a zero-argument UNO function, wrapping disposal or failures."""
    try:
        return fn()
    except WriterAgentException:
        # Preserve specific domain exceptions.
        raise
    except Exception as e:
        if is_disposal(e):
            _raise_disposed_during(what, e)
        text = resolve_exception_message(e)
        raise UnoObjectError(f"{what} failed: {text}", details={"operation": what, "type": type(e).__name__}) from e


