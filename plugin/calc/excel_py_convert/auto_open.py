# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatically rewrite Excel Python-in-Excel workbooks to DAG ``=PY`` on open.

No menu: a GlobalEventBroadcaster listener runs on Calc ``OnLoadFinished``.
Detection re-reads the ``.xlsx`` on disk (stock import often drops
``pythonScripts.xml``). Prefer writing a sibling ``*_py_dag.xlsx`` via openpyxl
and swapping documents; fall back to in-place UNO ``setFormula`` when openpyxl
is missing on the host. Failures leave the original document open.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Document udprop set after in-place UNO apply so OnViewCreated does not re-run.
_CONVERTED_PROP = "ExcelPyDagConverted"

_lock = threading.Lock()
_doc_listener: Any = None
_busy_paths: set[str] = set()


def _is_calc_doc(doc: Any) -> bool:
    try:
        return bool(doc and doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"))
    except Exception:
        return False


def _out_path_for(source: Path) -> Path:
    return source.with_name(f"{source.stem}_py_dag.xlsx")


def _swap_to_converted(ctx: Any, original_doc: Any, converted_path: Path) -> bool:
    """Close *original_doc* without saving and open *converted_path* visibly."""
    import uno

    from plugin.framework.uno_context import get_desktop

    desktop = get_desktop(ctx)
    if desktop is None:
        return False
    url = uno.systemPathToFileUrl(str(converted_path.resolve()))
    try:
        if hasattr(original_doc, "setModified"):
            original_doc.setModified(False)
        original_doc.close(True)
    except Exception:
        log.warning("excel_py auto-open: failed to close original after convert", exc_info=True)
        return False
    try:
        loaded = desktop.loadComponentFromURL(url, "_blank", 0, ())
        return loaded is not None
    except Exception:
        log.warning("excel_py auto-open: failed to open converted workbook %s", converted_path, exc_info=True)
        return False


def maybe_convert_excel_py_document(ctx: Any, doc: Any) -> bool:
    """If *doc* is an Excel-PY ``.xlsx``, convert to DAG ``=PY``. Return True if converted."""
    if not _is_calc_doc(doc):
        return False

    from plugin.doc.document_helpers import get_document_path
    from plugin.doc.udprops import get_document_property, set_document_property

    if get_document_property(doc, _CONVERTED_PROP):
        return False

    path_str = get_document_path(doc)
    if not path_str:
        return False
    path = Path(path_str)
    if path.suffix.lower() != ".xlsx":
        return False

    from plugin.calc.excel_py_convert.parse_excel_ooxml import has_excel_python_xlsx

    if not has_excel_python_xlsx(path):
        return False

    with _lock:
        if path_str in _busy_paths:
            return False
        _busy_paths.add(path_str)

    try:
        from plugin.calc.excel_py_convert.convert import convert_to_dag, write_dag_formulas_xlsx

        report = convert_to_dag(path)
        if not any(c.converted for c in report.cells):
            log.info("excel_py auto-open: no convertible PY cells in %s", path)
            return False
        if not report.ok:
            log.warning(
                "excel_py auto-open: fail-closed (leaving original open) for %s: %s",
                path,
                "; ".join(report.issues)
                or "; ".join(f"{c.sheet}!{c.cell}: {', '.join(c.issues)}" for c in report.cells if c.issues),
            )
            return False

        out = _out_path_for(path)
        try:
            write_dag_formulas_xlsx(path, report, out)
            if _swap_to_converted(ctx, doc, out):
                log.info("excel_py auto-open: converted %s → %s (per-sheet py_code_* banks)", path, out)
                return True
            log.warning("excel_py auto-open: wrote %s but could not swap documents", out)
        except ImportError:
            log.debug("excel_py auto-open: openpyxl unavailable; applying formulas via UNO")
        except Exception:
            log.warning("excel_py auto-open: xlsx rewrite failed; trying UNO apply", exc_info=True)

        from plugin.calc.excel_py_convert.apply_calc import apply_dag_formulas_to_calc_doc

        errors = apply_dag_formulas_to_calc_doc(doc, report)
        if errors:
            log.warning(
                "excel_py auto-open: UNO apply failed (leaving formulas as imported) for %s: %s",
                path,
                "; ".join(errors),
            )
            return False
        set_document_property(doc, _CONVERTED_PROP, "1")
        try:
            if hasattr(doc, "setModified"):
                doc.setModified(True)
        except Exception:
            pass
        log.info("excel_py auto-open: applied DAG =PY in place for %s (py_code_* sheets)", path)
        return True
    except Exception:
        log.warning("excel_py auto-open: conversion failed for %s", path, exc_info=True)
        return False
    finally:
        with _lock:
            _busy_paths.discard(path_str)


def _doc_from_event(event: Any) -> Any | None:
    controller = getattr(event, "ViewController", None)
    if controller is not None and hasattr(controller, "getModel"):
        try:
            return controller.getModel()
        except Exception:
            pass
    source = getattr(event, "Source", None)
    if source is not None and hasattr(source, "getCurrentController"):
        try:
            ctrl = source.getCurrentController()
            if ctrl is not None and hasattr(ctrl, "getModel"):
                return ctrl.getModel()
        except Exception:
            pass
    if source is not None and _is_calc_doc(source):
        return source
    return None


def install_excel_py_auto_convert(ctx: Any) -> None:
    """Attach a global listener so Excel-PY ``.xlsx`` files convert on open."""
    global _doc_listener
    with _lock:
        if _doc_listener is not None:
            return
    try:
        from plugin.framework.uno_listeners import BaseDocumentEventListener

        class _ExcelPyOpenListener(BaseDocumentEventListener):  # type: ignore[misc, valid-type]
            def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
                try:
                    name = getattr(Event, "EventName", "") or ""
                    # OnLoadFinished is enough; OnLoad/OnViewCreated can race mid-import.
                    if name != "OnLoadFinished":
                        return
                    doc = _doc_from_event(Event)
                    if doc is None:
                        return
                    maybe_convert_excel_py_document(ctx, doc)
                except Exception:
                    log.warning("excel_py auto-open: doc-event handling failed", exc_info=True)

        smgr = ctx.getServiceManager()
        broadcaster = smgr.createInstanceWithContext("com.sun.star.frame.GlobalEventBroadcaster", ctx)
        listener = _ExcelPyOpenListener()
        broadcaster.addDocumentEventListener(listener)
        with _lock:
            _doc_listener = listener
        log.debug("excel_py auto-open: global OnLoadFinished listener attached")
    except Exception:
        log.warning("excel_py auto-open: listener install failed", exc_info=True)
