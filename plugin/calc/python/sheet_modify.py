# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-sheet modify dispatcher — shared trigger for spill cleanup and geometric repair.

Spill orphan cleanup is ``cleanup_orphans`` on ``state.spills`` only. Geometric
repair cannot piggyback on that walk (it does not scan formula cells) and must
not register a sibling ``CalcGeometricModifyListener``. This module is the
only ``addModifyListener`` per sheet: it shares the 0.1s timer, UI-thread
drain, ``undo_lock``, and re-entrancy flag. After the debounce, spill cleanup
and geometric repair run as separate jobs — geometric then does its own
``discover_python_cells_on_sheet``.

See ``docs/calc/geometric-recalc-order.md`` §3.6 / Phase 3.

Listeners and debounce timers are keyed by the workbook lifecycle id
(RuntimeUID), not the file URL. Every unsaved workbook has an empty URL, and
Save-As changes the URL; either one shared or duplicated the single
``addModifyListener``. ``modified`` reconciles the document that owns the
sheet, not whichever component is currently active.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Any, Generator

from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.errors import best_effort, uno_read
from plugin.framework.queue_executor import main
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import BaseModifyListener

log = logging.getLogger(__name__)

# Modify dispatch depth: prevents re-entrant modified() dispatch during pass or spill writes.
_DISPATCH_DEPTH = 0
_MODIFY_DELAY_SEC = 0.1


@contextmanager
def sheet_modify_scope() -> Generator[None, None, None]:
    """Track sheet modify re-entrancy depth across operations."""
    global _DISPATCH_DEPTH
    _DISPATCH_DEPTH += 1
    try:
        yield
    finally:
        _DISPATCH_DEPTH -= 1


def reset_sheet_modify_runtime_for_tests() -> None:
    """Drop debounce timers and the re-entrancy flag. Tests only."""
    global _DISPATCH_DEPTH
    from plugin.calc.python.workbook_state import WorkbookState

    for s in WorkbookState.all_states():
        s.debounce.cancel_all()
    _DISPATCH_DEPTH = 0





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
        supports: Any = getattr(current, "supportsService", None)
        if callable(supports) and uno_read(lambda: supports("com.sun.star.sheet.SpreadsheetDocument"), False) is True:
            return current
        parent = getattr(current, "getParent", None)
        if not callable(parent):
            return None
        current = uno_read(parent, None)
    return None


def _sheet_name_of(sheet: Any) -> str:
    """Sheet name, or "" when the read fails. Never invent a default name."""
    if sheet is None:
        return ""
    return str(best_effort(lambda: sheet.getName(), "sheet_modify: getName failed", "") or "")


def _workbook_state_for_modify_pass(doc: Any | None, *, workbook_key: str = "") -> WorkbookState | None:
    """Return workbook state for spill cleanup (rekey on UI thread, then listener id).

    ``for_doc`` can attach a fresh empty state when PyUNO weakref lookup misses the
    document object that still owns ``state.spills``. The debounced pass always
    knows the listener's ``workbook_key``; prefer that state when it holds spills.
    """
    state = WorkbookState.for_doc(doc) if doc is not None else None
    keyed: WorkbookState | None = None
    if workbook_key:
        for candidate in WorkbookState.all_states():
            if candidate.scheduled_key_matches(workbook_key):
                keyed = candidate
                break
    if keyed is None:
        return state
    if state is None:
        return keyed
    if keyed is not state and keyed.spills and not state.spills:
        return keyed
    return state


def schedule_sheet_modify_pass(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "", delay_sec: float = _MODIFY_DELAY_SEC) -> None:
    """Debounce: schedule a 0.1s UI-thread pass via WorkbookState.debounce."""
    if sheet is None or doc is None:
        return
    name = sheet_name or _sheet_name_of(sheet)
    if not name:
        return
    state = WorkbookState.lookup(doc)
    if state is None or not state.key:
        return

    debounce_key = f"sheet:{name}"

    def _pass() -> None:
        sname = name
        disp = state.listeners.get(name)
        if disp is not None and hasattr(disp, "sheet_name"):
            sname = disp.sheet_name
        else:
            for candidate in state.listeners.values():
                if getattr(candidate, "doc", None) is doc and hasattr(candidate, "sheet_name"):
                    sname = candidate.sheet_name
                    break
        run_sheet_modify_pass(ctx, doc, sheet, doc_url=doc_url, sheet_name=sname, workbook_key=state.key)

    state.debounce.schedule(debounce_key, delay_sec, _pass)


