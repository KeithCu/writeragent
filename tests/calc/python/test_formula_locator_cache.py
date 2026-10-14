# WriterAgent - Tests for formula locator and array-formula origin detection
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.calc.python.formula_locator_cache import (
    extract_code_from_py_formula,
    is_matching_py_formula,
    locate_formula_cell,
    locate_formula_cell_in_doc,
)
from plugin.tests.testing_utils import CalcDocStub, CalcSheetStub


def _ctx_with_doc(doc: CalcDocStub):
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    return SimpleNamespace(ServiceManager=smgr)


def test_is_matching_py_formula_variants():
    assert is_matching_py_formula('=PY("plt.plot([1, 2])")', "plt.plot([1, 2])")
    assert is_matching_py_formula('=PYTHON("x = ""hello""")', 'x = "hello"')
    assert is_matching_py_formula(
        '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("plt.figure(); plt.show()")',
        "plt.figure(); plt.show()",
    )
    assert not is_matching_py_formula('=SUM(A1:A10)', "plt.plot()")
    assert not is_matching_py_formula('', "plt.plot()")
    assert not is_matching_py_formula('=PY("plt.plot([1, 2])")', "")
    # Shared 30-char prefix must not match a different script.
    code_a = "plt.figure(); plt.plot([1, 2, 3]); plt.title('Sales')"
    code_b = "plt.figure(); plt.plot([1, 2, 3]); plt.title('Costs')"
    assert code_a[:30] == code_b[:30]
    assert not is_matching_py_formula(f'=PY("{code_a}")', code_b)


def test_extract_code_from_py_formula():
    assert extract_code_from_py_formula('=PY("plt.plot([1, 2])")') == "plt.plot([1, 2])"
    assert extract_code_from_py_formula('=PYTHON("x = ""hello""", A1:B10)') == 'x = "hello"'
    assert (
        extract_code_from_py_formula(
            '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("import matplotlib.pyplot as plt\nplt.show()")'
        )
        == "import matplotlib.pyplot as plt\nplt.show()"
    )
    assert (
        extract_code_from_py_formula(
            '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("result = 1")'
        )
        == "result = 1"
    )
    assert extract_code_from_py_formula("=SUM(A1:A10)") is None
    assert extract_code_from_py_formula("") is None
    assert extract_code_from_py_formula('XPY("hello")') is None
    assert extract_code_from_py_formula('=CONCAT("PY(""x"")")') is None
    assert extract_code_from_py_formula('{=PY("result = 1")}') == "result = 1"
    assert extract_code_from_py_formula('=PY("x")+1') == "x"


def test_locate_formula_cell_convenience_helper():
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///test.ods")
    ctx = _ctx_with_doc(doc)

    code = "result = 99"
    sheet.getCellByPosition(1, 2).setFormula(f'=PYTHON("{code}")')

    coords = locate_formula_cell(ctx, sheet, code)
    assert coords == (2, 1)


def test_locate_formula_cell_in_doc_secondary_sheet_when_active_sheet_differs():
    """Formula cell on secondary sheet (viz) is located even when controller active sheet is analysis."""
    sheet_analysis = CalcSheetStub("analysis")
    sheet_viz = CalcSheetStub("viz")
    doc = CalcDocStub(sheets=[sheet_analysis, sheet_viz], url="file:///test_demo.ods", active_sheet="analysis")
    ctx = _ctx_with_doc(doc)

    code = "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.plot([3, 2, 1])"
    # Formula is on viz sheet at B2 (col=1, row=1)
    sheet_viz.getCellByPosition(1, 1).setFormula(
        f'=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON("{code}")'
    )

    located = locate_formula_cell_in_doc(ctx, doc, code)
    assert located is not None
    found_sheet, found_cell, (r, c) = located
    assert found_sheet.getName() == "viz"
    assert (r, c) == (1, 1)


def test_untitled_docs_locate_independently():
    """Empty getURL() must not collide; each doc locates formulas independently."""
    code = "plt.plot([1])"
    doc1 = CalcDocStub(url="", props={"RuntimeUID": "uid-untitled-1"})
    doc2 = CalcDocStub(url="", props={"RuntimeUID": "uid-untitled-2"})
    ctx1 = _ctx_with_doc(doc1)
    ctx2 = _ctx_with_doc(doc2)

    doc1.getSheets().getByIndex(0).getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    doc2.getSheets().getByIndex(0).getCellByPosition(5, 5).setFormula(f'=PY("{code}")')

    located1 = locate_formula_cell_in_doc(ctx1, doc1, code)
    located2 = locate_formula_cell_in_doc(ctx2, doc2, code)
    assert located1 is not None and located1[2] == (0, 0)
    assert located2 is not None and located2[2] == (5, 5)


