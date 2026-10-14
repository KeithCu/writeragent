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
        if current is None:
            return None
        supports = getattr(current, "supportsService", None)
        if callable(supports):
            try:
                result = supports("com.sun.star.sheet.SpreadsheetDocument")
            except Exception:
                result = False
            if result is True:
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


def schedule_sheet_modify_pass(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "", delay_sec: float = _MODIFY_DELAY_SEC) -> None:
    """Debounce: cancel the sheet's pending timer, start a new 0.1s UI-thread pass.

    Same shape as ``perform_deferred_spill`` (Timer → ``post_to_main_thread``).
    Unload cancels via the shared spill-timer registry.
    """
    if sheet is None:
        return
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    lifecycle_key = ""
    if doc is not None:
        try:
            from plugin.calc.python.workbook_lifecycle import _lifecycle_key

            lifecycle_key = str(_lifecycle_key(doc) or "")
        except Exception:
            log.debug("sheet_modify: lifecycle key failed", exc_info=True)
    identity = lifecycle_key or url
    key = _sheet_key(identity, name)
    _cancel_pending(key)

    timer_holder: list[Any] = []

    def _fire() -> None:
        with _PENDING_LOCK:
            if timer_holder and _PENDING_TIMERS.get(key) is timer_holder[0]:
                _PENDING_TIMERS.pop(key, None)
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(lambda: run_sheet_modify_pass(ctx, doc, sheet, doc_url=url, sheet_name=name))

    from plugin.calc.python.function import start_deferred_sheet_timer

    # Timer lives in function.py (Layer C allowlist) — same 0.1s spill site.
    timer = start_deferred_sheet_timer(delay_sec, _fire, lifecycle_key=lifecycle_key)
    timer_holder.append(timer)
    with _PENDING_LOCK:
        _PENDING_TIMERS[key] = timer


def flush_sheet_modify_pass_for_tests(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "") -> None:
    """Cancel debounce and run the pass on this thread. Tests only."""
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    identity = _doc_identity(doc) or url
    _cancel_pending(_sheet_key(identity, name))
    run_sheet_modify_pass(ctx, doc, sheet, doc_url=url, sheet_name=name)


