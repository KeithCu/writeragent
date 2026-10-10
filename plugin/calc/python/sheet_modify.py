# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-sheet modify dispatcher — shared trigger for spill cleanup and geometric repair.

``CalcSpillModifyListener.modified`` walks ``SPILL_REGISTRY`` only. Geometric
repair cannot piggyback on that walk (it does not scan formula cells) and must
not register a sibling ``CalcGeometricModifyListener``. This module is the
single ``addModifyListener`` per sheet: it shares the 0.1s timer, UI-thread
drain, ``_undo_lock``, and re-entrancy flag. After the debounce, spill cleanup
and geometric repair run as separate jobs — geometric then does its own
``list_python_cells_on_sheet``.

See ``docs/calc/geometric-recalc-order.md`` §3.6 / Phase 3.

Listeners and debounce timers are keyed by the workbook lifecycle id
(RuntimeUID), not the file URL. Every unsaved workbook has an empty URL, and
Save-As changes the URL; either one shared or duplicated the single
``addModifyListener``. ``modified`` reconciles the document that owns the
sheet, not whichever component is currently active.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

import unohelper
from com.sun.star.util import XModifyListener

log = logging.getLogger(__name__)

# Debounce one pass per sheet. Keyed by (lifecycle id, sheet_name).
# The file URL is empty for every unsaved workbook and changes on Save-As.
_PENDING_TIMERS: dict[tuple[str, str], threading.Timer] = {}
_PENDING_LOCK = threading.Lock()
# setFormula / clearContents during a pass re-enters modified(); skip.
_DISPATCHING = False
_MODIFY_DELAY_SEC = 0.1


def reset_sheet_modify_runtime_for_tests() -> None:
    """Drop debounce timers and the re-entrancy flag. Tests only."""
    global _DISPATCHING
    with _PENDING_LOCK:
        for timer in _PENDING_TIMERS.values():
            try:
                timer.cancel()
            except Exception:
                pass
        _PENDING_TIMERS.clear()
    _DISPATCHING = False


def is_sheet_modify_dispatching() -> bool:
    """True while a debounced pass is applying UNO writes."""
    return _DISPATCHING


def _sheet_key(doc_identity: str, sheet_name: str) -> tuple[str, str]:
    return (doc_identity, sheet_name)


def _doc_identity(doc: Any) -> str:
    """Stable per-document id. URL collides for unsaved books and moves on Save-As."""
    if doc is None:
        return ""
    try:
        from plugin.calc.python.workbook_lifecycle import _lifecycle_key

        key = _lifecycle_key(doc)
        if key:
            return str(key)
    except Exception:
        log.debug("sheet_modify: lifecycle key failed", exc_info=True)
    return _doc_url_of(doc)


def _owning_calc_doc(sheet: Any) -> Any | None:
    """Spreadsheet that contains *sheet*, walked from the modify event source.

    ``desktop.getCurrentComponent()`` is whichever window is focused. A change
    in a background workbook must not reconcile that other file. MagicMock
    fabricates ``getParent``; ignore it so tests that stub only ``_get_calc_doc``
    keep their fallback.
    """
    current = sheet
    for _hop in range(4):
        if current is None or type(current).__name__ == "MagicMock":
            return None
        supports = getattr(current, "supportsService", None)
        if callable(supports):
            try:
                result = supports("com.sun.star.sheet.SpreadsheetDocument")
            except Exception:
                result = False
            if type(result).__name__ != "MagicMock" and bool(result):
                return current
        parent = getattr(current, "getParent", None)
        if not callable(parent):
            return None
        try:
            current = parent()
        except Exception:
            return None
    return None


def _doc_url_of(doc: Any) -> str:
    try:
        return str(getattr(doc, "getURL", lambda: "")() or "")
    except Exception:
        return ""


def _sheet_name_of(sheet: Any) -> str:
    try:
        return str(sheet.getName() or "") or "Sheet1"
    except Exception:
        return "Sheet1"


def _lookup_sheet(doc: Any, sheet_name: str) -> Any | None:
    if doc is None:
        return None
    try:
        return doc.getSheets().getByName(sheet_name)
    except Exception:
        return None