def test_locate_duplicate_py_formulas_is_ambiguous():
    """Two gapped cells with the same code are not a unique origin (no calling cell on XAddIn)."""
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///dup.ods")
    ctx = _ctx_with_doc(doc)
    code = "result = [1, 2]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    sheet.getCellByPosition(0, 5).setFormula(f'=PY("{code}")')
    assert locate_formula_cell_in_doc(ctx, doc, code) is None


def test_locate_fill_down_duplicate_is_ambiguous():
    """Contiguous fill-down A1:A3 with the same code must NOT collapse to top-left origin."""
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///filldown.ods")
    ctx = _ctx_with_doc(doc)
    code = "result = [1, 2]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    sheet.getCellByPosition(0, 1).setFormula(f'=PY("{code}")')
    sheet.getCellByPosition(0, 2).setFormula(f'=PY("{code}")')

    assert locate_formula_cell_in_doc(ctx, doc, code) is None


def test_locate_array_formula_returns_single_top_left_origin():
    """A multi-cell array formula spanning A1:B2 returns one origin at top-left (0, 0)."""
    code = "result = [[1, 2], [3, 4]]"
    formula = f'=PY("{code}")'

    class _Cursor:
        def collapseToCurrentArray(self):
            pass

        def getRangeAddress(self):
            return SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=1, EndRow=1)

    class _ArrayRange:
        def getArrayFormula(self) -> str:
            return formula

        def getRangeAddress(self):
            return SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=1, EndRow=1)

        def getFormulaArray(self):
            return (
                (formula, formula),
                (formula, formula),
            )

    class _ArraySheet:
        def getName(self):
            return "Sheet1"

        def createCursorByRange(self, _rng):
            return _Cursor()

        def getCellRangeByPosition(self, c1, r1, c2, r2):
            return _ArrayRange()

        def getCellByPosition(self, col, row):
            cell = MagicMock()
            cell.getFormula.return_value = formula
            return cell

        def queryContentCells(self, _flags):
            return SimpleNamespace(
                getCount=lambda: 1,
                getByIndex=lambda _i: _ArrayRange(),
            )

    doc = MagicMock()
    doc.getSheets.return_value = SimpleNamespace(
        getCount=lambda: 1,
        getByIndex=lambda _i: _ArraySheet(),
    )
    ctx = MagicMock()

    located = locate_formula_cell_in_doc(ctx, doc, code)
    assert located is not None
    assert located[2] == (0, 0)


def test_locate_uses_get_formula_array_and_skips_per_cell_get_formula():
    """Locate calls getFormulaArray once per range and avoids per-cell getFormula calls."""
    code = "result = 42"
    formula = f'=PY("{code}")'
    get_formula_array_calls = 0
    get_formula_calls = 0

    class _TrackingCell:
        def __init__(self, c, r):
            self.col = c
            self.row = r

        def getFormula(self):
            nonlocal get_formula_calls
            get_formula_calls += 1
            return formula if (self.col == 1 and self.row == 1) else ""

    class _TrackingRange:
        def getRangeAddress(self):
            return SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=2, EndRow=2)

        def getFormulaArray(self):
            nonlocal get_formula_array_calls
            get_formula_array_calls += 1
            # 3x3 grid, only (1, 1) matches
            return (
                ("", "", ""),
                ("", formula, ""),
                ("", "", ""),
            )

    class _TrackingSheet:
        def getName(self):
            return "Sheet1"

        def createCursorByRange(self, _rng):
            cursor = MagicMock()
            # Ordinary cell collapses to itself
            cursor.getRangeAddress.return_value = SimpleNamespace(
                StartColumn=1, StartRow=1, EndColumn=1, EndRow=1
            )
            return cursor

        def getCellRangeByPosition(self, c1, r1, c2, r2):
            return _TrackingRange()

        def getCellByPosition(self, col, row):
            return _TrackingCell(col, row)

        def queryContentCells(self, _flags):
            return SimpleNamespace(
                getCount=lambda: 1,
                getByIndex=lambda _i: _TrackingRange(),
            )

    doc = MagicMock()
    doc.getSheets.return_value = SimpleNamespace(
        getCount=lambda: 1,
        getByIndex=lambda _i: _TrackingSheet(),
    )
    ctx = MagicMock()

    located = locate_formula_cell_in_doc(ctx, doc, code)
    assert located is not None
    assert located[2] == (1, 1)
    assert get_formula_array_calls == 1
    assert get_formula_calls == 0
