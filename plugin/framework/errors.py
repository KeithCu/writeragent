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
import errno
import logging
from typing import Any, Callable, TypeVar, overload

from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_json_loads, safe_python_literal_eval

from plugin.framework.deal_shim import DEAL_MAX_MSGID, UNDER_CROSSHAIR, deal

try:
    from com.sun.star.lang import DisposedException

    # isinstance must be DisposedException only. com.sun.star.uno.Exception is the
    # parent of almost every UNO error, and IllegalArgumentException subclasses
    # RuntimeException, so either of those bases reports a bad argument as disposal.
    # RuntimeException still matches by type name below (bridge teardown, mocks).
    # Drop the type when a test mock aliases it to builtins.Exception.
    if isinstance(DisposedException, type) and issubclass(DisposedException, BaseException) and DisposedException is not Exception:
        UNO_DISPOSED_EXCEPTIONS: tuple[type[BaseException], ...] = (DisposedException,)
    else:
        UNO_DISPOSED_EXCEPTIONS = ()
except (ImportError, AttributeError):
    UNO_DISPOSED_EXCEPTIONS = ()

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


def is_disposed_exception(exc: BaseException) -> bool:
    """Return True if exc represents a UNO object disposal or runtime teardown exception.

    Matching ``RuntimeException`` in the type name is a deliberate heuristic:
    ``com.sun.star.uno.RuntimeException`` (and name-alikes in mocks) is how
    bridge teardown often surfaces. Do not narrow this so UI lifecycle can
    still use :class:`suppress_disposed` without crashing the host. The check
    is the type name, not ``isinstance(..., RuntimeException)``:
    ``IllegalArgumentException`` subclasses ``RuntimeException`` and must stay
    a normal error. Genuine failures belong outside those blocks, not in a
    tighter name check here.
    Tool chat mapping uses :func:`is_tool_document_disposed` so a live-doc
    bare RuntimeException is not reported as DOCUMENT_DISPOSED.
    `reraise_if_disposed` uses `is_real_disposal` and must not call this function.
    """
    if isinstance(exc, DocumentDisposedError):
        return True
    if isinstance(exc, UNO_DISPOSED_EXCEPTIONS):
        return True
    name = type(exc).__name__
    return "DisposedException" in name or "RuntimeException" in name


_T = TypeVar("_T")


def uno_probe(fn: Callable[[], _T], default: _T) -> _T:
    """Return ``fn()``, or *default* when it raises a non-disposal ``Exception``.

    Disposal re-raises the original exception, judged by the broad
    :func:`is_disposed_exception` (RuntimeException name match included). That
    is the inline form of ``except Exception: if is_disposed_exception(e): raise;
    return default``. :func:`safe_uno_call` differs: it re-raises only real
    disposal, wrapped as ``DocumentDisposedError``, and logs at DEBUG.
    """
    try:
        return fn()
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        return default


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
    log_unexpected: bool
    suppress_all: bool
    exc_info: bool

    def __init__(self, action: str = "action", *, logger: logging.Logger | None = None, log_unexpected: bool = True, suppress_all: bool = True, exc_info: bool = False) -> None:
        self.action = action
        self.logger = logger
        self.log_unexpected = log_unexpected
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

        if is_disposed_exception(exc_val):
            log_obj.debug("%s skipped (likely disposed): %s", self.action, exc_val, exc_info=self.exc_info)
            return True

        if self.log_unexpected:
            log_obj.exception("Unexpected error during %s: %s", self.action, exc_val)

        return self.suppress_all


