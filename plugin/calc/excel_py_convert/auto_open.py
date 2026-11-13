# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel Python-in-Excel ↔ DAG ``=PY`` automatic round-trip on open/save.

``plugin.calc.python.workbook_lifecycle``'s global document-event listener calls
``maybe_convert_excel_py_document`` on open and ``maybe_export_excel_py_on_save``
on save:

* **Open** (``OnLoadFinished``): if the ``.xlsx`` on disk still has
  ``pythonScripts.xml`` / ``_xlws.PY``, rewrite in-place to DAG ``=PY`` via UNO.
* **Save** (``OnSaveDone`` / ``OnSaveAsDone`` / ``OnSaveToDone``): if the open
  Calc doc has DAG ``=PY`` cells and the destination is ``.xlsx``, snapshot
  formulas from memory (UNO) and ZipFile-patch the saved file to native Excel
  PY (``pythonScripts.xml`` + ``_xlws.PY``). Failures show a MessageBox.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import threading
import zipfile
from pathlib import Path
from typing import Any

from plugin.calc.python.workbook_lifecycle import is_calc_document
from plugin.doc.text_helpers import get_document_path
from plugin.framework.errors import best_effort
from plugin.framework.i18n import _
from plugin.framework.uno_context import product_display_name

log = logging.getLogger(__name__)

# No "converted" udprop: LibreOffice persists UserDefinedProperties into the saved
# .xlsx, so a reopened native-PY workbook would skip conversion forever. Peek the
# file instead; _busy_path guards same-process reentry.

_lock = threading.Lock()
_busy_paths: set[str] = set()


def _calc_xlsx_path(doc: Any) -> Path | None:
    if not is_calc_document(doc):
        return None
    path_str = get_document_path(doc)
    if not path_str:
        return None
    path = Path(path_str)
    if path.suffix.lower() != ".xlsx":
        return None
    return path


def has_excel_python_xlsx(path: str | Path) -> bool:
    """True when *path* is an ``.xlsx`` with ``pythonScripts`` and/or ``_xlws.PY`` cells.

    Peeks the ZIP on disk (stock Calc import may have dropped these parts from the
    in-memory model). Safe to call on any path; returns False on I/O/parse errors.
    """
    path = Path(path)
    if path.suffix.lower() != ".xlsx" or not path.is_file():
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            if any(n == "xl/pythonScripts.xml" or n.startswith("xl/pythonScripts/") for n in names):
                return True
            for name in names:
                if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                    continue
                data = zf.read(name)
                if b"_xlws.PY" in data or b"_xlws.py" in data:
                    return True
    except Exception:
        return False
    return False


@contextmanager
def _busy_path(path_str: str):
    with _lock:
        busy = path_str in _busy_paths
        if not busy:
            _busy_paths.add(path_str)
    if busy:
        yield False
        return
    try:
        yield True
    finally:
        with _lock:
            _busy_paths.discard(path_str)


def _issue_summary(report: Any) -> str:
    """Semicolon-joined first few issues (cell issues when the report has none)."""
    issues = list(report.issues) or [f"{c.sheet}!{c.cell}: {', '.join(c.issues)}" for c in report.cells if c.issues]
    shown = "; ".join(issues[:5])
    extra = len(issues) - 5
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def maybe_convert_excel_py_document(doc: Any) -> bool:
    """If *doc* is an Excel-PY ``.xlsx``, convert to DAG ``=PY``. Return True if converted."""
    path = _calc_xlsx_path(doc)
    if path is None:
        return False

    if not has_excel_python_xlsx(path):
        return False

    with _busy_path(str(path)) as acquired:
        if not acquired:
            return False
        try:
            # The converter loads only once the file on disk is an Excel-Python workbook.
            from plugin.calc.excel_py_convert.apply_calc import apply_dag_formulas_to_calc_doc
            from plugin.calc.excel_py_convert.convert import convert_to_dag, store_dag_meta_on_doc

            report = convert_to_dag(path)
            if not report.ok:
                log.warning("excel_py auto-open: fail-closed (leaving original open) for %s: %s", path, _issue_summary(report))
                return False
            if not report.has_conversions:
                log.info("excel_py auto-open: no convertible PY cells in %s", path)
                return False

            modified_before = best_effort(doc.isModified, "excel_py auto-open: read modified flag", default=True)
            errors = apply_dag_formulas_to_calc_doc(doc, report)
            if errors:
                log.warning("excel_py auto-open: UNO apply failed (leaving formulas as imported) for %s: %s", path, "; ".join(errors))
                return False

            try:
                store_dag_meta_on_doc(doc, report)
            except Exception:
                log.warning("excel_py auto-open: failed to store ExcelPyDagMeta for %s", path, exc_info=True)
            # setFormula dirties the doc; a pristine open must not prompt "Save changes?".
            best_effort(lambda: doc.setModified(modified_before), "excel_py auto-open: restore modified flag")
            if any(c.issues for c in report.cells):
                log.info("excel_py auto-open: %s", _issue_summary(report))
            log.info("excel_py auto-open: applied DAG =PY in place for %s (py_code_* sheets)", path)
            return True
        except Exception:
            log.warning("excel_py auto-open: conversion failed for %s", path, exc_info=True)
            return False


def _show_save_failure(ctx: Any, path: Path, detail: str) -> None:
    # Cross-app (chat UI), failure path only.
    from plugin.chatbot.dialogs import msgbox

    msg = _(
        "Could not write Microsoft Excel Python package parts for:\n{0}\n\n"
        "The file was saved with LibreOffice =PY formulas. Excel will not see pythonScripts.xml / _xlws.PY until export succeeds.\n\n{1}"
    ).format(path, detail)
    msgbox(ctx, product_display_name(ctx), msg, box_type=2)


def maybe_export_excel_py_on_save(ctx: Any, doc: Any) -> bool:
    """After LO stores a Calc ``.xlsx``, rewrite disk to native Excel PY. Return True if patched."""
    path = _calc_xlsx_path(doc)
    if path is None or not path.is_file():
        return False

    with _busy_path(str(path)) as acquired:
        if not acquired:
            return False
        try:
            # The snapshot and writer load only when a Calc document is saved as .xlsx.
            from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel
            from plugin.calc.excel_py_convert.xlsx_write import write_excel_python_xlsx

            report = convert_uno_doc_to_excel(doc)
            if not report.ok:
                detail = _issue_summary(report) or "snapshot report not ok"
                log.warning("excel_py auto-save: report not ok for %s: %s", path, detail)
                _show_save_failure(ctx, path, detail)
                return False
            if not report.has_conversions:
                # No DAG =PY in memory — leave the saved file alone (plain spreadsheet).
                return False

            try:
                write_excel_python_xlsx(path, report, path)
            except Exception as exc:
                detail = str(exc)
                log.warning("excel_py auto-save: ZipFile export failed for %s: %s", path, detail, exc_info=True)
                _show_save_failure(ctx, path, detail)
                return False

            if not has_excel_python_xlsx(path):
                detail = "pythonScripts.xml / _xlws.PY missing after write"
                log.warning("excel_py auto-save: %s for %s", detail, path)
                _show_save_failure(ctx, path, detail)
                return False

            log.info("excel_py auto-save: wrote native Excel PY package for %s", path)
            return True
        except Exception as exc:
            log.warning("excel_py auto-save: failed for %s", path, exc_info=True)
            detail = str(exc)
            best_effort(lambda: _show_save_failure(ctx, path, detail), "excel_py auto-save error dialog")
            return False
