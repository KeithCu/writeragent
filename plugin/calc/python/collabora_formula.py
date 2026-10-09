# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy / WriterAgent read-compat for Collabora Online ``=PY()`` files.

## Why this module exists (talking point for Collabora)

Calc does **not** store the display name ``=PY(...)``. It stores the add-in's
**OriginalName**: ``ServiceName.MethodName`` (see Core ``sc/source/core/tool/addincol.cxx``,
``pExactHashMap``). Typing ``=PY()`` looks up DisplayName; loading a file looks up
that exact token.

| Product | UNO service | IDL methods | Typical stored formula |
|---------|-------------|-------------|------------------------|
| LibrePy / WriterAgent | ``org.extension.writeragent.PythonFunction`` | ``py`` / ``python`` | ``ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY(...)`` |
| Collabora scaddins pythoncompute | ``org.collaboraoffice.sheet.addin.PythonComputeFunctions`` | ``getPy`` / ``getPython`` | ``ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY(...)`` |

A file saved in Collabora Online / Collabora Office with the C++ AddIn therefore
``#NAME?`` under LibrePy: our add-in is a different OriginalName.

We do **not** register Collabora's UNO package from the OXT (that would put a
second function in the Function Wizard, even if DisplayName were ``GETPY``, and
would collide with Collabora Office Classic if both shipped the same service).
Instead, on document open we rewrite only the **prefix** to ``=PY(`` / ``=PYTHON(``
so the existing writeragent add-in evaluates. Arguments are left untouched.

If Collabora later aliases ``ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY`` in
Core, this rewrite becomes unnecessary for Online→desktop; desktop→Online still
needs a Core alias (we keep **writing** writeragent tokens).

``XCompatibilityNames`` returning ``PY`` is for Excel short names, not for mapping
two UNO OriginalNames — do not expect that API to make these files interop.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator

from plugin.calc.python.formula_edit import normalize_formula_string

log = logging.getLogger(__name__)

# Core stores the service + IDL method, uppercased. Collabora's methods are the
# usual scaddins ``get*`` shape; ours are ``py`` / ``python``.
_COLLABORA_ADDIN_PREFIX = re.compile(
    r"^(=\s*)ORG\.COLLABORAOFFICE\.SHEET\.ADDIN\.PYTHONCOMPUTEFUNCTIONS\."
    r"(GETPYTHON|GETPY)\s*\(",
    re.IGNORECASE,
)

# com.sun.star.sheet.CellFlags.FORMULA
_CELL_FLAG_FORMULA = 16


def rewrite_collabora_addin_prefix(formula: str) -> str:
    """If *formula* is a Collabora pythoncompute OriginalName, return ``=PY(`` / ``=PYTHON(``.

    ``GETPY`` → ``PY`` (same display name Collabora uses). ``GETPYTHON`` → ``PYTHON``.
    Whitespace after ``=`` is preserved. Non-matching formulas are returned unchanged
    (after the same quote-normalize as other PY parsers).
    """
    if not formula:
        return formula
    raw = normalize_formula_string(formula)
    match = _COLLABORA_ADDIN_PREFIX.match(raw)
    if match is None:
        return formula
    equals = match.group(1)
    method = match.group(2).upper()
    short = "PYTHON" if method == "GETPYTHON" else "PY"
    return f"{equals}{short}(" + raw[match.end() :]


def is_collabora_py_formula(formula: str) -> bool:
    """True when *formula* uses Collabora's stored OriginalName (before rewrite)."""
    if not formula:
        return False
    return _COLLABORA_ADDIN_PREFIX.match(normalize_formula_string(formula)) is not None


@contextmanager
def _hidden_undo(doc: Any) -> Iterator[None]:
    """Do not put the compat rewrite on the undo stack as its own step."""
    um = None
    hidden = False
    locked = False
    try:
        raw_doc = doc
        try:
            from plugin.framework.thread_guard import _unwrap_uno

            raw_doc = _unwrap_uno(doc)
        except Exception:
            pass
        if hasattr(raw_doc, "getUndoManager"):
            um = raw_doc.getUndoManager()
            if um is not None:
                try:
                    if um.isUndoPossible():
                        um.enterHiddenUndoContext()
                        hidden = True
                    elif hasattr(um, "lock"):
                        um.lock()
                        locked = True
                except Exception:
                    try:
                        if hasattr(um, "lock"):
                            um.lock()
                            locked = True
                    except Exception:
                        pass
    except Exception:
        um = None
    try:
        yield
    finally:
        if um is not None:
            if hidden:
                try:
                    um.leaveUndoContext()
                except Exception:
                    log.debug("collabora PY rewrite: leaveUndoContext failed", exc_info=True)
            elif locked:
                try:
                    um.unlock()
                except Exception:
                    log.debug("collabora PY rewrite: UndoManager.unlock failed", exc_info=True)


def _rewrite_sheet_formulas(sheet: Any) -> int:
    """Rewrite Collabora PY OriginalNames on *sheet*. Returns cells changed."""
    changed = 0
    try:
        formula_cells = sheet.queryContentCells(_CELL_FLAG_FORMULA)
    except Exception:
        log.debug("collabora PY rewrite: queryContentCells failed", exc_info=True)
        return 0
    if formula_cells is None:
        return 0
    try:
        count = int(formula_cells.getCount())
    except Exception:
        return 0

    for i in range(count):
        try:
            cell_range = formula_cells.getByIndex(i)
            addr = cell_range.getRangeAddress()
            formula_matrix = cell_range.getFormulas() if hasattr(cell_range, "getFormulas") else None
        except Exception:
            continue

        if formula_matrix is not None and len(formula_matrix) > 0:
            for r_idx, row_formulas in enumerate(formula_matrix):
                row = addr.StartRow + r_idx
                for c_idx, formula in enumerate(row_formulas):
                    text = str(formula or "")
                    if not is_collabora_py_formula(text):
                        continue
                    new = rewrite_collabora_addin_prefix(text)
                    if new == text:
                        continue
                    try:
                        sheet.getCellByPosition(addr.StartColumn + c_idx, row).setFormula(new)
                        changed += 1
                    except Exception:
                        log.debug("collabora PY rewrite: setFormula failed", exc_info=True)
            continue

        for row in range(addr.StartRow, addr.EndRow + 1):
            for col in range(addr.StartColumn, addr.EndColumn + 1):
                try:
                    cell = sheet.getCellByPosition(col, row)
                    text = str(cell.getFormula() or "")
                except Exception:
                    continue
                if not is_collabora_py_formula(text):
                    continue
                new = rewrite_collabora_addin_prefix(text)
                if new == text:
                    continue
                try:
                    cell.setFormula(new)
                    changed += 1
                except Exception:
                    log.debug("collabora PY rewrite: setFormula failed", exc_info=True)
    return changed


def maybe_rewrite_collabora_py_formulas(doc: Any) -> int:
    """In-place prefix rewrite so Collabora-saved ``=PY()`` cells run under LibrePy.

    Called from the existing Excel-PY ``OnLoadFinished`` listener (no extra
    GlobalEventBroadcaster). Returns the number of cells rewritten.

    After ``setFormula``, Calc usually marks the document modified. Snapshot
    ``isModified`` before the rewrite and write that flag back: a clean file
    stays clean, and a file the user had already edited stays dirty. **Save**
    still persists writeragent tokens (Collabora Online would then need a
    writeragent alias to evaluate those files).
    """
    if doc is None:
        return 0
    try:
        if not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
            return 0
    except Exception:
        return 0

    from plugin.framework.thread_guard import guard_uno

    doc = guard_uno(doc)
    # setFormula flips the dirty flag itself. Restore the value from before
    # the scan so a user edit stays dirty. setModified(False) after the
    # rewrite clears that flag.
    was_modified: bool | None = None
    try:
        if hasattr(doc, "isModified"):
            was_modified = bool(doc.isModified())
    except Exception:
        log.debug("collabora PY rewrite: isModified failed", exc_info=True)
    changed = 0
    controllers_locked = False
    try:
        if hasattr(doc, "lockControllers"):
            doc.lockControllers()
            controllers_locked = True
        with _hidden_undo(doc):
            sheets = doc.getSheets()
            for i in range(int(sheets.getCount())):
                changed += _rewrite_sheet_formulas(sheets.getByIndex(i))
    except Exception:
        log.warning("collabora PY rewrite: document scan failed", exc_info=True)
        return changed
    finally:
        if controllers_locked:
            try:
                doc.unlockControllers()
            except Exception:
                log.debug("collabora PY rewrite: unlockControllers failed", exc_info=True)

    if changed:
        log.info("collabora PY rewrite: updated %s formula cell(s) to writeragent PY", changed)
        if was_modified is not None:
            try:
                if hasattr(doc, "setModified"):
                    doc.setModified(was_modified)
            except Exception:
                log.debug("collabora PY rewrite: setModified restore failed", exc_info=True)
    return changed
