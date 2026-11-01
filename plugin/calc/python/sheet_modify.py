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
import threading
from types import SimpleNamespace
from typing import Any, Iterator

from plugin.framework.errors import best_effort, uno_probe
from plugin.framework.uno_listeners import BaseModifyListener

log = logging.getLogger(__name__)

# Debounce one pass per sheet. Keyed by (lifecycle id, sheet_name).
# The file URL is empty for every unsaved workbook and changes on Save-As.
_PENDING_TIMERS: dict[tuple[str, str], threading.Timer] = {}
_PENDING_LOCK = threading.Lock()
# Modify dispatch depth: prevents re-entrant modified() dispatch during pass or spill writes.
_DISPATCH_DEPTH = 0
_MODIFY_DELAY_SEC = 0.1


@contextmanager
def sheet_modify_scope() -> Iterator[None]:
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
    with _PENDING_LOCK:
        for timer in list(_PENDING_TIMERS.values()):
            timer.cancel()
        _PENDING_TIMERS.clear()
    _DISPATCH_DEPTH = 0


def _doc_identity(doc: Any) -> str:
    """Stable per-document id. URL collides for unsaved books and moves on Save-As."""
    if doc is None:
        return ""
    from plugin.calc.python.workbook_lifecycle import _lifecycle_key

    return str(uno_probe(lambda: _lifecycle_key(doc), "") or "")


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
        if callable(supports) and uno_probe(lambda: supports("com.sun.star.sheet.SpreadsheetDocument"), False) is True:
            return current
        parent = getattr(current, "getParent", None)
        if not callable(parent):
            return None
        current = uno_probe(parent, None)
    return None


# Bugfix: What was wrong: posted pass outlives the doc; empty sheet name became "Sheet1".
# How it happened: unguarded UNO getURL()/getName() calls, and `or "Sheet1"` fallback on empty names.
# Why this change fixes it: best_effort returns "" on failure/disposal, and run_sheet_modify_pass aborts when name is empty.
def _doc_url_of(doc: Any) -> str:
    """URL, or "" when the document is gone. A posted pass can outlive it."""
    if doc is None:
        return ""
    return str(best_effort(lambda: doc.getURL(), "sheet_modify: getURL failed", "") or "")


def _sheet_name_of(sheet: Any) -> str:
    """Sheet name, or "" when the read fails. Never invent a default name."""
    if sheet is None:
        return ""
    return str(best_effort(lambda: sheet.getName(), "sheet_modify: getName failed", "") or "")


def _cancel_pending(key: tuple[str, str]) -> None:
    with _PENDING_LOCK:
        timer = _PENDING_TIMERS.pop(key, None)
    if timer is not None:
        timer.cancel()


def cancel_pending_sheet_timers(identity: str) -> None:
    """Cancel and pop all pending sheet debounce timers for a document identity.

    Bugfix: What was wrong: clear_in_memory_spill_state cancels the threading.Timer stored
    in _PENDING_SPILL_TIMERS, so _fire never runs and never pops _PENDING_TIMERS.
    How it happened: The cancelled timer's closure kept ctx, doc, and sheet. disposing is
    not the only unload path.
    Why this change fixes it: Pop every _PENDING_TIMERS entry matching identity under
    _PENDING_LOCK and cancel them outside the lock. Cancel is idempotent if the timer
    was already cancelled.
    """
    if not identity:
        return
    with _PENDING_LOCK:
        timers = [_PENDING_TIMERS.pop(k) for k in list(_PENDING_TIMERS) if k[0] == identity]
    for timer in timers:
        timer.cancel()


def schedule_sheet_modify_pass(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "", delay_sec: float = _MODIFY_DELAY_SEC) -> None:
    """Debounce: cancel the sheet's pending timer, start a new 0.1s UI-thread pass.

    Same shape as ``perform_deferred_spill`` (Timer → ``post_to_main_thread``).
    Unload cancels via the shared spill-timer registry.
    """
    if sheet is None:
        return
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    lifecycle_key = _doc_identity(doc)
    identity = lifecycle_key or url
    key = (identity, name)
    _cancel_pending(key)

    timer_holder: list[Any] = []

    def _fire() -> None:
        sheet_name_to_run = name
        with _PENDING_LOCK:
            if timer_holder:
                this_timer = timer_holder[0]
                for pkey, val in list(_PENDING_TIMERS.items()):
                    if val is this_timer:
                        _PENDING_TIMERS.pop(pkey, None)
                        sheet_name_to_run = pkey[1]
                        break
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(lambda: run_sheet_modify_pass(ctx, doc, sheet, doc_url=url, sheet_name=sheet_name_to_run))

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
    identity = _doc_identity(doc) or _doc_url_of(doc) or url
    _cancel_pending((identity, name))
    run_sheet_modify_pass(ctx, doc, sheet, doc_url=url, sheet_name=name)