def migrate_sheet_rename(
    doc: Any,
    old_sheet_name: str,
    new_sheet_name: str,
    dispatcher: "SheetModifyDispatcher | None" = None,
) -> None:
    """Migrate in-memory and persistent state when a sheet is renamed.

    Rekeys SPILL_REGISTRY, SHEET_MODIFY_LISTENERS, _PENDING_TIMERS,
    geometric records, and updates dispatcher.sheet_name.
    """
    if not old_sheet_name or not new_sheet_name or old_sheet_name == new_sheet_name:
        return

    identity = _doc_identity(doc) if doc is not None else ""
    url = _doc_url_of(doc) if doc is not None else ""

    if dispatcher is not None:
        dispatcher.sheet_name = new_sheet_name

    # Rekey SPILL_REGISTRY for this doc
    try:
        from plugin.calc.python.function import (
            SPILL_REGISTRY,
            _SPILL_REGISTRY_LOCK,
            _spill_registry_doc_key,
            save_spill_registry_for_doc,
        )

        doc_key = _spill_registry_doc_key(doc) if doc is not None else ""
        with _SPILL_REGISTRY_LOCK:
            keys_to_migrate = [
                k for k in list(SPILL_REGISTRY.keys())
                if k[1] == old_sheet_name and (not doc_key or k[0] in (doc_key, identity, url))
            ]
            for k in keys_to_migrate:
                val = SPILL_REGISTRY.pop(k)
                SPILL_REGISTRY[(k[0], new_sheet_name, k[2], k[3])] = val
        if doc is not None:
            save_spill_registry_for_doc(doc)
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate SPILL_REGISTRY")

    # Rekey SHEET_MODIFY_LISTENERS
    try:
        from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

        for skey in list(SHEET_MODIFY_LISTENERS.keys()):
            if skey[1] == old_sheet_name and (not identity or skey[0] in (identity, url)):
                disp = SHEET_MODIFY_LISTENERS.pop(skey)
                if hasattr(disp, "sheet_name"):
                    disp.sheet_name = new_sheet_name
                SHEET_MODIFY_LISTENERS[(skey[0], new_sheet_name)] = disp
                if dispatcher is None and isinstance(disp, SheetModifyDispatcher):
                    dispatcher = disp
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate SHEET_MODIFY_LISTENERS")

    # Rekey _PENDING_TIMERS
    with _PENDING_LOCK:
        for pkey in list(_PENDING_TIMERS.keys()):
            if pkey[1] == old_sheet_name and (not identity or pkey[0] in (identity, url)):
                timer = _PENDING_TIMERS.pop(pkey)
                _PENDING_TIMERS[(pkey[0], new_sheet_name)] = timer

    # Rekey geometric records and persist
    try:
        from plugin.calc.python.geometric_recalc import (
            geometric_workbook_key,
            records_for_sheet,
            replace_records_for_sheet,
            save_geometric_registry_for_doc,
        )

        if doc is not None:
            gwk = geometric_workbook_key(doc)
            if gwk:
                old_records = records_for_sheet(gwk, old_sheet_name)
                if old_records:
                    replace_records_for_sheet(gwk, new_sheet_name, old_records)
                    replace_records_for_sheet(gwk, old_sheet_name, {})
                save_geometric_registry_for_doc(doc, gwk)
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate geometric records")


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
        try:
            registry_id = _spill_registry_doc_key(doc) if doc is not None else url
            CalcSpillModifyListener(ctx, registry_id, name).modified(SimpleNamespace(Source=sheet))
        except Exception:
            log.exception("Error in spill orphan cleanup (%s)", name)

        # Job 2 — geometric list-diff. Own discovery; skip when flag is off.
        try:
            from plugin.calc.python.geometric_recalc import geometric_flag_enabled, reconcile_geometric_sheet

            if geometric_flag_enabled() and doc is not None:
                reconcile_geometric_sheet(ctx, doc, sheet)
        except Exception:
            log.exception("Error in geometric sheet modify pass (%s)", name)
    except Exception:
        log.exception("Error in sheet modify pass (%s)", name)
    finally:
        _DISPATCHING = False


def dispatch_sheet_modified(
    ctx: Any,
    doc_key: str,
    sheet_name: str,
    event: Any,
    doc: Any = None,
    dispatcher: "SheetModifyDispatcher | None" = None,
) -> None:
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
    live = _sheet_name_of(sheet)
    effective_name = sheet_name
    if live and sheet_name and live != sheet_name:
        migrate_sheet_rename(owner, sheet_name, live, dispatcher=dispatcher)
        effective_name = live
    live_url = _doc_url_of(owner) if owner is not None else ""
    schedule_sheet_modify_pass(ctx, owner, sheet, doc_url=live_url or doc_key, sheet_name=effective_name)


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
    if existing is not None:
        existing.doc = doc
        existing.doc_identity = identity
        if url:
            existing.doc_key = url
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
    doc_key: str
    sheet_name: str
    doc_identity: str

    def __init__(self, ctx: Any, doc: Any, doc_key: str, sheet_name: str, doc_identity: str) -> None:
        self.ctx = ctx
        self.doc = doc
        self.doc_key = doc_key
        self.sheet_name = sheet_name
        self.doc_identity = doc_identity

    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            dispatch_sheet_modified(self.ctx, self.doc_key, self.sheet_name, aEvent, doc=self.doc, dispatcher=self)
        except Exception:
            log.exception("Error in SheetModifyDispatcher.modified")

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

        SHEET_MODIFY_LISTENERS.pop((self.doc_identity, self.sheet_name), None)
        if self.doc_key and self.doc_key != self.doc_identity:
            SHEET_MODIFY_LISTENERS.pop((self.doc_key, self.sheet_name), None)
        # Ensure any key in SHEET_MODIFY_LISTENERS pointing to self is removed
        for k, v in list(SHEET_MODIFY_LISTENERS.items()):
            if v is self:
                SHEET_MODIFY_LISTENERS.pop(k, None)
                _cancel_pending(k)
        _cancel_pending((self.doc_identity, self.sheet_name))
        if self.doc_key and self.doc_key != self.doc_identity:
            _cancel_pending((self.doc_key, self.sheet_name))
