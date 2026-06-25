# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import PY extract + normalize."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from plugin.calc.python.formula_edit import parse_python_formula
from plugin.calc.spreadsheet_import.extract import (
    canonicalize_py_formula_for_parse,
    extract_py_cells,
    is_py_formula_text,
    normalize_py_formula,
    py_formula_semantics,
)
from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.models import CellRecord, SheetModel

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.calc.serialization_cases import all_serialization_cases, case_input_grids  # noqa: E402

_GEN_PATH = REPO_ROOT / "scripts" / "generate_serialization_spreadsheet.py"
if _GEN_PATH.is_file():
    _spec = importlib.util.spec_from_file_location("generate_serialization_spreadsheet", _GEN_PATH)
    assert _spec and _spec.loader
    _gen = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _gen
    _spec.loader.exec_module(_gen)
else:
    _gen = None


def test_canonicalize_libreoffice_addin_python_prefix():
    raw = '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON("np.sum(data)";A1:A2)'
    assert canonicalize_py_formula_for_parse(raw) == '=PYTHON("np.sum(data)";A1:A2)'
    assert is_py_formula_text(raw)
    norm = normalize_py_formula(raw)
    assert norm is not None
    assert py_formula_semantics(raw) == py_formula_semantics(norm)


def test_normalize_python_to_py_prefix():
    formula = '=PYTHON("np.sum(data)",A1:B2)'
    norm = normalize_py_formula(formula)
    assert norm is not None
    assert norm.startswith("=PY(")
    assert ";" in norm
    assert "," not in norm.split("(", 1)[1]


def test_normalize_preserves_code_and_data_args():
    formula = '=PYTHON("np.sum(data)"; Sheet1.A1:B2; C3)'
    norm = normalize_py_formula(formula)
    assert norm is not None
    assert py_formula_semantics(formula) == py_formula_semantics(norm)


def test_normalize_escapes_quotes_in_code():
    formula = '=PYTHON("x = ""hi""")'
    norm = normalize_py_formula(formula)
    assert norm is not None
    assert py_formula_semantics(formula) == py_formula_semantics(norm)


def test_normalize_already_canonical_semantics_unchanged():
    formula = '=PY("np.sum(data)"; A1:B2)'
    norm = normalize_py_formula(formula)
    assert norm is not None
    assert py_formula_semantics(formula) == py_formula_semantics(norm)


def test_normalize_non_python_returns_none():
    assert normalize_py_formula("=SUM(A1)") is None


def test_extract_py_cells_from_model():
    model = SheetModel(
        sheet_name="S",
        used_range="A1:B2",
        cells={
            "A1": CellRecord("A1", "constant", 1.0, None, None),
            "B1": CellRecord("B1", "py_formula", 2.0, '=PYTHON("np.sum(data)",A1)', None),
            "A2": CellRecord("A2", "formula", 3.0, "=SUM(A1)", None),
        },
    )
    extracts = extract_py_cells(model)
    assert len(extracts) == 1
    assert extracts[0].address == "B1"
    assert extracts[0].changed is True
    assert extracts[0].code == "np.sum(data)"
    assert extracts[0].data_args == ["A1"]


def test_extract_skips_unparseable_py_like():
    model = SheetModel(
        sheet_name="S",
        used_range="A1",
        cells={
            "A1": CellRecord("A1", "py_formula", 0.0, "=PY(broken", None),
        },
    )
    assert extract_py_cells(model) == []


@pytest.mark.skipif(_gen is None, reason="generate_serialization_spreadsheet.py not available")
def test_all_serialization_cases_normalize_semantically():
    for case in all_serialization_cases():
        grids = case_input_grids(case)
        max_nrows = max((_gen.grid_dimensions(g)[0] for g in grids), default=0)
        data_top = 2 + 1 if max_nrows else 2
        data_ranges = _gen.data_ranges_for_case(case, data_top)
        formula = _gen._python_formula(case, data_ranges)
        norm = normalize_py_formula(formula)
        assert norm is not None, f"{case.id}: could not normalize {formula!r}"
        assert py_formula_semantics(formula) == py_formula_semantics(norm), case.id
