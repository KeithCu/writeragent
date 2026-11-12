# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""VCL event loop pumping and idle waiting helpers.

Split out from ``uno_context`` to separate VCL loop pumping and focus preservation
from core context and desktop lookup.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from plugin.framework.queue_executor import Policy, main
from plugin.framework.thread_guard import main_thread_only

log = logging.getLogger("writeragent.vcl_pumping")

# Coalesce to one in-flight secondary-idle post without scanning the process queue.
# Unscoped posts are not cancelled by Stop. The flag clears only when _pump finishes
# or main.post raises. If a test double removes or drains the queued callable
# without running it, the flag stays set on purpose; do not scan the queue to clear it.
_secondary_idle_lock = threading.Lock()
_secondary_idle_in_flight = False

# Track active calls to processEventsToIdle on the stack. A pump started by
# WriterAgent runs until the VCL scheduler is idle, so a second pump started from
# a callback inside it cannot add anything and only deepens the stack while holding
# SolarMutex. Calls with force=False skip pumping when depth is positive.
# The lock is defensive; only the VCL thread pumps.
_vcl_pump_lock = threading.Lock()
_vcl_pump_depth: int = 0


@contextmanager
def focus_preserved(ctx: Any, restore: Any = None, *, restore_focus: Callable[[], None] | None = None) -> Generator[None, None, None]:
    """Restore focus after a block that may steal it (RichTextControl reveal).

    *restore* is the query field of the panel that is running this block.
    There is no process-wide pin: a second window's stream must not call
    ``setFocus`` here. When *restore* is omitted, the toolkit focus window
    at entry is restored — which is the Send button after a click, so
    callers that own an Ask field pass it.

    *restore_focus* is the panel's ``FrameSession.restore_focus`` callback.
    When given, it runs on exit instead of ``restore.setFocus()``. Why: a
    raw ``setFocus`` here ignored the frame session (closed, user left the
    Ask field, frame not the active window) and pulled focus back into a
    background document window (release QA BUG A, fix 3).
    """
    from plugin.framework.uno_context import get_toolkit

    saved = None
    if not callable(restore_focus):
        saved = restore
        if saved is None:
            try:
                tk = get_toolkit(ctx)
                if tk is not None and hasattr(tk, "getFocusWindow"):
                    saved = tk.getFocusWindow()
            except Exception as e:
                log.debug("focus_preserved capture: %s", e)
    try:
        yield
    finally:
        try:
            if callable(restore_focus):
                restore_focus()
            elif saved is not None and hasattr(saved, "setFocus"):
                saved.setFocus()
        except Exception as e:
            log.debug("focus_preserved restore: %s", e)


def pump_vcl_events(toolkit: Any) -> bool:
    """Call toolkit.processEventsToIdle(); only used from approved pump entry points."""
    global _vcl_pump_depth
    if toolkit is not None and hasattr(toolkit, "processEventsToIdle"):
        try:
            with _vcl_pump_lock:
                _vcl_pump_depth += 1
            try:
                toolkit.processEventsToIdle()
                return True
            finally:
                with _vcl_pump_lock:
                    _vcl_pump_depth -= 1
        except Exception:
            log.debug("processEventsToIdle failed", exc_info=True)
    return False


def vcl_pump_active() -> bool:
    """Return whether a VCL event pump is currently running on the stack."""
    with _vcl_pump_lock:
        return _vcl_pump_depth > 0


