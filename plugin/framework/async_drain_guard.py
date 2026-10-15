# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Main-Thread Async Event Loop Sentry.

Enforces single-ownership of main-thread UI event pumping to prevent
harmful nested event-loop reentrancy (e.g. recursive processEventsToIdle calls).

Concurrency: LibreOffice’s UI toolkit (VCL) misbehaves if two Python
loops call ``processEventsToIdle`` at once (nested dialogs, recursive
pumps, frozen window). ``_drain_lock`` records **which drain loop
currently owns that pump**, not a lock for all UI code. A second owner
trying to nest raises; the same owner re-entering is counted so it can
unwind. Do not hold this lock while talking to the document or the
network — acquire, note ownership, release, then pump.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from collections.abc import Generator

log = logging.getLogger("writeragent.framework.async_drain_guard")

_drain_lock = threading.Lock()
_active_owner_name: str | None = None
_owner_thread_ident: int | None = None
_drain_depth: int = 0
_suppressed_vcl_count: int = 0
_idle_notify = threading.local()
_IDLE_NOTIFY_MAX_PASSES = 8
# Peer messaging (and similar): start queued work only after the pump is free.
# Callbacks must not start a drain on this stack when ``post`` would run inline.
_drain_idle_callbacks: list[Callable[[], None]] = []


class NestedDrainOwnerError(RuntimeError):
    """Raised when a second drain owner attempts to start while another is active."""


def acquire_drain_owner(owner_name: str) -> None:
    """Mark *owner_name* as the pump owner.

    A different owner raises :class:`NestedDrainOwnerError`. The same name
    re-enters and increments the depth counter only on the owner thread;
    another thread raises :class:`NestedDrainOwnerError`. Pair with
    :func:`release_drain_owner`. The event-driven stream drain holds this
    across VCL callbacks, so it cannot use the context manager (that would
    drop the owner when the callback returns).
    """
    global _active_owner_name, _drain_depth, _owner_thread_ident
    caller = threading.get_ident()
    with _drain_lock:
        if _active_owner_name is not None and _active_owner_name != owner_name:
            msg = f"[SENTRY VIOLATION] Nested UI drain attempted by {owner_name!r} while {_active_owner_name!r} owns event loop"
            log.warning(msg)
            raise NestedDrainOwnerError(msg)
        if _active_owner_name is not None and _owner_thread_ident != caller:
            msg = f"[SENTRY VIOLATION] Drain owner {owner_name!r} re-entered from thread {caller} while thread {_owner_thread_ident} holds it"
            log.warning(msg)
            raise NestedDrainOwnerError(msg)
        if _active_owner_name is None:
            _active_owner_name = owner_name
            _owner_thread_ident = caller
        _drain_depth += 1


def release_drain_owner() -> None:
    """Undo one :func:`acquire_drain_owner`. Idle callbacks run at depth 0.

    While depth is still above zero the current owner name stays. Event-driven
    drains in two documents overlap and can finish in either order. Clearing
    the name when the first drain finishes would let a different owner (MCP)
    start under the drain that is still pumping. Acquire refuses a different
    name while one is set, so every holder at depth > 0 already has that name.
    A foreign release logs and returns; it does not raise and does not run
    idle callbacks. An underflow logs a warning and does not run idle
    callbacks.
    """
    # Bugfix: name-only depth let any thread share or drop the pump; the
    # ident check closes that.
    # Bugfix: An extra release used to decrement through zero, clamp, and
    # still run idle callbacks, so a mismatched release looked like a real
    # transition to idle and hid the missing acquire. Underflow now logs
    # and returns without running idle callbacks.
    global _active_owner_name, _drain_depth, _owner_thread_ident
    caller = threading.get_ident()
    became_idle = False
    with _drain_lock:
        if _drain_depth > 0 and _owner_thread_ident != caller:
            log.warning(
                "release_drain_owner ignored: caller thread %s is not owner thread %s (owner=%r depth=%s)",
                caller,
                _owner_thread_ident,
                _active_owner_name,
                _drain_depth,
            )
            return
        if _drain_depth <= 0:
            log.warning("release_drain_owner underflow (owner=%r depth=%s)", _active_owner_name, _drain_depth)
            _drain_depth = 0
            _active_owner_name = None
            _owner_thread_ident = None
            return
        _drain_depth -= 1
        if _drain_depth == 0:
            _active_owner_name = None
            _owner_thread_ident = None
            became_idle = True
    if became_idle:
        _notify_drain_idle()


