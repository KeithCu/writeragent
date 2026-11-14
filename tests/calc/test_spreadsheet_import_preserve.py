# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import preserve output model and PY normalization."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plugin.calc.python.cell_discovery import is_py_formula_text
from plugin.calc.python.formula_edit import PyFormula
from plugin.calc.spreadsheet_import.emit import build_converted_output_model
from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.models import CellRecord, OutputCell, OutputSheetModel, SheetModel
from plugin.calc.spreadsheet_import.preserve import (
    apply_output_to_sheet,
    enrich_number_formats,
    output_cell_from_record,
)
from tests.calc.serialization_cases import all_serialization_cases, case_input_grids

_GEN_PATH = Path(__file__).resolve().parents[2] / "scripts" / "generate_serialization_spreadsheet.py"


def _normalized(formula: str) -> str | None:
    return output_cell_from_record(CellRecord("A1", "py_formula", None, formula, None)).formula


def test_build_output_preserves_constants():
    model = SheetModel(
        sheet_name="S",
        used_range="A1:B1",
        cells={
            "A1": CellRecord("A1", "constant", 42.0, None, 123),
            "B1": CellRecord("B1", "constant", "hello", None, 456),
        },
    )
    output, _report = build_converted_output_model(model, vectorize=False)
    assert output.cells["A1"].value == 42.0
    assert output.cells["A1"].formula is None
    assert output.cells["A1"].number_format == 123
    assert output.cells["B1"].value == "hello"
    assert output.cells["B1"].number_format == 456


def test_build_output_leaves_calc_formula_unchanged():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[10.0, 20.0]],
        formula_array=[["=SUM(A1)", ""]],
    )
    output, _report = build_converted_output_model(model, vectorize=False)
    assert output.cells["A1"].formula == "=SUM(A1)"
    assert output.cells["B1"].value == 20.0


def test_build_output_normalizes_py_formula():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[1.0, 2.0]],
        formula_array=[["", '=PYTHON("np.sum(data)",A1)']],
    )
    output, _report = build_converted_output_model(model, vectorize=False)
    assert output.cells["B1"].formula is not None
    assert output.cells["B1"].formula.startswith("=PY(")
    assert ";" in output.cells["B1"].formula


def test_build_output_passes_through_prompt_and_array():
    model = SheetModel(
        sheet_name="S",
        used_range="A1:B1",
        cells={
            "A1": CellRecord("A1", "prompt", "x", '=PROMPT("hi")', None),
            "B1": CellRecord("B1", "array_formula", 6.0, "{=SUM(A1:A2)}", None),
        },
    )
    output, _report = build_converted_output_model(model, vectorize=False)
    assert output.cells["A1"].formula == '=PROMPT("hi")'
    assert output.cells["B1"].formula == "{=SUM(A1:A2)}"


def test_build_output_empty_cells():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[""]],
        formula_array=[[""]],
    )
    output, _report = build_converted_output_model(model, vectorize=False)
    assert output.cells["A1"].value is None
    assert output.cells["A1"].formula is None


def test_enrich_number_formats_updates_constants():
    model = SheetModel(
        sheet_name="S",
        used_range="A1:B1",
        cells={
            "A1": CellRecord("A1", "constant", 42.0, None, None),
            "B1": CellRecord("B1", "formula", "=SUM(1)", None, None),
        },
    )
    sheet = MagicMock()
    cell_a1 = MagicMock()
    cell_a1.getPropertyValue.return_value = 4
    sheet.getCellByPosition.return_value = cell_a1

    enrich_number_formats(sheet, model)
    assert model.cells["A1"].number_format == 4
    assert model.cells["B1"].number_format is None

    cell_a1.getPropertyValue.return_value = None
    model.cells["A1"].number_format = None
    enrich_number_formats(sheet, model)
    assert model.cells["A1"].number_format is None