def flush_sheet_modify_pass_for_tests(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "") -> None:
    """Cancel debounce and run the pass on this thread. Tests only."""
    name = sheet_name or _sheet_name_of(sheet)
    state = WorkbookState.lookup(doc)
    if state is not None:
        state.debounce.cancel(f"sheet:{name}")
    wk = state.key if state is not None else ""
    run_sheet_modify_pass(ctx, doc, sheet, doc_url=doc_url, sheet_name=name, workbook_key=wk)


def migrate_sheet_rename(
    doc: Any,
    old_sheet_name: str,
    new_sheet_name: str,
) -> None:
    """Migrate in-memory and persistent state when a sheet is renamed.

    Rekeys state.spills, state.listeners, state.debounce,
    geometric records, and updates dispatcher.sheet_name.
    """
    if not old_sheet_name or not new_sheet_name or old_sheet_name == new_sheet_name:
        return

    state = WorkbookState.lookup(doc)
    # Bugfix: What was wrong: migrate_sheet_rename migrated keys across unrelated workbooks when doc_key or identity was empty.
    # How it happened: The filter expression used (not doc_key or k[0] in ...) and (not identity or ...), treating an empty identity as a wildcard matching all keys.
    # Why this change fixes it: We collect non-empty IDs; if no valid IDs exist for the document, we log a warning and skip that map without rekeying other workbooks' entries.
    if state is None or not state.key:
        log.warning("migrate_sheet_rename: no workbook identity found; skipping in-memory migration")
        return

    # Rekey state.spills for this doc
    try:
        from plugin.calc.python.spill import save_spill_registry_for_doc

        for (sname, r, c) in list(state.spills):
            if sname == old_sheet_name:
                rec = state.spills.pop((sname, r, c))
                state.spills[(new_sheet_name, r, c)] = rec
        if doc is not None:
            save_spill_registry_for_doc(doc, state)
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate SPILL_REGISTRY")

    # Rekey state.listeners
    try:
        if old_sheet_name in state.listeners:
            disp = state.listeners.pop(old_sheet_name)
            setattr(disp, "sheet_name", new_sheet_name)
            state.listeners[new_sheet_name] = disp
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate listeners")

    # Rekey debounce
    state.debounce.rekey(f"sheet:{old_sheet_name}", f"sheet:{new_sheet_name}")

    # Rekey geometric records and persist
    try:
        from plugin.calc.python.geometric_recalc import (
            records_for_sheet,
            replace_records_for_sheet,
            save_geometric_registry_for_doc,
        )
        from plugin.scripting.session_manager import calc_workbook_base_session_id

        if doc is not None:
            gwk = calc_workbook_base_session_id(doc)
            if gwk:
                old_records = records_for_sheet(gwk, old_sheet_name)
                if old_records:
                    replace_records_for_sheet(gwk, new_sheet_name, old_records)
                    replace_records_for_sheet(gwk, old_sheet_name, {})
                save_geometric_registry_for_doc(doc, gwk)
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate geometric records")


def _skip_sheet_modify() -> bool:
    from plugin.calc.python.geometric_recalc import is_geometric_repairing

    return (not main.is_current()) or _DISPATCH_DEPTH > 0 or is_geometric_repairing()


