# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc formula evaluation messages (no soffice)."""

from plugin.calc.formulas import FORMULA, EvaluateFormula, formula_evaluation_error_message
from plugin.framework.tool import ToolContext


def test_error_503_is_num_not_div_zero():
    # 503 and 532 used to share a #DIV/0! string that always said code 532.
    message = formula_evaluation_error_message(503)
    assert message == "Formula evaluation error: #NUM! (Invalid numeric value, code 503)"
    assert "#DIV/0!" not in message
    assert "532" not in message


def test_error_532_is_div_zero():
    message = formula_evaluation_error_message(532)
    assert "#DIV/0!" in message
    assert "code 532" in message
    assert "#NUM!" not in message


def test_other_formula_error_codes_keep_their_labels():
    assert "Pair missing bracket (code 508)" in formula_evaluation_error_message(508)
    assert "#REF!" in formula_evaluation_error_message(524)
    assert formula_evaluation_error_message(519) == "Formula evaluation error: code 519"


class _EvalCell:
    """Cell that records setFormula. getCellRangeByName is not used."""

    def __init__(self) -> None:
        self.formulas: list[str] = []
        self.Error = 0
        self.FormulaResultType = 1
        self.kind = None
        self.value: float | str = 7
        self.text = "7"

    def setFormula(self, formula: str) -> None:
        self.formulas.append(formula)

    def getType(self):
        from plugin.calc.formulas import VALUE

        return VALUE if self.kind is None else self.kind

    def getValue(self):
        return self.value

    def getString(self) -> str:
        return self.text


class _EvalSheet:
    def __init__(self, name: str, cell: _EvalCell, foreign: _EvalCell | None = None) -> None:
        self.name = name
        self.cell = cell
        self.foreign = foreign if foreign is not None else cell
        self.positions: list[tuple[int, int]] = []
        self.range_calls: list[str] = []

    def getName(self) -> str:
        return self.name

    def getCellByPosition(self, col: int, row: int) -> _EvalCell:
        self.positions.append((col, row))
        return self.cell

    def getCellRangeByName(self, name: str):
        # Document-wide resolution: a qualified name or defined name returns
        # the live cell, which is the bug evaluate_formula must not hit.
        self.range_calls.append(name)

        class _Range:
            def __init__(self, target: _EvalCell) -> None:
                self.target = target

            def getCellByPosition(self, _col: int, _row: int) -> _EvalCell:
                return self.target

        return _Range(self.foreign)


class _EvalSheets:
    def __init__(self, live: _EvalSheet, temp: _EvalSheet) -> None:
        self.live = live
        self.temp = temp
        self.names = {live.name}
        self.copied: list[tuple[str, str, int]] = []
        self.removed: list[str] = []

    def hasByName(self, name: str) -> bool:
        return name in self.names

    def getCount(self) -> int:
        return len(self.names)

    def copyByName(self, src: str, dest: str, index: int) -> None:
        self.copied.append((src, dest, index))
        self.names.add(dest)

    def getByName(self, name: str) -> _EvalSheet:
        if name == self.live.name:
            return self.live
        return self.temp

    def removeByName(self, name: str) -> None:
        self.removed.append(name)
        self.names.discard(name)


class _EvalDoc:
    def __init__(self) -> None:
        self.live_cell = _EvalCell()
        self.temp_cell = _EvalCell()
        self.live = _EvalSheet("Sheet1", self.live_cell)
        # Qualifying a name on the copy still yields the live cell.
        self.temp = _EvalSheet("temp", self.temp_cell, foreign=self.live_cell)
        self.sheets = _EvalSheets(self.live, self.temp)

    def getSheets(self) -> _EvalSheets:
        return self.sheets

    def getCurrentController(self):
        return self

    def getActiveSheet(self) -> _EvalSheet:
        return self.live


def _eval_ctx(doc: _EvalDoc) -> ToolContext:
    return ToolContext(doc, None, "calc", None)


def test_evaluate_formula_sheet_qualified_cell_does_not_write_the_live_sheet():
    # getCellRangeByName("Sheet1.C5") resolves the live sheet. The context
    # is only the bare coordinate on the temporary copy.
    doc = _EvalDoc()
    result = EvaluateFormula().execute(_eval_ctx(doc), formula="=A1+1", cell="Sheet1.C5")

    assert result["status"] == "ok"
    assert result["result"] == 7
    assert doc.live_cell.formulas == []
    assert doc.live.positions == []
    assert doc.live.range_calls == []
    assert doc.temp.range_calls == []
    assert doc.temp.positions == [(2, 4)]
    assert doc.temp_cell.formulas == ["=A1+1"]
    assert doc.sheets.removed  # the copy is deleted afterwards


def test_evaluate_formula_defined_name_does_not_write_the_live_sheet():
    doc = _EvalDoc()
    result = EvaluateFormula().execute(_eval_ctx(doc), formula="=1", cell="TaxRate")

    assert result["status"] == "error"
    assert "TaxRate" in result["message"] or "TAXRATE" in result["message"]
    assert doc.live_cell.formulas == []
    assert doc.temp_cell.formulas == []
    assert doc.sheets.copied == []
    assert doc.temp.range_calls == []


def test_evaluate_formula_nonzero_stays_a_float_when_result_kind_differs():
    # FormulaResult.VALUE is 0 on some LibreOffice builds and 1 on others.
    # A hardcoded 1 used to send =2+3 through getString(), so the result was
    # the text "5" and the UNO check against 5.0 failed.
    doc = _EvalDoc()
    doc.temp_cell.kind = FORMULA
    doc.temp_cell.value = 5
    doc.temp_cell.text = "5"
    doc.temp_cell.FormulaResultType = 0

    result = EvaluateFormula().execute(_eval_ctx(doc), formula="=2+3", cell="A1")

    assert result["status"] == "ok"
    assert result["result"] == 5.0
    assert isinstance(result["result"], float)
    assert result["result_type"] == "formula"


def test_evaluate_formula_numeric_zero_stays_a_number():
    doc = _EvalDoc()
    doc.temp_cell.kind = FORMULA
    doc.temp_cell.value = 0.0
    doc.temp_cell.text = "0"
    doc.temp_cell.FormulaResultType = 1

    result = EvaluateFormula().execute(_eval_ctx(doc), formula="=1-1", cell="A1")

    assert result["status"] == "ok"
    assert result["result"] == 0.0
    assert result["result_type"] == "formula"
    assert not isinstance(result["result"], str)

    text_doc = _EvalDoc()
    text_doc.temp_cell.kind = FORMULA
    text_doc.temp_cell.value = 0.0
    text_doc.temp_cell.text = "hello"
    text_doc.temp_cell.FormulaResultType = 2
    text = EvaluateFormula().execute(_eval_ctx(text_doc), formula='="hello"', cell="A1")
    assert text["status"] == "ok"
    assert text["result"] == "hello"
