# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-send cancellation scopes and agent activity tracking.

Exposes SendCancellation, agent_session, and stop-checking primitives.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, Callable, ClassVar, cast

if TYPE_CHECKING:
    from collections.abc import Generator
    from plugin.framework.queue_executor import QueueExecutor, _ScopeUnset

log = logging.getLogger("writeragent.framework.queue_executor")

__all__ = [
    "SendCancellation",
    "SendCancelled",
    "_AGENT_ACTIVE_COUNT",
    "_AGENT_ACTIVE_LOCK",
    "_AGENT_OPEN_SCOPES",
    "_current_send_cancellation",
    "_fail_closed",
    "_resolve_bound_scope",
    "_restore_send_cancellation",
    "agent_session",
    "bind_send_stop_checker",
    "capture_send_stop",
    "get_current_send_cancellation",
    "is_agent_active",
    "send_stopped",
    "wait_for_approval",
]

_AGENT_ACTIVE_LOCK = threading.Lock()
_AGENT_ACTIVE_COUNT = 0
# Scopes whose agent_session has not exited yet (identity, under the lock).
_AGENT_OPEN_SCOPES: list["SendCancellation"] = []
_current_send_cancellation: ContextVar["SendCancellation | None"] = ContextVar("current_send_cancellation", default=None)


class SendCancelled(Exception):
    """Raised when main-thread work is skipped because the user stopped the send."""


def wait_for_approval(event: threading.Event, stop_checker: Callable[[], bool] | None, timeout: float = 0.2) -> bool:
    """Block until *event* is set, or the send is stopped.

    Approval used to call ``event.wait()`` with no stop check. Closing the
    sidebar latches stop and never signals the event, so the worker stayed
    parked after the drain ended. A set event wins a race with stop.
    """
    while not event.is_set():
        if stop_checker is not None and stop_checker():
            return False
        event.wait(timeout)
    return True


class SendCancellation:
    """Per-send cancellation: flag, registered HTTP clients, and optional hooks."""

    __slots__: ClassVar[tuple[str, ...]] = ("_cancelled", "_lock", "_hooks", "_executors")
    _cancelled: threading.Event
    _lock: threading.Lock

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._hooks: list[Callable[[], None]] = []
        self._executors: list[QueueExecutor] = []

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def bind_executor(self, executor: "QueueExecutor") -> None:
        """Track a :class:`QueueExecutor` whose pending work Stop must cancel."""
        with self._lock:
            if executor not in self._executors:
                self._executors.append(executor)

    def register_client(self, client: Any) -> None:
        # Resolve .stop() at registration time so cancel() only needs one list of
        # plain callables — no duck-type dispatch needed there.
        # B13: Stop can fire before the drain creates/registers the client. If the
        # scope is already cancelled, call stop() immediately so the worker cannot
        # open a socket under llm_request_lane.
        stop = getattr(client, "stop", None)
        if not callable(stop):
            return
        with self._lock:
            # The same bound stop is one hook; a second register_client must not
            # run it again, and a duplicate after cancel() does not call it again
            # because the first registration or cancel() already did.
            if stop in self._hooks:
                return
            self._hooks.append(cast("Callable[[], None]", stop))
            already = self._cancelled.is_set()
        if already:
            try:
                stop()
            except Exception:
                log.exception("SendCancellation: error stopping late-registered client")

    def register_on_cancel(self, hook: Callable[[], None]) -> None:
        with self._lock:
            # Equality, not identity, because each adapter.stop access is a new
            # bound method that compares equal.
            if hook in self._hooks:
                return
            self._hooks.append(hook)
            already = self._cancelled.is_set()
        # Stop can win the race: a hook registered after cancel() must still run.
        if already:
            try:
                hook()
            except Exception:
                log.exception("SendCancellation: error in late cancel hook")

    def cancel(self) -> None:
        # Set the flag under the lock so two callers cannot both run the hooks.
        with self._lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            hooks = list(self._hooks)
            executors = list(self._executors)
        for hook in hooks:
            try:
                hook()
            except Exception:
                log.exception("SendCancellation: error in cancel hook")
        # Unbound Stop only latches the flag. Falling back to default_executor
        # cancelled MCP, grammar, and peer items posted before the drain bound
        # this scope. The sidebar drain checks is_cancelled() and returns.
        if not executors:
            return
        for executor in executors:
            executor.cancel_pending_work(self)


def get_current_send_cancellation() -> SendCancellation | None:
    return _current_send_cancellation.get()


def _resolve_bound_scope(bound_scope: SendCancellation | None | _ScopeUnset) -> SendCancellation | None:
    """Current send when the caller did not pass a scope.

    ``is _SCOPE_UNSET`` is the only unset value. Checkers do not narrow ``is``
    against a class instance, so the else branch is cast.
    """
    if bound_scope is _SCOPE_UNSET:
        return get_current_send_cancellation()
    return cast("SendCancellation | None", bound_scope)


def _fail_closed(predicate: Callable[[], bool]) -> Callable[[], bool]:
    """Stop checker that treats a raising predicate as stopped.

    A checker that raises used to look like "not stopped" when the caller
    ignored the exception. Fail closed so a broken latch still aborts the send.
    """

    def _cancelled() -> bool:
        try:
            return predicate()
        except Exception:
            log.exception("stop_checker raised exception; failing closed (treating as stopped)")
            return True

    return _cancelled