def run_sheet_modify_pass(
    ctx: Any,
    doc: Any,
    sheet: Any,
    *,
    doc_url: str = "",
    sheet_name: str = "",
    workbook_key: str = "",
) -> None:
    """UI-thread jobs after the shared debounce. Spill and geometric stay separate.

    Spill: ``cleanup_orphans`` on ``state.spills`` only.
    Geometric: own ``discover_python_cells_on_sheet`` via ``reconcile_geometric_sheet``.
    """
    if _skip_sheet_modify():
        return
    if sheet is None:
        return
    name = sheet_name or _sheet_name_of(sheet)
    if not name:
        log.debug("run_sheet_modify_pass: unreadable sheet name; skipping")
        return
    with sheet_modify_scope():
        # Bugfix #3186: known-then-for_doc when is_current() was dead here because
        # _skip_sheet_modify already returned off the UI thread; RuntimeUID rekey was skipped.
        state = _workbook_state_for_modify_pass(doc, workbook_key=workbook_key)
        # Job 1 — spill orphan cleanup. Walks state.spills only.
        try:
            from plugin.calc.python.spill import cleanup_orphans

            cleanup_orphans(state, sheet, doc=doc, ctx=ctx, sheet_name=name)
        except Exception:
            log.exception("Error in spill orphan cleanup (%s)", name)

        # Job 2 — geometric list-diff. Own discovery; skip when flag is off.
        try:
            from plugin.calc.python.geometric_recalc import geometric_flag_enabled, reconcile_geometric_sheet

            if geometric_flag_enabled() and doc is not None:
                reconcile_geometric_sheet(ctx, doc, sheet)
        except Exception:
            log.exception("Error in geometric sheet modify pass (%s)", name)


@main_thread_only
def dispatch_sheet_modified(
    ctx: Any,
    doc_key: str,
    sheet_name: str,
    event: Any,
    doc: Any = None,
) -> None:
    """Shared ``modified`` entry. Debounces; does not walk ``state.spills``."""
    if _skip_sheet_modify():
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
        migrate_sheet_rename(owner, sheet_name, live)
        effective_name = live
    live_url = owner.getURL() if owner is not None and hasattr(owner, "getURL") else ""
    schedule_sheet_modify_pass(ctx, owner, sheet, doc_url=live_url or doc_key, sheet_name=effective_name)


def ensure_sheet_modify_listener(ctx: Any, doc: Any, sheet: Any) -> Any | None:
    """Register the one dispatcher on *sheet*. Idempotent; no second listener."""
    if doc is None or sheet is None:
        return None
    name = _sheet_name_of(sheet)
    if not name:
        return None
    from plugin.calc.python.workbook_state import WorkbookState

    state = WorkbookState.for_doc(doc)
    if state is None:
        return None

    url = doc.getURL() if hasattr(doc, "getURL") else ""
    existing = state.listeners.get(name)
    if existing is not None:
        existing.doc = doc
        existing.doc_identity = state.key
        if url:
            existing.doc_key = url
        return existing
    listener = SheetModifyDispatcher(ctx, doc, url or "", name, state.key)
    try:
        sheet.addModifyListener(listener)
    except Exception:
        log.exception("Failed to register sheet modify dispatcher on %s", name)
        return None
    state.listeners[name] = listener
    return listener


class SheetModifyDispatcher(BaseModifyListener):
    """One ``XModifyListener`` per sheet. Schedules; does not own either job."""

    ctx: Any
    doc: Any
    doc_key: str
    sheet_name: str
    doc_identity: str

    def __init__(self, ctx: Any, doc: Any, doc_key: str, sheet_name: str, doc_identity: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.doc = doc
        self.doc_key = doc_key
        self.sheet_name = sheet_name
        self.doc_identity = doc_identity

    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            dispatch_sheet_modified(self.ctx, self.doc_key, self.sheet_name, aEvent, doc=self.doc)
        except Exception:
            log.exception("Error in SheetModifyDispatcher.modified")

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        from plugin.calc.python.workbook_state import WorkbookState

        state = WorkbookState.known(self.doc)
        if state is not None:
            if state.listeners.get(self.sheet_name) is self:
                state.listeners.pop(self.sheet_name, None)
            state.debounce.cancel(f"sheet:{self.sheet_name}")
