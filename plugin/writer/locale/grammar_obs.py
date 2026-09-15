# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""DEBUG observability and sidebar status helpers for the grammar worker."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, cast

from plugin.framework import event_bus

log = logging.getLogger("writeragent.grammar")


def grammar_obs(event: str, **fields: Any) -> None:
    """DEBUG-only observability for queue / worker (grep ``[grammar] obs`` in logs)."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.debug("[grammar] obs %s %s", event, kv)


_last_status_indicator: Any = None

# Per-keystroke Harper / enqueue strings. Painting these via XStatusIndicator
# replaces the Writer status bar on every doProofreading (issue #768).
_ROUTINE_LIBREHARPER_STATUS_RESULTS = frozenset({
    "harper",
    "harper check",
    "queued",
})
_ISSUE_COUNT_RESULT = re.compile(r"^\d+ issues?$", re.IGNORECASE)

_UNO_HELPER_BASENAMES = frozenset({
    "uno",
    "uno.bin",
    "unopkg",
    "unopkg.bin",
    "unopkg.com",
    "unopkg.exe",
})


def is_routine_libreharper_status(phase: str, result: str) -> bool:
    """True for steady-state LibreHarper strings that must not paint the status bar.

    Failures always paint. Progress text (download, starting harper-ls) is not routine.
    """
    if phase == "failed":
        return False
    normalized = (result or "").strip().lower()
    if not normalized:
        return True
    if normalized in _ROUTINE_LIBREHARPER_STATUS_RESULTS:
        return True
    return _ISSUE_COUNT_RESULT.fullmatch(normalized) is not None


def desktop_create_is_unsafe() -> bool:
    """True in uno.bin / unopkg helpers that have no VCL.

    ``createInstanceWithContext("com.sun.star.frame.Desktop")`` on that ctx
    takes SolarMutexGuard → GetYieldMutex and SEGVs (issue #768). GUI soffice
    already has Desktop; this is only the register/enable URP helper.
    """
    argv = [str(arg) for arg in sys.argv]
    argv0 = os.path.basename(argv[0]) if argv else ""
    if argv0 in _UNO_HELPER_BASENAMES:
        return True
    return any(arg == "--singleaccept" or arg.startswith("--singleaccept=") for arg in argv)


def _frame_from_controller(obj: Any) -> Any | None:
    try:
        getter = getattr(obj, "getCurrentController", None)
        controller = getter() if callable(getter) else None
        if controller is None:
            return None
        return cast("Any", controller).getFrame()
    except Exception:
        return None


def _frame_from_bound_grammar_docs() -> Any | None:
    """Frame from a Writer model already bound by proofreading — no Desktop create."""
    try:
        from plugin.writer.locale.grammar_persistence import grammar_registry
    except Exception:
        return None
    try:
        with grammar_registry.lock:
            persistences = list(grammar_registry.doc_persistence_instances.values())
    except Exception:
        return None
    for persistence in persistences:
        model = getattr(persistence, "_model", None)
        if model is None:
            continue
        frame = _frame_from_controller(model)
        if frame is not None:
            return frame
    return None


def _frame_from_existing_desktop_singleton(ctx: Any) -> Any | None:
    """Look up ``theDesktop`` only when it cannot SEGV; never create Desktop.

    Register-time uno.bin has no VCL. ``get_active_document`` / ``get_desktop``
    always ``createInstanceWithContext(Desktop)`` and that is the #768 crash.
    Singleton lookup on a helper ctx can instantiate too, so skip it there.
    No existing Desktop → fail soft (status paint is optional).
    """
    if ctx is None or desktop_create_is_unsafe():
        return None
    try:
        gvn = getattr(ctx, "getValueByName", None)
        if not callable(gvn):
            return None
        desktop = cast("Any", gvn("/singletons/com.sun.star.frame.theDesktop"))
        if desktop is None:
            return None
        try:
            frame = desktop.getCurrentFrame()
            if frame is not None:
                return frame
        except Exception:
            pass
        component = desktop.getCurrentComponent() if hasattr(desktop, "getCurrentComponent") else None
        return _frame_from_controller(component) if component is not None else None
    except Exception:
        return None


def resolve_status_bar_frame(ctx: Any) -> Any | None:
    """Resolve a frame for XStatusIndicator without creating Desktop.

    Prefer a bound proofreading document. If none, use an already-live Desktop
    singleton in GUI soffice. Missing frame → skip paint (do not call
    ``get_desktop`` / ``get_active_document``).
    """
    frame = _frame_from_bound_grammar_docs()
    if frame is not None:
        return frame
    return _frame_from_existing_desktop_singleton(ctx)


def update_libreoffice_status_bar(phase: str, text: str, result: str) -> None:
    """Update the LibreOffice window status bar using XStatusIndicator."""
    global _last_status_indicator
    if is_routine_libreharper_status(phase, result):
        return
    from plugin.framework.uno_context import get_ctx

    # Determine status message
    msg = f"LibreHarper: {result or 'Checking...'}"
    if phase in ("done", "complete"):
        msg = "LibreHarper: Grammar check complete"
    elif phase == "failed":
        msg = f"LibreHarper: Failed ({result})"

    try:
        ctx = get_ctx()
        # Never create Desktop here: uno.bin register ctx has no VCL (issue #768).
        frame = resolve_status_bar_frame(ctx)
        if frame is None:
            return

        if phase in ("start", "request"):
            if _last_status_indicator is None:
                try:
                    _last_status_indicator = frame.createStatusIndicator()
                    if _last_status_indicator is not None:
                        _last_status_indicator.start(msg, 100)
                except Exception:
                    _last_status_indicator = None
            else:
                try:
                    _last_status_indicator.setText(msg)
                    _last_status_indicator.setValue(50)
                except Exception:
                    pass
        elif phase in ("done", "complete", "failed"):
            if _last_status_indicator is not None:
                try:
                    _last_status_indicator.setText(msg)
                    _last_status_indicator.setValue(100)
                    _last_status_indicator.end()
                except Exception:
                    pass
                _last_status_indicator = None
    except Exception as e:
        log.debug("[grammar] update_libreoffice_status_bar failed: %s", e)
        _last_status_indicator = None


def emit_grammar_status(
    phase: str,
    text: str,
    *,
    result: str = "",
    elapsed_ms: int | None = None,
    preview_source: str | None = None,
    length_hint: int | None = None,
) -> None:
    """Emit status to the LibreOffice status bar (for LibreHarper) or sidebar event bus (for WriterAgent)."""
    from .grammar_proofread_text import slice_preview_debug
    from plugin.framework.uno_context import is_libreharper

    try:
        if preview_source is not None:
            raw = preview_source.strip() or "(empty)"
            preview = slice_preview_debug(raw, 10)
            length = len(raw) if length_hint is None else length_hint
        else:
            preview = slice_preview_debug(text.strip() or "(empty)", 10)
            length = len(text)

        if is_libreharper():
            # Skip main-thread posts for per-keystroke strings so typing does not
            # thrash XStatusIndicator (issue #768 status-bar flash / stalls).
            if is_routine_libreharper_status(phase, result):
                return
            from plugin.framework.queue_executor import post_to_main_thread
            from plugin.framework.thread_guard import get_background_task_name, on_main_thread

            # doProofreading is already on the VCL thread. post() only enqueues
            # AsyncCallback, so "Checking..." never paints until after the walk.
            if on_main_thread() and not get_background_task_name():
                update_libreoffice_status_bar(phase, text, result)
            else:
                try:
                    post_to_main_thread(update_libreoffice_status_bar, phase, text, result)
                except Exception:
                    log.debug("Failed to post status bar update to main thread", exc_info=True)
        else:
            event_bus.global_event_bus.emit("grammar:status", phase=phase, preview=preview, length=length, result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


def emit_harper_worker_status(sentence_text: str, message: str) -> None:
    """Relay Harper venv-worker progress to the sidebar grammar status field."""
    emit_grammar_status("request", sentence_text, result=message, preview_source=sentence_text)