def resolve_exception_message(e: Any) -> str:
    """Extract non-empty message string from an exception, resolving UNO Exception Message attributes and causes."""
    msg = ""
    try:
        raw = getattr(e, "Message", None)
        if isinstance(raw, str) and raw.strip():
            msg = raw.strip()
    except Exception as exc:
        logging.getLogger("writeragent.errors").debug("resolve_exception_message: Message unreadable (%s)", type(exc).__name__)
    if not msg:
        try:
            raw = str(e)
            if isinstance(raw, str) and raw.strip():
                msg = raw.strip()
        except Exception as exc:
            logging.getLogger("writeragent.errors").debug("resolve_exception_message: str(exception) failed (%s)", type(exc).__name__)
    if not msg:
        cause = None
        try:
            cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        except Exception as exc:
            logging.getLogger("writeragent.errors").debug("resolve_exception_message: cause lookup failed (%s)", type(exc).__name__)
        if cause is not None and cause is not e:
            try:
                raw = getattr(cause, "Message", None)
                if isinstance(raw, str) and raw.strip():
                    msg = raw.strip()
            except Exception as exc:
                logging.getLogger("writeragent.errors").debug("resolve_exception_message: cause Message unreadable (%s)", type(exc).__name__)
            if not msg:
                try:
                    raw = str(cause)
                    if isinstance(raw, str) and raw.strip():
                        msg = raw.strip()
                except Exception as exc:
                    logging.getLogger("writeragent.errors").debug("resolve_exception_message: str(cause) failed (%s)", type(exc).__name__)
    if not msg:
        if isinstance(e, BaseException):
            msg = type(e).__name__.strip() or "BaseException"
        else:
            msg = "Unknown error"
    return msg


def _translate_exception_message(message: Any) -> str:
    """Translate a catalog msgid. Long runtime text skips ``_()``.

    ``_()`` rejects strings longer than ``DEAL_MAX_MSGID`` with
    ``deal.PreContractError``. A provider body or UNO text would then raise
    that contract error instead of the exception the caller asked for.
    Gettext only matches an extracted source string, so a message past the
    msgid bound is not in the catalog anyway. Return it unchanged.
    """
    text = resolve_exception_message(message)
    if len(text) > DEAL_MAX_MSGID:
        return text
    return _(text)


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
        # unless the exact source string was extracted. Long messages skip _().
        text = "mock" if UNDER_CROSSHAIR else _translate_exception_message(message)
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


class CalcError(WriterAgentException):
    """Calc spreadsheet manipulation and calculation failures."""

    code: str = "CALC_ERROR"


class FormulaError(CalcError):
    """Calc formula parsing, rebuilding, or evaluation failures."""

    code: str = "FORMULA_ERROR"


class SpillCollisionError(FormulaError):
    """Dynamic array formula cannot spill because destination cells are not empty."""

    code: str = "SPILL_COLLISION"


class SandboxSecurityError(ScriptingError):
    """Script attempted an operation or import forbidden by the sandbox policy."""

    code: str = "SANDBOX_SECURITY_ERROR"


@deal.post(lambda result: isinstance(result, dict) and result.get("status") == "error" and "code" in result and "message" in result)
@deal.ensure(lambda e, result: isinstance(e, WriterAgentException) or (result.get("code") == "INTERNAL_ERROR" and isinstance(result.get("details"), dict) and "type" in result["details"]))
@deal.ensure(lambda e, result: not isinstance(e, WriterAgentException) or result.get("code") == e.code)
def format_error_payload(e: BaseException) -> dict[str, Any]:
    """Format an exception into the standard JSON error payload schema."""
    if isinstance(e, WriterAgentException):
        payload: dict[str, Any] = {"status": "error", "code": e.code, "message": e.message}
        if e.details:
            payload["details"] = e.details
        return payload

    # For unexpected exceptions
    if UNDER_CROSSHAIR:
        err_type = "ValueError"
        err_msg = "mock"
    else:
        err_type = type(e).__name__
        err_msg = resolve_exception_message(e)
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
# remains in client/errors.py. Generic display formatting (format_error_for_display)
# and the i18n mapper (format_error_message) live in this module.


def _is_connection_refused(exc: BaseException, reason: str) -> bool:
    """True for a refused TCP connect, not for the digits 111 in an unrelated message."""
    import errno
    import urllib.error

    if isinstance(exc, ConnectionRefusedError):
        return True
    nested = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else None
    if isinstance(nested, ConnectionRefusedError):
        return True
    if getattr(exc, "errno", None) == errno.ECONNREFUSED:
        return True
    if getattr(nested, "errno", None) == errno.ECONNREFUSED:
        return True
    return "connection refused" in reason.lower()