@main_thread_only
def process_events_to_idle(ctx: Any, rounds: int = 1, force: bool = False) -> bool:
    """Drain the UI event queue *rounds* times via the approved VCL pump chokepoint.

    When a chat/MCP :func:`~plugin.framework.async_drain_guard.drain_owner_scope` is
    active, or another WriterAgent pump is already on the stack, skips VCL pumping
    so secondary progress helpers and callbacks cannot nest ``processEventsToIdle``.
    Pass force=True to pump VCL regardless (reserved for native test hooks such as
    ``sidebar_test_hooks.pump_until``).
    Returns True if at least one VCL pump ran. Blocking secondary waits should
    use :func:`wait_while_pumping` rather than a local PE2I loop.
    """
    from plugin.framework.async_drain_guard import get_drain_owner, note_suppressed_vcl_pump
    from plugin.framework.uno_context import get_toolkit

    # MainThread.post can fall back onto the caller when AsyncCallback
    # is missing. process_events_to_idle is @main_thread_only — skip.
    if not main.is_current():
        return False

    if not force:
        if main.policy is Policy.test_inline:
            return False
        owner = get_drain_owner()
        if owner is not None:
            note_suppressed_vcl_pump(owner)
            return False
        if vcl_pump_active():
            note_suppressed_vcl_pump(None)
            return False

    try:
        tk = get_toolkit(ctx)
    except Exception:
        log.debug("process_events_to_idle: failed to get toolkit", exc_info=True)
        return False
    if tk is None:
        return False

    pumped = False
    for _idx in range(max(1, rounds)):
        if pump_vcl_events(tk):
            pumped = True
    return pumped


def _post_secondary_idle(ctx: Any) -> None:
    """Enqueue one PE2I tick on the VCL thread. Must not run PE2I on the waiter."""
    global _secondary_idle_in_flight

    def _pump() -> None:
        global _secondary_idle_in_flight
        try:
            # post() can run this on the waiter when AsyncCallback is missing.
            # process_events_to_idle is main-thread-only; skip rather than pump here.
            if main.is_current():
                process_events_to_idle(ctx, force=False)
        finally:
            with _secondary_idle_lock:
                _secondary_idle_in_flight = False

    with _secondary_idle_lock:
        if _secondary_idle_in_flight:
            return
        _secondary_idle_in_flight = True
    try:
        main.post(_pump, bound_scope=None)
    except Exception:
        with _secondary_idle_lock:
            _secondary_idle_in_flight = False
        raise


def pump_secondary_idle_tick(ctx: Any, *, on_caller: bool | None = None) -> None:
    """One secondary-caller idle tick: PE2I on the main thread, else post it there.

    Same per-tick pump as :func:`wait_while_pumping`, for wait loops that block
    on something other than an Event (e.g. a lock). Failures are swallowed.
    """
    if on_caller is None:
        on_caller = main.is_current()
    try:
        if on_caller:
            process_events_to_idle(ctx, force=False)
        else:
            _post_secondary_idle(ctx)
    except Exception:
        log.debug("wait_while_pumping process_events_to_idle failed", exc_info=True)


def wait_while_pumping(done: "threading.Event", ctx: Any, *, timeout: float, poll_sec: float = 0.075) -> bool:
    """Wait for *done* while pumping VCL as a secondary caller.

    On the LibreOffice main thread, each tick calls :func:`process_events_to_idle`
    with ``force=False`` so a chat/MCP drain owner suppresses nested VCL.
    Off the main thread (Writer ``doProofreading`` linguistic workers are
    ``Dummy-*``, not VCL) PE2I is **posted** to the main thread — never called
    on the waiter. Calling PE2I on Dummy-21 popped a UNO thread-violation
    dialog every poll tick (the wait loop from #778). Repeated off-main ticks
    coalesce to one outstanding secondary-idle pump; other marshal items do
    not count. Drain-owner wait loops must keep using
    :func:`~plugin.framework.queue_executor.pump_ui_idle` /
    ``run_blocking_in_thread``, not this helper.

    Default *poll_sec* is 75ms (stay inside 50–100ms; same band as the
    linguistic PE2I-in-proofread wait). Returns True if *done* was set, False
    if *timeout* elapsed first. Post/PE2I failures are swallowed so a pump
    miss cannot abort the wait.
    """
    pump_on_caller = main.is_current()
    deadline = time.monotonic() + max(0.0, timeout)
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        pump_secondary_idle_tick(ctx, on_caller=pump_on_caller)
        if done.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        done.wait(timeout=min(poll_sec, remaining))
    return True
