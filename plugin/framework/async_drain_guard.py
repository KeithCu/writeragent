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
_drain_depth: int = 0
_suppressed_vcl_count: int = 0
# Peer messaging (and similar): start queued work only after the pump is free.
# Callbacks must not start a drain on this stack when ``post`` would run inline.
_drain_idle_callbacks: list[Callable[[], None]] = []


class NestedDrainOwnerError(RuntimeError):
    """Raised when a second drain owner attempts to start while another is active."""


def acquire_drain_owner(owner_name: str) -> str | None:
    """Mark *owner_name* as the pump owner. Return the previous owner.

    A different owner raises :class:`NestedDrainOwnerError`. The same name
    re-enters and increments the depth counter. Pair with
    :func:`release_drain_owner`. The event-driven stream drain holds this
    across VCL callbacks, so it cannot use the context manager (that would
    drop the owner when the callback returns).
    """
    global _active_owner_name, _drain_depth
    with _drain_lock:
        if _active_owner_name is not None and _active_owner_name != owner_name:
            msg = f"[SENTRY VIOLATION] Nested UI drain attempted by {owner_name!r} while {_active_owner_name!r} owns event loop"
            log.warning(msg)
            raise NestedDrainOwnerError(msg)
        previous_owner = _active_owner_name
        _active_owner_name = owner_name
        _drain_depth += 1
        return previous_owner


def release_drain_owner(previous_owner: str | None) -> None:
    """Undo one :func:`acquire_drain_owner`. Idle callbacks run at depth 0.

    While depth is still above zero the current owner name stays. Event-driven
    drains in two documents overlap and can finish in either order. Clearing
    the name when the first drain finishes would let a different owner (MCP)
    start under the drain that is still pumping. Acquire refuses a different
    name while one is set, so every holder at depth > 0 already has that name.
    """
    global _active_owner_name, _drain_depth
    became_idle = False
    with _drain_lock:
        _drain_depth -= 1
        if _drain_depth <= 0:
            _drain_depth = 0
            _active_owner_name = None
            became_idle = True
        elif _active_owner_name is None:
            # Defensive: never leave a held pump without a name.
            _active_owner_name = previous_owner
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
    previous_owner = acquire_drain_owner(owner_name)
    try:
        yield
    finally:
        release_drain_owner(previous_owner)


def get_drain_owner() -> str | None:
    """Return the active drain owner name, or None if idle."""
    with _drain_lock:
        return _active_owner_name


# Alias for backward compatibility
get_active_drain_owner = get_drain_owner


def get_drain_depth() -> int:
    """Return current drain recursion depth."""
    with _drain_lock:
        return _drain_depth


def is_vcl_pump_allowed() -> bool:
    """Return True if native VCL pumping is safe (depth <= 1)."""
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


# Alias for backward compatibility
get_suppressed_vcl_count = get_suppressed_vcl_pump_count


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
    # Copy under the lock, then drop it before calling. Peer ``_on_drain_idle``
    # calls ``get_drain_owner()``, which takes this same non-reentrant lock.
    # Do not clear the list. peer_message.py registers one process-wide
    # callback (add_drain_idle_callback(_on_drain_idle)) that must stay across
    # drain cycles so a peer turn queued during a drain still gets kicked.
    with _drain_lock:
        callbacks = list(_drain_idle_callbacks)
    for cb in callbacks:
        try:
            cb()
        except RecursionError:
            raise
        except Exception:
            log.exception("drain idle callback failed")


def reset_sentry_state() -> None:
    """Test hook: reset sentry state and counters completely."""
    global _active_owner_name, _drain_depth, _suppressed_vcl_count
    with _drain_lock:
        _active_owner_name = None
        _drain_depth = 0
        _suppressed_vcl_count = 0