def _is_socket_timeout_phrase(text: str) -> bool:
    """True for standard socket/HTTP timeout phrases rather than host timeouts."""
    stripped = text.strip().lower()
    return stripped == "timed out" or "the read operation timed out" in stripped


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

    msg = "mock" if UNDER_CROSSHAIR else resolve_exception_message(e)
    if isinstance(e, ssl.SSLError):
        return _("TLS/SSL Error: {0}").format(msg)
    # Only HTTPError carries a status. RemoteDisconnected, BadStatusLine,
    # and IncompleteRead have no .code/.status/.reason; treating every
    # HTTPException like HTTPError discarded str(e) and reported "HTTP Error 0".
    # Those fall through to the connection/OSError path or the final str(e).
    if isinstance(e, urllib.error.HTTPError):
        code_candidate = getattr(e, "code", None)
        if code_candidate is None:
            code_candidate = getattr(e, "status", None)
        try:
            code = int(code_candidate) if code_candidate is not None else 0
        except (TypeError, ValueError):
            code = 0
        mapped = http_status_message(code)
        if mapped is not None:
            return mapped
        if code >= 500:
            return _("Server error ({0}). The AI provider is having issues.").format(code)
        reason = "mock" if UNDER_CROSSHAIR else str(getattr(e, "reason", "") or "")
        return _("HTTP Error {0}: {1}").format(code, reason)

    # Typed exceptions first. Substring checks below are last-resort for
    # stdlib/HTTP-library strings we do not own a subclass for.
    if isinstance(e, ConfigError) and getattr(e, "code", "") == "missing_api_key":
        provider = ""
        if isinstance(e.details, dict):
            provider = str(e.details.get("provider") or "").strip()
        if provider and provider != "custom":
            return _("No API key configured for {0}. Open Settings and add a key.").format(provider)
        return _("No API key configured. Open Settings and add a key.")
    if isinstance(e, VenvNotFoundError):
        return _("Python venv not found. Open Settings → Python, set the venv path, then Test.")
    if isinstance(e, VenvTimeoutError):
        return _("Python execution timed out. Open Settings → Python to raise the timeout.")
    if isinstance(e, SpillCollisionError):
        return _("Formula spill collision: destination range contains non-empty cells.")
    if isinstance(e, SandboxSecurityError):
        return _("Script execution blocked by sandbox policy: {0}").format(getattr(e, "message", str(e)))

    # Filesystem errors are not a down local server. Match connection-shaped
    # transport errors, not filesystem classes or errnos.
    reason_obj: BaseException | None
    if isinstance(e, urllib.error.URLError):
        raw_reason = getattr(e, "reason", None)
        reason_obj = raw_reason if isinstance(raw_reason, BaseException) else None
    else:
        reason_obj = e if isinstance(e, BaseException) else None

    if UNDER_CROSSHAIR:
        reason = "mock"
    elif reason_obj is not None:
        reason = resolve_exception_message(reason_obj)
    elif isinstance(e, urllib.error.URLError):
        reason = str(getattr(e, "reason", None) or e)
    else:
        reason = str(e)

    is_fs = isinstance(e, _FILESYSTEM_CLASSES) or (getattr(e, "errno", None) in _FILESYSTEM_ERRNOS)
    if isinstance(e, (urllib.error.URLError, OSError)) and not is_fs:
        is_transport = (
            isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, socket.gaierror, socket.herror))
            or (getattr(e, "errno", None) in _TRANSPORT_ERRNOS)
            or ("timed out" in reason.lower() and "formula" not in reason.lower())
            or _is_connection_refused(e, reason)
            or ("getaddrinfo failed" in reason.lower())
        )
        if is_transport:
            # URLError is not itself a socket.timeout; the timeout is e.reason.
            # Check that before the connection-error sentence, or a wrapped
            # timeout tells the user the server is down.
            if reason_obj is not None and isinstance(reason_obj, (socket.timeout, TimeoutError)):
                msg_obj = "mock" if UNDER_CROSSHAIR else resolve_exception_message(reason_obj)
                if _is_socket_timeout_phrase(msg_obj):
                    return _("Request Timed Out. Try increasing 'Request Timeout' in Settings.")
                return msg_obj
            # urllib.request wraps a certificate failure as URLError(reason=SSLError).
            # A direct SSLError is handled above. The wrapped form used to return "Connection Error"
            # and hide the TLS sentence.
            if isinstance(reason_obj, ssl.SSLError):
                tls = "mock" if UNDER_CROSSHAIR else resolve_exception_message(reason_obj)
                return _("TLS/SSL Error: {0}").format(tls)
            if "timed out" in reason.lower() and "formula" not in reason.lower():
                return _("Request Timed Out. Try increasing 'Request Timeout' in Settings.")
            # Errno text and unrelated messages both contain "111" (port 1111).
            # Match the errno or the words, not that substring.
            if _is_connection_refused(e, reason):
                return _("Connection Refused. Is your local AI server (Ollama/LM Studio) running?")
            if "getaddrinfo failed" in reason.lower():
                return _("DNS Error. Could not resolve the endpoint URL.")
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