def _cancel_pending(key: tuple[str, str]) -> None:
    with _PENDING_LOCK:
        timer = _PENDING_TIMERS.pop(key, None)
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass
        # Timer.cancel() sets finished. _register_spill_timer prunes finished
        # timers on the next register (#1098), so a reschedule drops this one
        # from _PENDING_SPILL_TIMERS.


def schedule_sheet_modify_pass(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "", delay_sec: float = _MODIFY_DELAY_SEC) -> None:
    """Debounce: cancel the sheet's pending timer, start a new 0.1s UI-thread pass.

    Same shape as ``perform_deferred_spill`` (Timer → ``post_to_main_thread``).
    Unload cancels via the shared spill-timer registry.
    """
    if sheet is None:
        return
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    identity = _doc_identity(doc) or url
    key = _sheet_key(identity, name)
    _cancel_pending(key)

    def _fire() -> None:
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(lambda: run_sheet_modify_pass(ctx, doc, sheet, doc_url=url, sheet_name=name))

    lifecycle_key = ""
    if doc is not None:
        try:
            from plugin.calc.python.workbook_lifecycle import _lifecycle_key

            lifecycle_key = _lifecycle_key(doc)
        except Exception:
            log.debug("sheet_modify: lifecycle key failed", exc_info=True)
    from plugin.calc.python.function import start_deferred_sheet_timer

    # Timer lives in function.py (Layer C allowlist) — same 0.1s spill site.
    timer = start_deferred_sheet_timer(delay_sec, _fire, lifecycle_key=lifecycle_key)
    with _PENDING_LOCK:
        _PENDING_TIMERS[key] = timer


def flush_sheet_modify_pass_for_tests(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "") -> None:
    """Cancel debounce and run the pass on this thread. Tests only."""
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    identity = _doc_identity(doc) or url
    _cancel_pending(_sheet_key(identity, name))
    run_sheet_modify_pass(ctx, doc, sheet, doc_url=url, sheet_name=name)


def run_sheet_modify_pass(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "") -> None:
    """UI-thread jobs after the shared debounce. Spill and geometric stay separate.

    Spill: ``CalcSpillModifyListener.modified`` (``SPILL_REGISTRY`` walk only).
    Geometric: own ``list_python_cells_on_sheet`` via ``reconcile_geometric_sheet``.
    """
    global _DISPATCHING
    from plugin.calc.python.geometric_recalc import is_geometric_repairing
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return
    if _DISPATCHING or is_geometric_repairing():
        return
    if doc is None:
        from plugin.calc.python.function import _get_calc_doc

        doc = _get_calc_doc(ctx)
    if sheet is None and doc is not None:
        sheet = _lookup_sheet(doc, sheet_name)
    if sheet is None:
        return
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    _DISPATCHING = True
    try:
        from plugin.calc.python.function import CalcSpillModifyListener, _spill_registry_doc_key

        # Job 1 — spill orphan cleanup. Walks SPILL_REGISTRY only.
        # Pass the lifecycle id the spill registry uses. The URL is empty
        # for every unsaved book, so matching on it hits the wrong book.
        registry_id = _spill_registry_doc_key(doc) if doc is not None else url
        CalcSpillModifyListener(ctx, registry_id, name).modified(SimpleNamespace(Source=sheet))

        # Job 2 — geometric list-diff. Own discovery; skip when flag is off.
        from plugin.calc.python.geometric_recalc import geometric_flag_enabled, reconcile_geometric_sheet

        if geometric_flag_enabled() and doc is not None:
            reconcile_geometric_sheet(ctx, doc, sheet)
    except Exception:
        log.exception("Error in sheet modify pass (%s)", name)
    finally:
        _DISPATCHING = False


def dispatch_sheet_modified(ctx: Any, doc_url: str, sheet_name: str, event: Any, doc: Any = None) -> None:
    """Shared ``modified`` entry. Debounces; does not walk ``SPILL_REGISTRY``."""
    from plugin.calc.python.geometric_recalc import is_geometric_repairing
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return
    if _DISPATCHING or is_geometric_repairing():
        return
    sheet = getattr(event, "Source", None)
    if sheet is None:
        return
    # Prefer the sheet's owner, then the document stored when the listener
    # was attached. _get_calc_doc is the active window, so a modify in a
    # background workbook was scheduled against that other document.
    owner = _owning_calc_doc(sheet)
    if owner is None:
        owner = doc
    if owner is None:
        from plugin.calc.python.function import _get_calc_doc

        owner = _get_calc_doc(ctx)
    live_url = _doc_url_of(owner) if owner is not None else ""
    schedule_sheet_modify_pass(ctx, owner, sheet, doc_url=live_url or doc_url, sheet_name=sheet_name)