def test_apply_output_to_sheet():
    sheet = MagicMock()
    cells: dict[tuple[int, int], MagicMock] = {}

    def get_cell(c: int, r: int) -> MagicMock:
        if (c, r) not in cells:
            cells[(c, r)] = MagicMock()
        return cells[(c, r)]

    sheet.getCellByPosition.side_effect = get_cell

    output = OutputSheetModel(
        sheet_name="S",
        used_range="A1:E1",
        cells={
            "A1": OutputCell(address="A1", value=None, formula="=SUM(1, 2)", number_format=None),
            "B1": OutputCell(address="B1", value=None, formula="{=A1:A2}", number_format=None),
            "C1": OutputCell(address="C1", value=None, formula=None, number_format=None),
            "D1": OutputCell(address="D1", value=42.5, formula=None, number_format=5),
            "E1": OutputCell(address="E1", value="hello", formula=None, number_format=None),
        },
    )

    apply_output_to_sheet(sheet, output)

    cells[(0, 0)].setFormula.assert_called_once_with("=SUM(1, 2)")
    cells[(1, 0)].setFormula.assert_called_once_with("{=A1:A2}")
    assert (2, 0) not in cells
    cells[(3, 0)].setValue.assert_called_once_with(42.5)
    cells[(3, 0)].setPropertyValue.assert_called_once_with("NumberFormat", 5)
    cells[(4, 0)].setString.assert_called_once_with("hello")


def test_apply_output_empty_model_does_not_raise():
    model = ingest_from_arrays(sheet_name="S", start_col=0, start_row=0, data_array=[], formula_array=[])
    output, _report = build_converted_output_model(model, vectorize=False)
    sheet = MagicMock()
    apply_output_to_sheet(sheet, output)
    assert sheet.getCellByPosition.call_count == 0


def test_apply_output_ragged_model_writes_only_existing_cells():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[1.0, 2.0], [3.0]],
        formula_array=[["", ""], [""]],
    )
    output, _report = build_converted_output_model(model, vectorize=False)
    sheet = MagicMock()
    apply_output_to_sheet(sheet, output)
    assert sheet.getCellByPosition.call_count == 3
    assert ("B2" in output.cells) is False


def test_apply_output_keeps_text_that_looks_numeric():
    sheet = MagicMock()
    cells: dict[tuple[int, int], MagicMock] = {}

    def get_cell(c: int, r: int) -> MagicMock:
        if (c, r) not in cells:
            cells[(c, r)] = MagicMock()
        return cells[(c, r)]

    sheet.getCellByPosition.side_effect = get_cell

    output = OutputSheetModel(
        sheet_name="S",
        used_range="A1:G1",
        cells={
            "A1": OutputCell(address="A1", value="00123", formula=None, number_format=None),
            "B1": OutputCell(address="B1", value="42", formula=None, number_format=None),
            "C1": OutputCell(address="C1", value="=00123", formula=None, number_format=None),
            "D1": OutputCell(address="D1", value=7, formula=None, number_format=None),
            "E1": OutputCell(address="E1", value=0, formula=None, number_format=None),
            "F1": OutputCell(address="F1", value=True, formula=None, number_format=None),
            "G1": OutputCell(address="G1", value=False, formula=None, number_format=None),
        },
    )

    apply_output_to_sheet(sheet, output)

    cells[(0, 0)].setString.assert_called_once_with("00123")
    cells[(0, 0)].setValue.assert_not_called()

    cells[(1, 0)].setString.assert_called_once_with("42")
    cells[(1, 0)].setValue.assert_not_called()

    cells[(2, 0)].setString.assert_called_once_with("=00123")
    cells[(2, 0)].setFormula.assert_not_called()

    cells[(3, 0)].setValue.assert_called_once_with(7)
    cells[(3, 0)].setString.assert_not_called()

    cells[(4, 0)].setValue.assert_called_once_with(0)
    cells[(4, 0)].setString.assert_not_called()

    cells[(5, 0)].setValue.assert_called_once_with(1.0)
    cells[(5, 0)].setString.assert_not_called()

    cells[(6, 0)].setValue.assert_called_once_with(0.0)
    cells[(6, 0)].setString.assert_not_called()


