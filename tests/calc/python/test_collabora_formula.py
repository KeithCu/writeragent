# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Collabora pythoncompute OriginalName → LibrePy =PY() prefix rewrite."""

from __future__ import annotations

from collections.abc import Callable

from plugin.calc.python.cell_discovery import is_py_formula_text
from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas, rewrite_collabora_addin_prefix
from plugin.calc.python.formula_edit import PyFormula
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


def test_rewrite_preserves_quotes_and_brackets():
    curly = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("s = “hi”")'
    assert rewrite_collabora_addin_prefix(curly) == '=PY("s = “hi”")'

    doubled = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("x = ""a""")'
    assert rewrite_collabora_addin_prefix(doubled) == '=PY("x = ""a""")'

    wrapped = '  {=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("x")}'
    assert rewrite_collabora_addin_prefix(wrapped) == '  {=PY("x")}'


def test_canonicalize_and_parse_collabora_getpy():
    assert is_py_formula_text(_GETPY)
    parsed = PyFormula.parse(_GETPY)
    assert parsed is not None
    assert parsed.code == "result = 1"


def test_maybe_rewrite_skips_non_calc():
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    class _Writer:
        def supportsService(self, name: str) -> bool:
            return name != "com.sun.star.sheet.SpreadsheetDocument"

    assert maybe_rewrite_collabora_py_formulas(_Writer()) == 0


def test_maybe_rewrite_none_doc():
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    assert maybe_rewrite_collabora_py_formulas(None) == 0


class _FakeDisposedException(Exception):
    pass


class _FakeAddr:
    StartRow = 0
    StartColumn = 0
    EndRow = 0
    EndColumn = 0


class _FakeCalcDoc:
    def __init__(
        self,
        formulas: list[str] | None = None,
        *,
        modified: bool = False,
        on_get_sheet: Callable[[int], None] | None = None,
        on_set_formula: Callable[[int, str], None] | None = None,
        is_modified_exc: Exception | None = None,
        unlock_exc: Exception | None = None,
    ) -> None:
        self.formulas = list(formulas) if formulas is not None else [_GETPY]
        self.modified = modified
        self.sets: list[bool] = []
        self.calculated = 0
        self.locked = False
        self._on_get_sheet = on_get_sheet
        self._on_set_formula = on_set_formula
        self._is_modified_exc = is_modified_exc
        self._unlock_exc = unlock_exc

    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.sheet.SpreadsheetDocument"

    def isModified(self) -> bool:
        if self._is_modified_exc is not None:
            raise self._is_modified_exc
        return self.modified

    def setModified(self, value: bool) -> None:
        self.sets.append(bool(value))
        self.modified = bool(value)

    def calculateAll(self) -> None:
        self.calculated += 1

    def lockControllers(self) -> None:
        self.locked = True

    def unlockControllers(self) -> None:
        if self._unlock_exc is not None:
            raise self._unlock_exc

    def getSheets(self):
        doc = self

        def _make_sheet(sheet_idx: int):
            class _Range:
                def getRangeAddress(self):
                    return _FakeAddr()

                def getFormulaArray(self):
                    return ((doc.formulas[sheet_idx],),)

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
                            if doc._on_set_formula is not None:
                                doc._on_set_formula(sheet_idx, value)
                            doc.formulas[sheet_idx] = value

                    return _Cell()

            return _Sheet()

        class _Sheets:
            def getCount(self):
                return len(doc.formulas)

            def getByIndex(self, index: int):
                if doc._on_get_sheet is not None:
                    doc._on_get_sheet(index)
                return _make_sheet(index)

        return _Sheets()


def test_maybe_rewrite_restores_is_modified():
    """setFormula dirties the book; restore the flag from before the scan."""
    dirty = _FakeCalcDoc([_GETPY], modified=True)
    assert maybe_rewrite_collabora_py_formulas(dirty) == 1
    assert dirty.formulas[0].startswith("=PY(")
    assert dirty.sets == [True]
    assert dirty.calculated == 1

    clean = _FakeCalcDoc([_GETPY], modified=False)
    assert maybe_rewrite_collabora_py_formulas(clean) == 1
    assert clean.sets == [False]
    assert clean.calculated == 1

    untouched = _FakeCalcDoc(["=SUM(A1:A2)"], modified=True)
    assert maybe_rewrite_collabora_py_formulas(untouched) == 0
    assert untouched.sets == []
    assert untouched.calculated == 0


def test_maybe_rewrite_partial_failure_restores_is_modified():
    def _fail_second_sheet(index: int) -> None:
        if index > 0:
            raise RuntimeError("sheet fetch failed")

    clean = _FakeCalcDoc([_GETPY, _GETPY], modified=False, on_get_sheet=_fail_second_sheet)
    with pytest.raises(RuntimeError):
        maybe_rewrite_collabora_py_formulas(clean)
    assert clean.formulas[0].startswith("=PY(")
    assert clean.sets == [False]


def test_maybe_rewrite_disposal_reraises_document_disposed():
    def _dispose_on_sheet_1(sheet_idx: int, _value: str) -> None:
        if sheet_idx == 1:
            raise _FakeDisposedException("document disposed")

    clean = _FakeCalcDoc([_GETPY, _GETPY], modified=False, on_set_formula=_dispose_on_sheet_1)
    with pytest.raises(DocumentDisposedError):
        maybe_rewrite_collabora_py_formulas(clean)
    assert clean.formulas[0].startswith("=PY(")
    assert clean.sets == [False]


def test_maybe_rewrite_is_modified_disposal_raises():
    doc = _FakeCalcDoc([], is_modified_exc=_FakeDisposedException("gone"))
    with pytest.raises(DocumentDisposedError):
        maybe_rewrite_collabora_py_formulas(doc)
    assert doc.locked is False


def test_maybe_rewrite_unlock_disposal_raises():
    doc = _FakeCalcDoc([], unlock_exc=_FakeDisposedException("gone"))
    with pytest.raises(DocumentDisposedError):
        maybe_rewrite_collabora_py_formulas(doc)


def test_rewrite_edge_spellings_unchanged():
    from plugin.calc.python.collabora_formula import rewrite_collabora_addin_prefix

    g = "ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY"
    assert rewrite_collabora_addin_prefix("= " + g + '  ("x")') == '= PY("x")'
    assert rewrite_collabora_addin_prefix("=" + g + "X(1)") == "=" + g + "X(1)"
    assert rewrite_collabora_addin_prefix("=" + g + "THONX(1)") == "=" + g + "THONX(1)"
    assert rewrite_collabora_addin_prefix(" { =  " + g.lower() + 'thon ("y"; A1)}') == ' { =  PYTHON("y"; A1)}'
    assert rewrite_collabora_addin_prefix("=" + g) == "=" + g
