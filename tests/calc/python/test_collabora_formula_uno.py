# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO: Collabora GETPY OriginalName rewrites to =PY() and evaluates."""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_collabora_getpy_rewrites_and_evaluates(ctx, doc):
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    sheet = doc.getSheets().getByIndex(0)
    cell = sheet.getCellByPosition(0, 0)
    cell.setFormula(
        '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("result = 1 + 1")'
    )
    changed = maybe_rewrite_collabora_py_formulas(doc)
    assert changed >= 1
    formula = str(cell.getFormula() or "").upper()
    assert "COLLABORAOFFICE" not in formula
    assert formula.startswith("=PY(") or "PYTHONFUNCTION.PY" in formula
    # The rewrite recalculates so the cell value is updated on open.
    value = cell.getValue()

    # Query LibreOffice ServiceManager for the Calc add-in UNO service
    try:
        smgr = ctx.getServiceManager()
        has_addin = smgr.createInstanceWithContext("org.extension.writeragent.PythonFunction", ctx) is not None
    except Exception:
        has_addin = False

    if not has_addin:
        import unittest

        raise unittest.SkipTest(
            "org.extension.writeragent.PythonFunction addin service not registered in throwaway profile"
        )

    assert float(value) == 2.0
