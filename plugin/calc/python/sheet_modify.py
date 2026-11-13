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
(RuntimeUID), not the file URL. A URL key would be wrong: every unsaved
workbook has an empty URL (they would share one listener) and Save As changes
it (the sheet would get a second ``addModifyListener``). ``modified``
reconciles the document that owns the sheet, not whichever component is
currently active.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Any, Generator

from plugin.calc.python.cell_discovery import sheet_name_of
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.errors import uno_read
from plugin.framework.queue_executor import main
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_listeners import BaseModifyListener
from plugin.scripting.session_manager import calc_workbook_base_session_id

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

    for s in WorkbookState.all_states():
        s.debounce.cancel_all()
    _DISPATCH_DEPTH = 0


def owning_calc_doc(sheet: Any) -> Any | None:
    """Spreadsheet that contains *sheet*, walked from the modify event source.

    ``desktop.getCurrentComponent()`` is whichever window is focused. A change
    in a background workbook must not reconcile that other file.
    """
    current = sheet
    for _hop in range(4):
        if current is None:
            return None
        supports: Any = getattr(current, "supportsService", None)
        # Real UNO returns a Python bool (pyuno_runtime.cxx, Runtime::any2PyObject,
        # TypeClass_BOOLEAN); any other answer does not name an owner, and the
        # dispatcher's stored document is used instead.
        if callable(supports) and uno_read(lambda: supports("com.sun.star.sheet.SpreadsheetDocument"), False) is True:
            return current
        parent = getattr(current, "getParent", None)
        if not callable(parent):
            return None
        current = uno_read(parent, None)
    return None


def schedule_sheet_modify_pass(ctx: Any, doc: Any, sheet: Any, *, sheet_name: str = "", delay_sec: float = _MODIFY_DELAY_SEC) -> None:
    """Debounce: schedule a 0.1s UI-thread pass via WorkbookState.debounce."""
    if sheet is None or doc is None:
        return
    name = sheet_name or sheet_name_of(sheet)
    if not name:
        return
    state = WorkbookState.lookup(doc)
    if state is None or not state.key:
        return

    debounce_key = f"sheet:{name}"
    disp = state.listeners.get(name)

    def _pass() -> None:
        # migrate_sheet_rename moves this dispatcher to the new key and updates
        # its sheet_name while the timer waits, so reading it here gives the live
        # name. Using another dispatcher of the same document would clean up the
        # wrong sheet's spill records.
        run_sheet_modify_pass(ctx, doc, sheet, sheet_name=disp.sheet_name if disp is not None else name)

    state.debounce.schedule(debounce_key, delay_sec, _pass)


def flush_sheet_modify_pass_for_tests(ctx: Any, doc: Any, sheet: Any, *, sheet_name: str = "") -> None:
    """Cancel debounce and run the pass on this thread. Tests only."""
    name = sheet_name or sheet_name_of(sheet)
    state = WorkbookState.lookup(doc)
    if state is not None:
        state.debounce.cancel(f"sheet:{name}")
    run_sheet_modify_pass(ctx, doc, sheet, sheet_name=name)


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
    # Skip migration when workbook identity is empty—otherwise empty filters rekey unrelated workbooks.
    if state is None or not state.key:
        log.warning("migrate_sheet_rename: no workbook identity found; skipping in-memory migration")
        return

    # Rekey state.spills for this doc
    try:
        # Lazy: spill imports this module at its top.
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
            disp.sheet_name = new_sheet_name
            state.listeners[new_sheet_name] = disp
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate listeners")

    # Rekey debounce
    state.debounce.rekey(f"sheet:{old_sheet_name}", f"sheet:{new_sheet_name}")

    # Rekey geometric records and persist
    try:
        # Lazy: geometric_recalc imports this module at its top.
        from plugin.calc.python.geometric_recalc import (
            records_for_sheet,
            replace_records_for_sheet,
            save_geometric_registry_for_doc,
        )

        if doc is not None:
            workbook_key = calc_workbook_base_session_id(doc)
            if workbook_key:
                old_records = records_for_sheet(workbook_key, old_sheet_name)
                if old_records:
                    replace_records_for_sheet(workbook_key, new_sheet_name, old_records)
                    replace_records_for_sheet(workbook_key, old_sheet_name, {})
                save_geometric_registry_for_doc(doc, workbook_key)
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate geometric records")


