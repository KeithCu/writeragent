# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Collabora pythoncompute OriginalName → LibrePy =PY() prefix rewrite."""

from __future__ import annotations

from plugin.calc.python.cell_discovery import canonicalize_py_formula_for_parse, is_py_formula_text
from plugin.calc.python.collabora_formula import (
    is_collabora_py_formula,
    rewrite_collabora_addin_prefix,
)
from plugin.calc.python.formula_edit import parse_python_formula
from plugin.framework.errors import DocumentDisposedError
import pytest


_GETPY = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("result = 1"; A1:A2)'
_GETPYTHON = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPYTHON("result = 2")'


def test_rewrite_getpy_to_py():
    out = rewrite_collabora_addin_prefix(_GETPY)
    assert out.startswith("=PY(")
    assert 'result = 1' in out
    assert "A1:A2" in out
    assert "COLLABORAOFFICE" not in out.upper()


def test_rewrite_getpython_to_python():
    out = rewrite_collabora_addin_prefix(_GETPYTHON)
    assert out.startswith("=PYTHON(")
    assert "result = 2" in out


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param('=org.collaboraoffice.sheet.addin.PythonComputeFunctions.getPy("x")', "=PY(", id="test_rewrite_is_case_insensitive"),
        pytest.param('= ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("x")', "= PY(", id="test_rewrite_preserves_space_after_equals"),
    ],
)
def test_rewrite_is_case_insensitive(value, value_2):
    raw = value
    assert rewrite_collabora_addin_prefix(raw).startswith(value_2)

def test_rewrite_leaves_writeragent_and_short_py_alone():
    wa = '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("result = 1")'
    assert rewrite_collabora_addin_prefix(wa) == wa
    short = '=PY("result = 1")'
    assert rewrite_collabora_addin_prefix(short) == short
    assert rewrite_collabora_addin_prefix("=SUM(A1:A2)") == "=SUM(A1:A2)"


def test_is_collabora_py_formula():
    assert is_collabora_py_formula(_GETPY)
    assert is_collabora_py_formula(_GETPYTHON)
    assert not is_collabora_py_formula('=PY("result = 1")')
    assert not is_collabora_py_formula('=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("x")')


def test_rewrite_preserves_quotes_and_brackets():
    curly = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("s = “hi”")'
    assert rewrite_collabora_addin_prefix(curly) == '=PY("s = “hi”")'

    doubled = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("x = ""a""")'
    assert rewrite_collabora_addin_prefix(doubled) == '=PY("x = ""a""")'

    wrapped = '  {=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("x")}'
    assert rewrite_collabora_addin_prefix(wrapped) == '  {=PY("x")}'


def test_is_collabora_py_formula_leading_whitespace_and_bracket():
    assert is_collabora_py_formula('  {=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("x")}')
    assert is_collabora_py_formula(' =ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPYTHON("x")')
    assert not is_collabora_py_formula('  {=PY("x")}')
    assert not is_collabora_py_formula('=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("x")')
    assert not is_collabora_py_formula("=SUM(A1:A2)")


def test_canonicalize_and_parse_collabora_getpy():
    canonical = canonicalize_py_formula_for_parse(_GETPY)
    assert canonical.upper().startswith("=PY(")
    assert is_py_formula_text(_GETPY)
    parts = parse_python_formula(canonical)
    assert parts is not None
    assert parts.code == "result = 1"


def test_maybe_rewrite_skips_non_calc():
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    class _Writer:
        def supportsService(self, name: str) -> bool:
            return name != "com.sun.star.sheet.SpreadsheetDocument"

    assert maybe_rewrite_collabora_py_formulas(_Writer()) == 0


def test_maybe_rewrite_none_doc():
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    assert maybe_rewrite_collabora_py_formulas(None) == 0