def bind_send_stop_checker(scope: SendCancellation | None, fallback: Callable[[], bool] | None = None) -> Callable[[], bool]:
    """Return a stop predicate tied to *scope*, not the panel field.

    Worker threads must use this (or ``scope.is_cancelled``) so Stop stays latched after
    the main thread clears ``panel._send_cancellation`` when the drain loop exits.

    When both *scope* and *fallback* are set, either latch is enough: Stop can
    fire after SEND_CLICKED but before the deferred drain enters ``agent_session``.
    """
    if scope is not None and fallback is not None:
        return _fail_closed(lambda: scope.is_cancelled() or fallback())
    if scope is not None:
        return _fail_closed(scope.is_cancelled)
    if fallback is not None:
        return _fail_closed(fallback)
    return lambda: False


def capture_send_stop(host: Any) -> tuple[Any, Callable[[], bool]]:
    """Scope and stop checker frozen on the send thread at worker spawn.

    Call this before ``run_in_background``. The checker is whatever
    ``resolve_stop_checker`` returns now, which on the panel is
    ``bind_send_stop_checker`` closed over this scope object. The worker
    closes over both results and must not read the panel field again.
    Sub-agent and tool workers that call ``resolve_stop_checker()`` and
    read ``host._send_cancellation`` inside the thread body miss Stop:
    Stop's drain clears that field, and the next send stores a new scope
    there. A worker spawned but not yet in its body then binds the next
    send, so the old Stop is missed and the old worker can cancel the new
    one. ``run_in_background`` already copies contextvars at submit;
    ``get_current_send_cancellation`` stays that submit-time scope.
    """
    scope = getattr(host, "_send_cancellation", None)
    checker = host.resolve_stop_checker()
    return scope, checker


def send_stopped(bound_scope: Any, stop_checker: Callable[[], bool], client: Any) -> bool:
    """True when send cancellation was requested via scope, stop_checker callback, or client latch."""
    if bound_scope is not None and bound_scope.is_cancelled():
        return True
    if stop_checker():
        return True
    return bool(getattr(client, "_stopped", False))


@contextmanager
def agent_session(scope: SendCancellation | None = None) -> Generator[SendCancellation, None, None]:
    """Mark a chat/agent session as active and expose a :class:`SendCancellation` scope.

    Pass an existing *scope* when Stop must be able to cancel before the drain
    body starts (Send ``actionPerformed`` returns, then AsyncCallback runs drain).
    """
    global _AGENT_ACTIVE_COUNT
    if scope is None:
        scope = SendCancellation()
    # Bind here, not when StartSendEffect creates the scope: Stop before the
    # deferred drain must not cancel_pending_work that posted closer.
    scope.bind_executor(default_executor)
    token = _current_send_cancellation.set(scope)
    with _AGENT_ACTIVE_LOCK:
        _AGENT_ACTIVE_COUNT += 1
        _AGENT_OPEN_SCOPES.append(scope)
    abort = True
    try:
        yield scope
        abort = False
    finally:
        # Cancel on abort (exception / GeneratorExit), not on success. Stop and
        # disposing() call cancel() while still inside the with-body; do not
        # cancel again when _do_send returns normally after Stop.
        if abort and not scope.is_cancelled():
            scope.cancel()
        with _AGENT_ACTIVE_LOCK:
            _AGENT_ACTIVE_COUNT = max(0, _AGENT_ACTIVE_COUNT - 1)
            for i, open_scope in enumerate(_AGENT_OPEN_SCOPES):
                if open_scope is scope:
                    del _AGENT_OPEN_SCOPES[i]
                    break
            previous = token.old_value
            previous_open = any(open_scope is previous for open_scope in _AGENT_OPEN_SCOPES)
        _restore_send_cancellation(scope, token, previous_open)


def _restore_send_cancellation(scope: SendCancellation, token: Any, previous_open: bool) -> None:
    """Undo agent_session's ``set`` without resurrecting a finished scope.

    The event-driven drain keeps a session open across VCL callbacks, so
    two documents' sessions can exit in either order on the main thread.
    ``ContextVar.reset`` restores the value from when *this* session
    started. Exiting A first would reset B's live scope to None; exiting B
    would then restore A's finished (maybe cancelled) scope, and later
    main-thread work outside any session would be bound to it (posts
    dropped by its Stop, LLM lane and indexer waits seeing "cancelled").
    Leave the variable alone if another session is now current; when this
    scope is current, restore the earlier value only while that session is
    still open, otherwise None. Strict nesting behaves as before.
    """
    if _current_send_cancellation.get() is not scope:
        return
    try:
        if previous_open or token.old_value is None or token.old_value is Token.MISSING:
            _current_send_cancellation.reset(token)
        else:
            _current_send_cancellation.set(None)
    except ValueError:
        # Token from another Context (exit on a different thread/context).
        _current_send_cancellation.set(None)


def is_agent_active() -> bool:
    with _AGENT_ACTIVE_LOCK:
        return _AGENT_ACTIVE_COUNT > 0


from plugin.framework.queue_executor import _SCOPE_UNSET, default_executor