def test_canonicalize_libreoffice_addin_python_prefix():
    raw = '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON("np.sum(data)";A1:A2)'
    parsed = PyFormula.parse(raw)
    assert parsed is not None
    assert parsed.code == "np.sum(data)"
    assert parsed.data_args() == ["A1:A2"]
    assert is_py_formula_text(raw)
    norm = _normalized(raw)
    assert norm is not None
    assert norm == '=PY("np.sum(data)";A1:A2)'
    norm_parsed = PyFormula.parse(norm)
    assert norm_parsed is not None
    assert (parsed.code, parsed.data_args()) == (norm_parsed.code, norm_parsed.data_args())


def test_canonicalize_collabora_getpy_prefix():
    raw = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("np.sum(data)";A1:A2)'
    parsed = PyFormula.parse(raw)
    assert parsed is not None
    assert parsed.code == "np.sum(data)"
    assert parsed.data_args() == ["A1:A2"]
    assert is_py_formula_text(raw)
    norm = _normalized(raw)
    assert norm is not None
    assert norm == '=PY("np.sum(data)";A1:A2)'


def test_normalize_python_to_py_prefix():
    formula = '=PYTHON("np.sum(data)",A1:B2)'
    norm = _normalized(formula)
    assert norm is not None
    assert norm.startswith("=PY(")
    assert ";" in norm
    assert "," not in norm.split("(", 1)[1]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('=PYTHON("np.sum(data)"; Sheet1.A1:B2; C3)', id="test_normalize_preserves_code_and_data_args"),
        pytest.param('=PYTHON("x = ""hi""")', id="test_normalize_escapes_quotes_in_code"),
        pytest.param('=PY("np.sum(data)"; A1:B2)', id="test_normalize_already_canonical_semantics_unchanged"),
    ],
)
def test_normalize_preserves_code_and_data_args(value):
    formula = value
    norm = _normalized(formula)
    assert norm is not None
    raw_p = PyFormula.parse(formula)
    norm_p = PyFormula.parse(norm)
    assert raw_p is not None and norm_p is not None
    assert (raw_p.code, raw_p.data_args()) == (norm_p.code, norm_p.data_args())


@pytest.mark.parametrize("formula", ["=PY(broken", "=SUM(A1)"])
def test_preserve_keeps_formula_that_does_not_parse_as_py(formula):
    rec = CellRecord("A1", "py_formula", 0.0, formula, None)
    out = output_cell_from_record(rec)
    assert out.formula == formula


def test_preserve_normalizes_py_cell_from_record():
    rec = CellRecord("B1", "py_formula", 2.0, '=PYTHON("np.sum(data)",A1)', None)
    out = output_cell_from_record(rec)
    assert out.formula is not None
    assert out.formula.startswith("=PY(")
    assert ";" in out.formula
    parsed = PyFormula.parse(out.formula)
    assert parsed is not None
    assert (parsed.code, parsed.data_args()) == ("np.sum(data)", ["A1"])


@pytest.fixture
def _gen_module(monkeypatch: pytest.MonkeyPatch):
    if not _GEN_PATH.is_file():
        pytest.skip("generate_serialization_spreadsheet.py not available")
    spec = importlib.util.spec_from_file_location("generate_serialization_spreadsheet", _GEN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules for @dataclass string-annotation resolution during exec_module.
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def test_all_serialization_cases_normalize_semantically(_gen_module):
    for case in all_serialization_cases():
        grids = case_input_grids(case)
        max_nrows = max((_gen_module.grid_dimensions(g)[0] for g in grids), default=0)
        data_top = 2 + 1 if max_nrows else 2
        data_ranges = _gen_module.data_ranges_for_case(case, data_top)
        formula = _gen_module._python_formula(case, data_ranges)
        norm = _normalized(formula)
        assert norm is not None, f"{case.id}: could not normalize {formula!r}"
        raw_p = PyFormula.parse(formula)
        norm_p = PyFormula.parse(norm)
        assert raw_p is not None and norm_p is not None
        assert (raw_p.code, raw_p.data_args()) == (norm_p.code, norm_p.data_args()), case.id
