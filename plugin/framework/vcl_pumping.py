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
import sys
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from plugin.framework.thread_guard import main_thread_only, on_main_thread

log = logging.getLogger("writeragent.vcl_pumping")

# One in-flight secondary-idle post. ``_SECONDARY_IDLE_RESERVING`` covers the
# window inside ``post_to_main_thread`` before the callable is visible on the
# queue. The stored callable is cleared on the next tick once it is no longer
# scheduled, so a drop is not sticky.
_SECONDARY_IDLE_RESERVING = object()
_secondary_idle_lock = threading.Lock()
_secondary_idle_posted: object | None = None


def _get_pe2i_fn() -> Callable[..., bool]:
    """Return process_events_to_idle, prioritizing any monkeypatch on uno_context."""
    uno_ctx_mod = sys.modules.get("plugin.framework.uno_context")
    if uno_ctx_mod is not None:
        pe2i = getattr(uno_ctx_mod, "process_events_to_idle", None)
        if pe2i is not None:
            return pe2i
    return process_events_to_idle


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


@main_thread_only
def process_events_to_idle(ctx: Any, rounds: int = 1, force: bool = False) -> bool:
    """Drain the UI event queue *rounds* times via the approved VCL pump chokepoint.

    When a chat/MCP :func:`~plugin.framework.queue_executor.drain_owner_scope` is
    active, skips VCL pumping so secondary progress helpers (grep, Harper status,
    notebook import) cannot nest ``processEventsToIdle`` inside the drain loop.
    Pass force=True (e.g. for RichTextControl caret reveal) to pump VCL even when
    under a drain owner.
    Returns True if at least one VCL pump ran. Blocking secondary waits should
    use :func:`wait_while_pumping` rather than a local PE2I loop.
    """
    from plugin.framework.queue_executor import _note_suppressed_vcl_pump, _pump_vcl_events, get_drain_owner
    from plugin.framework.uno_context import get_toolkit

    if not on_main_thread():
        return False

    if not force:
        if os.environ.get("WRITERAGENT_TESTING") == "1":
            return False
        owner = get_drain_owner()
        if owner is not None:
            _note_suppressed_vcl_pump(owner)
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
        try:
            if _pump_vcl_events(tk):
                pumped = True
        except Exception:
            log.debug("process_events_to_idle failed", exc_info=True)
    return pumped


def _post_secondary_idle(ctx: Any) -> None:
    """Enqueue one PE2I tick on the VCL thread. Must not run PE2I on the waiter."""
    global _secondary_idle_posted
    from plugin.framework.queue_executor import default_executor, post_to_main_thread

    def _pump() -> None:
        # QueueExecutor.post can fall back onto the caller when AsyncCallback
        # is missing. process_events_to_idle is @main_thread_only — skip.
        if not on_main_thread():
            return
        _get_pe2i_fn()(ctx, force=False)

    uno_ctx_mod = sys.modules.get("plugin.framework.uno_context")
    with _secondary_idle_lock:
        if uno_ctx_mod is not None and getattr(uno_ctx_mod, "_secondary_idle_posted", None) is None:
            _secondary_idle_posted = None
        posted = _secondary_idle_posted
        if posted is _SECONDARY_IDLE_RESERVING:
            return
        if posted is not None and default_executor.callable_is_scheduled(posted):
            return
        # Coalesce only this pump. pending_work_count() is the whole
        # process-wide marshal queue. Skipping the post whenever that count
        # is non-zero treats a leftover item from another test (pytest-xdist)
        # or unrelated UI work as "our pump is already queued", so a Dummy-*
        # linguistic wait never posts and the lint runs out its own timeout
        # (CI: ``posts["n"] == 0``, slow result elapsed_ms=2000). ``post``
        # dropping the callable, or a test double that does not enqueue it,
        # leaves nothing scheduled, so the next tick tries again.
        _secondary_idle_posted = _SECONDARY_IDLE_RESERVING
        if uno_ctx_mod is not None and hasattr(uno_ctx_mod, "_secondary_idle_posted"):
            setattr(uno_ctx_mod, "_secondary_idle_posted", _SECONDARY_IDLE_RESERVING)

    try:
        post_to_main_thread(_pump)
    except Exception:
        with _secondary_idle_lock:
            if _secondary_idle_posted is _SECONDARY_IDLE_RESERVING:
                _secondary_idle_posted = None
                if uno_ctx_mod is not None and hasattr(uno_ctx_mod, "_secondary_idle_posted"):
                    setattr(uno_ctx_mod, "_secondary_idle_posted", None)
        raise

    with _secondary_idle_lock:
        if _secondary_idle_posted is not _SECONDARY_IDLE_RESERVING:
            return
        if default_executor.callable_is_scheduled(_pump):
            _secondary_idle_posted = _pump
        else:
            _secondary_idle_posted = None
        if uno_ctx_mod is not None and hasattr(uno_ctx_mod, "_secondary_idle_posted"):
            setattr(uno_ctx_mod, "_secondary_idle_posted", _secondary_idle_posted)


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
    pe2i = _get_pe2i_fn()
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            if pump_on_caller:
                pe2i(ctx, force=False)
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