@deal.post(lambda result: isinstance(result, dict) and result.get("status") == "error" and "code" in result and "message" in result)
def make_tool_error(message: Any, code: str = "TOOL_EXECUTION_ERROR", **details: Any) -> dict[str, Any]:
    """Central factory for all standardized tool error payloads.

    A long UNO or provider string is a runtime message. Construction goes
    through ``WriterAgentException``, which does not apply the gettext msgid
    length contract to that text.
    """
    if not isinstance(message, str):
        message = resolve_exception_message(message)
    if not isinstance(code, str) or not code:
        code = "TOOL_EXECUTION_ERROR"
    return format_error_payload(ToolExecutionError(message, code=code, details=details))


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


class ToolPermissionError(WriterAgentException):
    """User rejected tool execution or permission denied."""

    code: str = "PERMISSION_DENIED"


class AgentParsingError(WriterAgentException):
    """LLM output / JSON parsing failures."""

    code: str = "PARSE_ERROR"


def check_disposed(model: Any, context_name: str = "Object") -> None:
    """Raise UnoObjectError if *model* is None.

    Null guard only. Live disposal is ``DisposedException``,
    :func:`is_document_disposed`, or :func:`safe_uno_call`. Probing UNO here
    would add document-model calls to LibrePy-light helpers that only need None.
    """
    if model is None:
        raise UnoObjectError(f"{context_name} is null", code="UNO_NULL_OBJECT")


def is_real_disposal(exc: BaseException) -> bool:
    """True only for DisposedException / DocumentDisposedError.

    A bare UNO RuntimeException on a live document is a real error. Mapping it
    to DocumentDisposedError made is_tool_document_disposed skip the live-doc
    check and report "Document was closed".
    """
    return isinstance(exc, DocumentDisposedError) or "DisposedException" in type(exc).__name__


def is_document_disposed(doc: Any) -> bool:
    """Safely check if a UNO document or component is disposed or invalid."""
    if doc is None:
        return True
    try:
        impl_fn = getattr(doc, "getImplementationName", None)
        if not callable(impl_fn):
            return False
        _unused = impl_fn()
        return False
    except Exception as exc:
        return is_real_disposal(exc)


def is_tool_document_disposed(exc: BaseException, doc: Any = None) -> bool:
    """True when ``execute_safe`` should report DOCUMENT_DISPOSED.

    ``is_disposed_exception`` is the UI-lifecycle heuristic and matches any
    ``RuntimeException``. A bare RuntimeException from a still-live document
    is a real UNO error — e.g. ``createTextCursorByRange(ViewCursor)`` on a
    Writer body — not a closed document. Do not map that to the lying
    "Document was closed or disposed by LibreOffice" chat string.
    """
    if not is_disposed_exception(exc):
        return False
    if is_real_disposal(exc):
        return True
    if doc is not None and not is_document_disposed(doc):
        return False
    return True


def _uno_disposal_message(exc: BaseException) -> str:  # pyright: ignore[reportUnusedFunction]  # imported by uno_context.py and scratch_writer.py
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
    """Disposed UNO is a closed document or component, not a generic tool failure.

    Only DisposedException and DocumentDisposedError become DocumentDisposedError.
    An existing DocumentDisposedError is re-raised unchanged.
    A bare RuntimeException must remain the original exception because wrapping
    it makes is_tool_document_disposed skip the live-document probe.
    """
    if isinstance(exc, DocumentDisposedError):
        raise exc
    if is_real_disposal(exc):
        raise DocumentDisposedError(message, object_type=object_type) from exc


def uno_read(fn: Callable[[], _T], default: _T, *, object_type: str = "document") -> _T:
    """Return ``fn()``, or *default* when it raises an ordinary ``Exception``.

    The call form of ``except Exception as e: reraise_if_disposed(e); return
    default``: only real disposal (:func:`is_real_disposal`) leaves, wrapped
    as ``DocumentDisposedError`` exactly as :func:`reraise_if_disposed` does.
    A bare UNO ``RuntimeException`` returns *default*, as in
    :func:`safe_uno_call`. *object_type* is passed to ``reraise_if_disposed``.
    :func:`uno_probe` is the broad-predicate variant.
    """
    try:
        return fn()
    except Exception as exc:
        reraise_if_disposed(exc, object_type=object_type)
        return default