def test_maybe_rewrite_restores_is_modified():
    """setFormula dirties the book; restore the flag from before the scan."""
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    class _Addr:
        StartRow = 0
        StartColumn = 0
        EndRow = 0
        EndColumn = 0

    class _Doc:
        def __init__(self, formula: str, modified: bool) -> None:
            self.formula = formula
            self.modified = modified
            self.sets: list[bool] = []
            self.calculated = 0

        def supportsService(self, name: str) -> bool:
            return name == "com.sun.star.sheet.SpreadsheetDocument"

        def isModified(self) -> bool:
            return self.modified

        def setModified(self, value: bool) -> None:
            self.sets.append(bool(value))
            self.modified = bool(value)

        def calculateAll(self) -> None:
            self.calculated += 1

        def getSheets(self):
            doc = self

            class _Range:
                def getRangeAddress(self):
                    return _Addr()

                def getFormulaArray(self):
                    return ((doc.formula,),)

            class _Cells:
                def getCount(self):
                    return 1

                def getByIndex(self, _index):
                    return _Range()

            class _Sheet:
                def queryContentCells(self, _flags):
                    return _Cells()

                def getCellByPosition(self, _col, _row):
                    class _Cell:
                        def setFormula(self, value: str) -> None:
                            doc.formula = value

                    return _Cell()

            class _Sheets:
                def getCount(self):
                    return 1

                def getByIndex(self, _index):
                    return _Sheet()

            return _Sheets()

    dirty = _Doc(_GETPY, True)
    assert maybe_rewrite_collabora_py_formulas(dirty) == 1
    assert dirty.formula.startswith("=PY(")
    assert dirty.sets == [True]
    assert dirty.calculated == 1

    clean = _Doc(_GETPY, False)
    assert maybe_rewrite_collabora_py_formulas(clean) == 1
    assert clean.sets == [False]
    assert clean.calculated == 1

    untouched = _Doc("=SUM(A1:A2)", True)
    assert maybe_rewrite_collabora_py_formulas(untouched) == 0
    assert untouched.sets == []
    assert untouched.calculated == 0


def test_maybe_rewrite_partial_failure_restores_is_modified():
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    class _Addr:
        StartRow = 0
        StartColumn = 0
        EndRow = 0
        EndColumn = 0

    class _Doc:
        def __init__(self) -> None:
            self.formula = _GETPY
            self.modified = False
            self.sets: list[bool] = []
            self.calculated = 0

        def supportsService(self, name: str) -> bool:
            return name == "com.sun.star.sheet.SpreadsheetDocument"

        def isModified(self) -> bool:
            return self.modified

        def setModified(self, value: bool) -> None:
            self.sets.append(bool(value))
            self.modified = bool(value)

        def calculateAll(self) -> None:
            self.calculated += 1

        def getSheets(self):
            doc = self

            class _Range:
                def getRangeAddress(self):
                    return _Addr()

                def getFormulaArray(self):
                    return ((doc.formula,),)

            class _Cells:
                def getCount(self):
                    return 1

                def getByIndex(self, _index):
                    return _Range()

            class _Sheet0:
                def queryContentCells(self, _flags):
                    return _Cells()

                def getCellByPosition(self, _col, _row):
                    class _Cell:
                        def setFormula(self, value: str) -> None:
                            doc.formula = value

                    return _Cell()

            class _Sheets:
                def getCount(self):
                    return 2

                def getByIndex(self, index):
                    if index == 0:
                        return _Sheet0()
                    raise RuntimeError("sheet fetch failed")

            return _Sheets()

    clean = _Doc()
    with pytest.raises(RuntimeError):
        maybe_rewrite_collabora_py_formulas(clean)
    assert clean.formula.startswith("=PY(")
    assert clean.sets == [False]


def test_maybe_rewrite_disposal_reraises_document_disposed():
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    class DisposedException(Exception):
        pass

    class _Addr:
        StartRow = 0
        StartColumn = 0
        EndRow = 0
        EndColumn = 0

    class _Doc:
        def __init__(self) -> None:
            self.formula0 = _GETPY
            self.formula1 = _GETPY
            self.modified = False
            self.sets: list[bool] = []
            self.calculated = 0

        def supportsService(self, name: str) -> bool:
            return name == "com.sun.star.sheet.SpreadsheetDocument"

        def isModified(self) -> bool:
            return self.modified

        def setModified(self, value: bool) -> None:
            self.sets.append(bool(value))
            self.modified = bool(value)

        def calculateAll(self) -> None:
            self.calculated += 1

        def getSheets(self):
            doc = self

            def _make_sheet(is_sheet_1: bool):
                class _Range:
                    def getRangeAddress(self):
                        return _Addr()

                    def getFormulaArray(self):
                        f = doc.formula1 if is_sheet_1 else doc.formula0
                        return ((f,),)

                class _Cells:
                    def getCount(self):
                        return 1

                    def getByIndex(self, _index):
                        return _Range()

                class _Sheet:
                    def queryContentCells(self, _flags):
                        return _Cells()

                    def getCellByPosition(self, _col, _row):
                        class _Cell:
                            def setFormula(self, value: str) -> None:
                                if is_sheet_1:
                                    raise DisposedException("document disposed")
                                doc.formula0 = value

                        return _Cell()

                return _Sheet()

            class _Sheets:
                def getCount(self):
                    return 2

                def getByIndex(self, index):
                    return _make_sheet(index == 1)

            return _Sheets()

    clean = _Doc()
    with pytest.raises(DocumentDisposedError):
        maybe_rewrite_collabora_py_formulas(clean)
    assert clean.formula0.startswith("=PY(")
    assert clean.sets == [False]