def _skip_sheet_modify() -> bool:
    # Lazy: geometric_recalc imports this module at its top.
    from plugin.calc.python.geometric_recalc import is_geometric_repairing

    return (not main.is_current()) or _DISPATCH_DEPTH > 0 or is_geometric_repairing()


def run_sheet_modify_pass(
    ctx: Any,
    doc: Any,
    sheet: Any,
    *,
    sheet_name: str = "",
) -> None:
    """UI-thread jobs after the shared debounce. Spill and geometric stay separate.

    Spill: ``cleanup_orphans`` on ``state.spills`` only.
    Geometric: own ``discover_python_cells_on_sheet`` via ``reconcile_geometric_sheet``.
    ``schedule_sheet_modify_pass`` refuses a None *doc*, so *doc* is non-None here.
    """
    if _skip_sheet_modify():
        return
    if sheet is None:
        return
    name = sheet_name or sheet_name_of(sheet)
    if not name:
        log.debug("run_sheet_modify_pass: unreadable sheet name; skipping")
        return
    with sheet_modify_scope():
        # for_doc (not known) so RuntimeUID rekey runs; UI thread only (_skip_sheet_modify guards).
        state = WorkbookState.for_doc(doc)
        # Job 1 — spill orphan cleanup. Walks state.spills only.
        try:
            # Lazy: spill imports this module at its top.
            from plugin.calc.python.spill import cleanup_orphans

            if state is not None:
                cleanup_orphans(state, sheet, doc=doc, sheet_name=name)
        except Exception:
            log.exception("Error in spill orphan cleanup (%s)", name)

        # Job 2 — geometric list-diff. Own discovery; skip when flag is off.
        try:
            # Lazy: geometric_recalc imports this module at its top.
            from plugin.calc.python.geometric_recalc import geometric_flag_enabled, reconcile_geometric_sheet

            if geometric_flag_enabled():
                reconcile_geometric_sheet(ctx, doc, sheet)
        except Exception:
            log.exception("Error in geometric sheet modify pass (%s)", name)


@main_thread_only
def dispatch_sheet_modified(
    ctx: Any,
    sheet_name: str,
    event: Any,
    doc: Any,
) -> None:
    """Shared ``modified`` entry. Debounces; does not walk ``state.spills``."""
    if _skip_sheet_modify():
        return
    sheet = getattr(event, "Source", None)
    if sheet is None:
        return
    # Reconcile the sheet's owner (or the document stored on the dispatcher),
    # not whichever component happens to be the focused window.
    owner = owning_calc_doc(sheet)
    if owner is None:
        owner = doc
    live = sheet_name_of(sheet)
    effective_name = sheet_name
    if live and sheet_name and live != sheet_name:
        migrate_sheet_rename(owner, sheet_name, live)
        effective_name = live
    schedule_sheet_modify_pass(ctx, owner, sheet, sheet_name=effective_name)


def ensure_sheet_modify_listener(ctx: Any, doc: Any, sheet: Any) -> Any | None:
    """Register the one dispatcher on *sheet*. Idempotent; no second listener."""
    if doc is None or sheet is None:
        return None
    name = sheet_name_of(sheet)
    if not name:
        return None
    state = WorkbookState.for_doc(doc)
    if state is None:
        return None

    existing = state.listeners.get(name)
    if existing is not None:
        existing.doc = doc
        return existing
    listener = SheetModifyDispatcher(ctx, doc, name)
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
    sheet_name: str

    def __init__(self, ctx: Any, doc: Any, sheet_name: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.doc = doc
        self.sheet_name = sheet_name

    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            dispatch_sheet_modified(self.ctx, self.sheet_name, aEvent, self.doc)
        except Exception:
            log.exception("Error in SheetModifyDispatcher.modified")

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        state = WorkbookState.known(self.doc)
        if state is not None:
            if state.listeners.get(self.sheet_name) is self:
                state.listeners.pop(self.sheet_name, None)
            state.debounce.cancel(f"sheet:{self.sheet_name}")