@contextmanager
def drain_owner_scope(owner_name: str) -> Generator[None, None, None]:
    """Sentry context manager for main-thread UI event pumping.

    A different owner name raises :class:`NestedDrainOwnerError`. The same name
    re-enters and increments the depth counter (``pump_ui_idle`` skips VCL when
    depth > 1). ``run_stream_drain_loop`` takes this scope, or
    ``acquire_drain_owner("stream")``, directly: that same name is allowed and
    a different name raises. ``run_async_worker_with_drain`` still refuses any
    current owner, including ``"stream"``, before it spawns a worker. The owner
    may call :func:`pump_ui_idle`; other code must use
    :func:`process_events_to_idle`, which no-ops VCL while owned.
    """
    acquire_drain_owner(owner_name)
    try:
        yield
    finally:
        release_drain_owner()


def get_drain_owner() -> str | None:
    """Return the active drain owner name, or None if idle."""
    with _drain_lock:
        return _active_owner_name


def get_drain_depth() -> int:
    """Return current drain recursion depth."""
    with _drain_lock:
        return _drain_depth


def owner_may_pump_vcl() -> bool:
    """Return True at depth 0 and depth 1; False above 1 (same owner re-entered).

    Only pump_ui_idle may call it. Every other caller keeps using
    process_events_to_idle, which skips VCL whenever get_drain_owner() is not
    None, including at depth 1. Depth 1 is not permission for a second pump.
    """
    with _drain_lock:
        return _drain_depth <= 1


def note_suppressed_vcl_pump(owner: str | None = None) -> None:
    """Increment suppressed VCL pump diagnostic counter and log debug note."""
    global _suppressed_vcl_count
    with _drain_lock:
        _suppressed_vcl_count += 1
    if owner is not None:
        log.debug("process_events_to_idle suppressed (drain owner=%s)", owner)


def get_suppressed_vcl_pump_count() -> int:
    """Diagnostic helper: return total suppressed secondary VCL pumps."""
    with _drain_lock:
        return _suppressed_vcl_count


def reset_suppressed_vcl_pump_count() -> None:
    """Test hook: reset the suppressed VCL pump counter."""
    global _suppressed_vcl_count
    with _drain_lock:
        _suppressed_vcl_count = 0


def add_drain_idle_callback(fn: Callable[[], None]) -> None:
    """Register *fn* to run when drain depth hits 0 (outside the sentry lock)."""
    # Membership and append raced ``_notify_drain_idle``'s snapshot: both
    # touched the list with no lock, so a register could miss that idle pass
    # or pass ``in`` twice and append a duplicate. Same lock as the copy below.
    with _drain_lock:
        if fn not in _drain_idle_callbacks:
            _drain_idle_callbacks.append(fn)


def remove_drain_idle_callback(fn: Callable[[], None]) -> None:
    """Remove *fn* from the idle callbacks if it is registered."""
    with _drain_lock:
        try:
            _drain_idle_callbacks.remove(fn)
        except ValueError:
            pass


def _notify_drain_idle() -> None:
    """Invoke idle callbacks. Never raise into the drain ``finally``."""
    # Bugfix: re-raising RecursionError escaped release_drain_owner, and
    # _EventDrain._finish calls that before epilogues, so _close_peer_drain
    # never ran and the peer agent_session stayed open.
    if getattr(_idle_notify, "active", False):
        _idle_notify.again = True
        return

    _idle_notify.active = True
    _idle_notify.again = False
    try:
        for _ in range(_IDLE_NOTIFY_MAX_PASSES):
            _idle_notify.again = False
            # Copy under the lock, then drop it before calling. Peer ``_on_drain_idle``
            # calls ``get_drain_owner()``, which takes this same non-reentrant lock.
            # Do not clear the list. peer_message.py registers one process-wide
            # callback (add_drain_idle_callback(_on_drain_idle)) that must stay across
            # drain cycles so a peer turn queued during a drain still gets kicked.
            with _drain_lock:
                if _drain_depth != 0:
                    return
                callbacks = list(_drain_idle_callbacks)
            for cb in callbacks:
                try:
                    cb()
                except Exception:
                    log.exception("drain idle callback failed")
            if not _idle_notify.again:
                return
        log.warning("drain idle notification stopped after %d passes", _IDLE_NOTIFY_MAX_PASSES)
    finally:
        _idle_notify.active = False
        _idle_notify.again = False


def reset_sentry_state() -> None:
    """Test hook: reset owner name, owner thread ident, depth, and suppressed-VCL counter.

    Deliberately leaves ``_drain_idle_callbacks`` in place because import-time
    subscribers (such as peer messaging) must stay registered across tests.
    """
    global _active_owner_name, _drain_depth, _owner_thread_ident, _suppressed_vcl_count
    with _drain_lock:
        _active_owner_name = None
        _owner_thread_ident = None
        _drain_depth = 0
        _suppressed_vcl_count = 0

