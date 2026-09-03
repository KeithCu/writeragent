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
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

import unohelper
from com.sun.star.util import XModifyListener

log = logging.getLogger(__name__)

# Debounce one pass per sheet. Keyed by (doc_url, sheet_name).
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


def _sheet_key(doc_url: str, sheet_name: str) -> tuple[str, str]:
    return (doc_url, sheet_name)


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


def schedule_sheet_modify_pass(
    ctx: Any,
    doc: Any,
    sheet: Any,
    *,
    doc_url: str = "",
    sheet_name: str = "",
    delay_sec: float = _MODIFY_DELAY_SEC,
) -> None:
    """Debounce: cancel the sheet's pending timer, start a new 0.1s UI-thread pass.

    Same shape as ``perform_deferred_spill`` (Timer → ``post_to_main_thread``).
    Unload cancels via the shared spill-timer registry.
    """
    if sheet is None:
        return
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    key = _sheet_key(url, name)
    _cancel_pending(key)

    def _fire() -> None:
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(
            lambda: run_sheet_modify_pass(
                ctx, doc, sheet, doc_url=url, sheet_name=name
            )
        )

    lifecycle_key = ""
    if doc is not None:
        try:
            from plugin.calc.python.workbook_lifecycle import _lifecycle_key

            lifecycle_key = _lifecycle_key(doc)
        except Exception:
            log.debug("sheet_modify: lifecycle key failed", exc_info=True)
    from plugin.calc.python.function import start_deferred_sheet_timer

    # Timer lives in function.py (Layer C allowlist) — same 0.1s spill site.
    timer = start_deferred_sheet_timer(
        delay_sec, _fire, lifecycle_key=lifecycle_key
    )
    with _PENDING_LOCK:
        _PENDING_TIMERS[key] = timer


def flush_sheet_modify_pass_for_tests(
    ctx: Any,
    doc: Any,
    sheet: Any,
    *,
    doc_url: str = "",
    sheet_name: str = "",
) -> None:
    """Cancel debounce and run the pass on this thread. Tests only."""
    url = doc_url or _doc_url_of(doc)
    name = sheet_name or _sheet_name_of(sheet)
    _cancel_pending(_sheet_key(url, name))
    run_sheet_modify_pass(ctx, doc, sheet, doc_url=url, sheet_name=name)


def run_sheet_modify_pass(
    ctx: Any,
    doc: Any,
    sheet: Any,
    *,
    doc_url: str = "",
    sheet_name: str = "",
) -> None:
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
        from plugin.calc.python.function import CalcSpillModifyListener

        # Job 1 — spill orphan cleanup. Walks SPILL_REGISTRY only.
        CalcSpillModifyListener(ctx, url, name).modified(SimpleNamespace(Source=sheet))

        # Job 2 — geometric list-diff. Own discovery; skip when flag is off.
        from plugin.calc.python.geometric_recalc import (
            geometric_flag_enabled,
            reconcile_geometric_sheet,
        )

        if geometric_flag_enabled() and doc is not None:
            reconcile_geometric_sheet(ctx, doc, sheet)
    except Exception:
        log.exception("Error in sheet modify pass (%s)", name)
    finally:
        _DISPATCHING = False


def dispatch_sheet_modified(
    ctx: Any,
    doc_url: str,
    sheet_name: str,
    event: Any,
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
    from plugin.calc.python.function import _get_calc_doc

    doc = _get_calc_doc(ctx)
    schedule_sheet_modify_pass(
        ctx, doc, sheet, doc_url=doc_url, sheet_name=sheet_name
    )


def ensure_sheet_modify_listener(ctx: Any, doc: Any, sheet: Any) -> Any | None:
    """Register the one dispatcher on *sheet*. Idempotent; no second listener."""
    if doc is None or sheet is None:
        return None
    url = _doc_url_of(doc)
    name = _sheet_name_of(sheet)
    key = _sheet_key(url, name)
    from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

    existing = SHEET_MODIFY_LISTENERS.get(key)
    if existing is not None:
        return existing
    listener = SheetModifyDispatcher(ctx, url, name)
    try:
        sheet.addModifyListener(listener)
    except Exception:
        log.exception("Failed to register sheet modify dispatcher on %s", name)
        return None
    SHEET_MODIFY_LISTENERS[key] = listener
    return listener


class SheetModifyDispatcher(unohelper.Base, XModifyListener):
    """One ``XModifyListener`` per sheet. Schedules; does not own either job."""

    def __init__(self, ctx: Any, doc_url: str, sheet_name: str) -> None:
        self.ctx = ctx
        self.doc_url = doc_url
        self.sheet_name = sheet_name

    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            dispatch_sheet_modified(self.ctx, self.doc_url, self.sheet_name, aEvent)
        except Exception:
            log.exception("Error in SheetModifyDispatcher.modified")

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        from plugin.calc.python.function import SHEET_MODIFY_LISTENERS

        SHEET_MODIFY_LISTENERS.pop((self.doc_url, self.sheet_name), None)
        _cancel_pending((self.doc_url, self.sheet_name))