def _dispatcher_on_sheet(sheet: Any) -> "SheetModifyDispatcher | None":
    """Listener already attached, when the sheet exposes its listener list.

    Real UNO does not enumerate modify listeners. The lifecycle-id map is the
    dedupe for a live document; this covers stubs and a URL-keyed entry that
    Save-As would otherwise miss.
    """
    listeners = getattr(sheet, "_modify_listeners", None)
    if not isinstance(listeners, list):
        return None
    for listener in listeners:
        if isinstance(listener, SheetModifyDispatcher):
            return listener
    return None


def ensure_sheet_modify_listener(ctx: Any, doc: Any, sheet: Any) -> Any | None:
    """Register the one dispatcher on *sheet*. Idempotent; no second listener."""
    if doc is None or sheet is None:
        return None
    url = _doc_url_of(doc)
    name = _sheet_name_of(sheet)
    identity = _doc_identity(doc) or url
    key = _sheet_key(identity, name)
    from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

    existing = SHEET_MODIFY_LISTENERS.get(key)
    if existing is None and url and url != identity:
        # A listener stored under the file URL (pre-identity key, or the URL
        # before Save-As) is the same sheet. Reusing it avoids a second
        # addModifyListener and drops the stale key.
        candidate = SHEET_MODIFY_LISTENERS.get((url, name))
        if isinstance(candidate, SheetModifyDispatcher):
            existing = SHEET_MODIFY_LISTENERS.pop((url, name), None)
    if existing is None:
        # Save-As changes a URL key. Match the same document object or the
        # same lifecycle id so the sheet keeps the listener it already has.
        for skey, listener in list(SHEET_MODIFY_LISTENERS.items()):
            if not isinstance(listener, SheetModifyDispatcher):
                continue
            same_sheet = getattr(listener, "sheet_name", None) == name
            same_doc = listener.doc is doc or getattr(listener, "doc_identity", None) == identity
            if same_sheet and same_doc:
                SHEET_MODIFY_LISTENERS.pop(skey, None)
                existing = listener
                break
    if existing is None:
        existing = _dispatcher_on_sheet(sheet)
    if existing is not None:
        existing.doc = doc
        existing.doc_identity = identity
        if url:
            existing.doc_url = url
        SHEET_MODIFY_LISTENERS[key] = existing
        return existing
    listener = SheetModifyDispatcher(ctx, doc, url, name, identity)
    try:
        sheet.addModifyListener(listener)
    except Exception:
        log.exception("Failed to register sheet modify dispatcher on %s", name)
        return None
    SHEET_MODIFY_LISTENERS[key] = listener
    return listener


class SheetModifyDispatcher(unohelper.Base, XModifyListener):
    """One ``XModifyListener`` per sheet. Schedules; does not own either job."""

    ctx: Any
    doc: Any
    doc_url: str
    sheet_name: str
    doc_identity: str

    def __init__(self, ctx: Any, doc: Any, doc_url: str, sheet_name: str, doc_identity: str) -> None:
        self.ctx = ctx
        self.doc = doc
        self.doc_url = doc_url
        self.sheet_name = sheet_name
        self.doc_identity = doc_identity

    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            dispatch_sheet_modified(self.ctx, self.doc_url, self.sheet_name, aEvent, doc=self.doc)
        except Exception:
            log.exception("Error in SheetModifyDispatcher.modified")

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

        SHEET_MODIFY_LISTENERS.pop((self.doc_identity, self.sheet_name), None)
        if self.doc_url and self.doc_url != self.doc_identity:
            SHEET_MODIFY_LISTENERS.pop((self.doc_url, self.sheet_name), None)
        _cancel_pending((self.doc_identity, self.sheet_name))
        if self.doc_url and self.doc_url != self.doc_identity:
            _cancel_pending((self.doc_url, self.sheet_name))
