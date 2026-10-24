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
import os
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from plugin.framework.thread_guard import main_thread_only, on_main_thread

log = logging.getLogger("writeragent.vcl_pumping")

# Coalesce to one in-flight secondary-idle post without scanning the process queue.
# Unscoped posts are not cancelled by Stop. The flag clears only when _pump finishes
# or post_to_main_thread raises. If a test double removes or drains the queued callable
# without running it, the flag stays set on purpose; do not scan the queue to clear it.
_secondary_idle_lock = threading.Lock()
_secondary_idle_in_flight = False


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
    if toolkit is not None and hasattr(toolkit, "processEventsToIdle"):
        try:
            toolkit.processEventsToIdle()
            return True
        except Exception:
            log.debug("processEventsToIdle failed", exc_info=True)
    return False


@main_thread_only
def process_events_to_idle(ctx: Any, rounds: int = 1, force: bool = False) -> bool:
    """Drain the UI event queue *rounds* times via the approved VCL pump chokepoint.

    When a chat/MCP :func:`~plugin.framework.async_drain_guard.drain_owner_scope` is
    active, skips VCL pumping so secondary progress helpers (grep, Harper status,
    notebook import) cannot nest ``processEventsToIdle`` inside the drain loop.
    Pass force=True (e.g. for RichTextControl caret reveal) to pump VCL even when
    under a drain owner.
    Returns True if at least one VCL pump ran. Blocking secondary waits should
    use :func:`wait_while_pumping` rather than a local PE2I loop.
    """
    from plugin.framework.async_drain_guard import get_drain_owner, note_suppressed_vcl_pump
    from plugin.framework.uno_context import get_toolkit

    # QueueExecutor.post can fall back onto the caller when AsyncCallback
    # is missing. process_events_to_idle is @main_thread_only — skip.
    if not on_main_thread():
        return False

    if not force:
        if os.environ.get("WRITERAGENT_TESTING") == "1":
            return False
        owner = get_drain_owner()
        if owner is not None:
            note_suppressed_vcl_pump(owner)
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
    from plugin.framework.queue_executor import post_to_main_thread

    def _pump() -> None:
        global _secondary_idle_in_flight
        try:
            # post() can run this on the waiter when AsyncCallback is missing.
            # process_events_to_idle is main-thread-only; skip rather than pump here.
            if on_main_thread():
                process_events_to_idle(ctx, force=False)
        finally:
            with _secondary_idle_lock:
                _secondary_idle_in_flight = False

    with _secondary_idle_lock:
        if _secondary_idle_in_flight:
            return
        _secondary_idle_in_flight = True
    try:
        post_to_main_thread(_pump, bound_scope=None)
    except Exception:
        with _secondary_idle_lock:
            _secondary_idle_in_flight = False
        raise


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
    pump_on_caller = on_main_thread()
    deadline = time.monotonic() + max(0.0, timeout)
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            if pump_on_caller:
                process_events_to_idle(ctx, force=False)
            else:
                _post_secondary_idle(ctx)
        except Exception:
            log.debug("wait_while_pumping process_events_to_idle failed", exc_info=True)
        if done.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        done.wait(timeout=min(poll_sec, remaining))
    return True