# safe_uno_call is for probes (any failure returns default, except real disposal).
# safe_call wraps real operations; treats only DisposedException as disposal.
# suppress_disposed still uses the broad is_disposed_exception heuristic.
def safe_uno_call(default: Any = None) -> Any:
    """Decorator to safely call UNO methods with automatic error handling, returning default on failure (disposal exceptions re-raised).

    A UNO ``RuntimeException`` is not disposal: probes return ``default``, and
    :func:`safe_call` wraps it as ``UnoObjectError``. Re-raise only
    ``DisposedException`` / ``DocumentDisposedError``.
    See ``docs/framework/uno-thread-safety.md`` and
    ``test_safe_uno_call_returns_default_on_runtime_error``.
    """

    def decorator(func: Any) -> Any:
        from functools import wraps

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Do not add "RuntimeException": that is a probe failure, not disposal.
                if is_real_disposal(e):
                    # Bugfix: details used to store str(args) and str(kwargs); a probe
                    # argument can be document text or a UNO proxy, and stringifying
                    # it during teardown is large and can touch the dead object;
                    # the message already names func.__name__.
                    raise DocumentDisposedError(f"UNO object disposed during {func.__name__}", object_type=func.__name__, details={"original_error": resolve_exception_message(e)}) from e
                logging.getLogger("writeragent.errors").debug("safe_uno_call: %s failed (%s), returning default %r", func.__name__, e, default)
                return default

        return wrapper

    return decorator


def safe_call(fn: Any, context_name: str, *args: Any, **kwargs: Any) -> Any:
    """Safely call a UNO method. If it raises any exception (e.g., DisposedException), wrap it in UnoObjectError or DocumentDisposedError."""
    try:
        return fn(*args, **kwargs)
    except WriterAgentException:
        # Bugfix: safe_call previously caught all Exception instances and wrapped them
        # in UnoObjectError, converting WriterAgentException subclasses (like
        # ToolPermissionError) into generic UNO errors and losing their error codes.
        # Catching WriterAgentException first preserves specific domain exceptions.
        raise
    except Exception as e:
        # Only DisposedException is disposal. Treating RuntimeException as
        # disposal makes a live document look closed. Other UNO failures
        # stay UnoObjectError.
        e_name = type(e).__name__
        if is_real_disposal(e):
            raise DocumentDisposedError(f"UNO object disposed during {context_name}", object_type=context_name, details={"original_error": resolve_exception_message(e)}) from e

        # Catch Exception; current PyUNO errors subclass Exception;
        # KeyboardInterrupt and SystemExit propagate. We immediately wrap it.
        text = resolve_exception_message(e)
        raise UnoObjectError(f"{context_name} failed: {text}", details={"operation": context_name, "type": e_name}) from e


# Exception types and error helpers live here. safe_json_loads / safe_python_literal_eval
# are implemented in json_utils and re-exported as stable public imports (intentional).
__all__ = [
    "AgentParsingError",
    "BlockingWaitStopped",
    "CalcError",
    "ConfigError",
    "ConfigValidationError",
    "DocumentDisposedError",
    "FormulaError",
    "NetworkError",
    "SandboxSecurityError",
    "ScriptingError",
    "SpillCollisionError",
    "ToolExecutionError",
    "ToolPermissionError",
    "UnoObjectError",
    "VenvError",
    "VenvNotFoundError",
    "VenvTimeoutError",
    "WorkerPoolError",
    "WriterAgentException",
    "best_effort",
    "check_disposed",
    "format_error_for_display",
    "format_error_message",  # The single i18n-friendly mapper (centralized here in 2026 janitor effort)
    "format_error_payload",
    "http_status_message",
    "is_disposed_exception",
    "is_document_disposed",
    "is_real_disposal",
    "is_tool_document_disposed",
    "make_tool_error",  # Central factory for all tool error dicts
    "reraise_if_disposed",
    "resolve_exception_message",
    "safe_call",
    "safe_json_loads",
    "safe_python_literal_eval",
    "safe_uno_call",
    "suppress_disposed",
    "uno_probe",
    "uno_read",
]
