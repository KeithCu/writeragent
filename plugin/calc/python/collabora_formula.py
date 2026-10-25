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
so the existing writeragent add-in evaluates. Rewritten cells are recalculated
on open, which runs their Python once (same as the Excel-PY apply path).
Arguments are left untouched.

If Collabora later aliases ``ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY`` in
Core, this rewrite becomes unnecessary for Online→desktop; desktop→Online still
needs a Core alias (we keep **writing** writeragent tokens).

``XCompatibilityNames`` returning ``PY`` is for Excel short names, not for mapping
two UNO OriginalNames — do not expect that API to make these files interop.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from plugin.calc.python.formula_locator_cache import CELL_FLAG_FORMULA, iter_range_formula_texts
from plugin.framework.errors import best_effort, reraise_if_disposed

log = logging.getLogger(__name__)

# Core stores the service + IDL method, uppercased. Collabora's methods are the
# usual scaddins ``get*`` shape; ours are ``py`` / ``python``.
_COLLABORA_ADDIN_PREFIX = re.compile(
    r"^(\s*\{?\s*=\s*)ORG\.COLLABORAOFFICE\.SHEET\.ADDIN\.PYTHONCOMPUTEFUNCTIONS\."
    r"(GETPYTHON|GETPY)\s*\(",
    re.IGNORECASE,
)

_SHORT_NAME_BY_METHOD = {"GETPYTHON": "PYTHON", "GETPY": "PY"}


def rewrite_collabora_addin_prefix(formula: str) -> str:
    """If *formula* is a Collabora pythoncompute OriginalName, return ``=PY(`` / ``=PYTHON(``.

    ``GETPY`` → ``PY`` (same display name Collabora uses). ``GETPYTHON`` → ``PYTHON``.
    The prefix (leading whitespace/optional ``{``, ``=``, spacing) is preserved,
    the argument text is copied byte-for-byte. Non-matching formulas are returned unchanged.
    """
    if not formula:
        return formula
    match = _COLLABORA_ADDIN_PREFIX.match(formula)
    if match is None:
        return formula
    return match.group(1) + _SHORT_NAME_BY_METHOD[match.group(2).upper()] + "(" + formula[match.end() :]


def is_collabora_py_formula(formula: str) -> bool:
    """True when *formula* uses Collabora's stored OriginalName (before rewrite)."""
    if not formula:
        return False
    return _COLLABORA_ADDIN_PREFIX.match(formula) is not None


def _rewrite_sheet_formulas(sheet: Any) -> int:
    """Rewrite Collabora PY OriginalNames on *sheet*. Returns cells changed."""
    changed = 0
    try:
        formula_cells = sheet.queryContentCells(CELL_FLAG_FORMULA)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("collabora PY rewrite: queryContentCells failed", exc_info=True)
        return 0
    if formula_cells is None:
        return 0
    try:
        count = int(formula_cells.getCount())
    except Exception as exc:
        reraise_if_disposed(exc)
        return 0

    for i in range(count):
        try:
            cell_range = formula_cells.getByIndex(i)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("collabora PY rewrite: getByIndex(%s) failed", i, exc_info=True)
            continue

        try:
            for row, col, text in iter_range_formula_texts(sheet, cell_range, skip_cell_errors=True):
                new = rewrite_collabora_addin_prefix(text)
                if new == text:
                    continue
                try:
                    sheet.getCellByPosition(col, row).setFormula(new)
                    changed += 1
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("collabora PY rewrite: setFormula failed", exc_info=True)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("collabora PY rewrite: formula range scan failed", exc_info=True)
            continue
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
    # auto_open returns when doc is None and logs a raised rewrite.
    if not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
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
    # A scan failure used to be logged and swallowed here. auto_open already logs it.
    try:
        if hasattr(doc, "lockControllers"):
            doc.lockControllers()
            controllers_locked = True
        # Reuses the shared hidden/locked undo context; do not create a new undo-context module.
        from plugin.calc.python.function import _undo_lock

        with _undo_lock(doc):
            sheets = doc.getSheets()
            for i in range(int(sheets.getCount())):
                changed += _rewrite_sheet_formulas(sheets.getByIndex(i))
    finally:
        if controllers_locked:
            try:
                doc.unlockControllers()
            except Exception:
                log.debug("collabora PY rewrite: unlockControllers failed", exc_info=True)
        if changed:
            log.info("collabora PY rewrite: updated %s formula cell(s) to writeragent PY", changed)
            best_effort(lambda: doc.calculateAll(), "calculateAll after Collabora PY rewrite")
            if was_modified is not None:
                best_effort(lambda: doc.setModified(was_modified), "collabora PY rewrite: setModified restore failed")
    return changed