def migrate_sheet_rename(
    doc: Any,
    old_sheet_name: str,
    new_sheet_name: str,
) -> None:
    """Migrate in-memory and persistent state when a sheet is renamed.

    Rekeys SPILL_REGISTRY, SHEET_MODIFY_LISTENERS, _PENDING_TIMERS,
    geometric records, and updates dispatcher.sheet_name.
    """
    if not old_sheet_name or not new_sheet_name or old_sheet_name == new_sheet_name:
        return

    # Bugfix: What was wrong: migrate_sheet_rename migrated keys across unrelated workbooks when doc_key or identity was empty.
    # How it happened: The filter expression used (not doc_key or k[0] in ...) and (not identity or ...), treating an empty identity as a wildcard matching all keys.
    # Why this change fixes it: We collect non-empty IDs; if no valid IDs exist for the document, we log a warning and skip that map without rekeying other workbooks' entries.
    identity = _doc_identity(doc)
    url = _doc_url_of(doc)
    ids = {x for x in (identity, url) if x}

    def _rekey(mapping: dict[Any, Any], touch: Any = None) -> None:
        for key in list(mapping):
            if key[1] == old_sheet_name and key[0] in ids:
                value = mapping.pop(key)
                if touch is not None:
                    touch(value)
                mapping[(key[0], new_sheet_name, *key[2:])] = value

    # Rekey SPILL_REGISTRY for this doc
    try:
        from plugin.calc.python.function import (
            SPILL_REGISTRY,
            _spill_registry_doc_key,
            save_spill_registry_for_doc,
        )

        doc_key = _spill_registry_doc_key(doc) if doc is not None else ""
        if doc_key:
            ids.add(doc_key)
        _rekey(SPILL_REGISTRY)
        if doc is not None:
            save_spill_registry_for_doc(doc)
    except Exception:
        log.exception("migrate_sheet_rename: failed to migrate SPILL_REGISTRY")

    if not ids:
        log.warning("migrate_sheet_rename: no workbook identity found; skipping in-memory migration")
    else:
        # Rekey SHEET_MODIFY_LISTENERS
        try:
            from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

            _rekey(SHEET_MODIFY_LISTENERS, lambda disp: setattr(disp, "sheet_name", new_sheet_name))
        except Exception:
            log.exception("migrate_sheet_rename: failed to migrate SHEET_MODIFY_LISTENERS")

        # Rekey _PENDING_TIMERS
        with _PENDING_LOCK:
            _rekey(_PENDING_TIMERS)

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


def _skip_sheet_modify() -> bool:
    from plugin.calc.python.geometric_recalc import is_geometric_repairing
    from plugin.framework.thread_guard import on_main_thread

    return (not on_main_thread()) or _DISPATCH_DEPTH > 0 or is_geometric_repairing()


def run_sheet_modify_pass(ctx: Any, doc: Any, sheet: Any, *, doc_url: str = "", sheet_name: str = "") -> None:
    """UI-thread jobs after the shared debounce. Spill and geometric stay separate.

    Spill: ``CalcSpillModifyListener.modified`` (``SPILL_REGISTRY`` walk only).
    Geometric: own ``discover_python_cells_on_sheet`` via ``reconcile_geometric_sheet``.
    """
    if _skip_sheet_modify():
        return
    if sheet is None:
        return
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    if not name:
        log.debug("run_sheet_modify_pass: unreadable sheet name; skipping (%s)", url or "?")
        return
    with sheet_modify_scope():
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


def dispatch_sheet_modified(
    ctx: Any,
    doc_key: str,
    sheet_name: str,
    event: Any,
    doc: Any = None,
) -> None:
    """Shared ``modified`` entry. Debounces; does not walk ``SPILL_REGISTRY``."""
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread() or _skip_sheet_modify():
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
    live_url = _doc_url_of(owner) if owner is not None else ""
    schedule_sheet_modify_pass(ctx, owner, sheet, doc_url=live_url or doc_key, sheet_name=effective_name)


def ensure_sheet_modify_listener(ctx: Any, doc: Any, sheet: Any) -> Any | None:
    """Register the one dispatcher on *sheet*. Idempotent; no second listener."""
    if doc is None or sheet is None:
        return None
    url = _doc_url_of(doc)
    name = _sheet_name_of(sheet)
    identity = _doc_identity(doc) or url
    key = (identity, name)
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
        from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

        # Unload may already have popped this entry. Cancel is idempotent.
        key = (self.doc_identity, self.sheet_name)
        if SHEET_MODIFY_LISTENERS.get(key) is self:
            SHEET_MODIFY_LISTENERS.pop(key, None)
        _cancel_pending(key)
