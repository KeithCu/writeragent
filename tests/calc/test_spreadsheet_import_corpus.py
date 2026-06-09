# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Corpus conversion rate gate for spreadsheet import."""

from __future__ import annotations

import json
from pathlib import Path

from plugin.calc.spreadsheet_import.emit import build_converted_output_model
from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.models import FORMULA_LIKE_TYPES

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "spreadsheet_import_corpus" / "simple_budget_snapshot.json"


def _load_fixture():
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return ingest_from_arrays(
        sheet_name=payload["sheet_name"],
        start_col=payload["start_col"],
        start_row=payload["start_row"],
        data_array=payload["data_array"],
        formula_array=payload["formula_array"],
    )


def test_budget_corpus_conversion_rate():
    model = _load_fixture()
    output, report = build_converted_output_model(model)

    formula_cells = [
        addr
        for addr, cell in model.cells.items()
        if cell.type in FORMULA_LIKE_TYPES and cell.type not in ("py_formula", "array_formula")
    ]
    formula_cells = [a for a in formula_cells if model.cells[a].type == "formula"]

    from plugin.contrib.calc_formula_parser import parse_formula
    from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
    from plugin.calc.spreadsheet_import.emit import _has_function_node

    function_formula_cells = []
    for a in formula_cells:
        cell = model.cells[a]
        if cell.formula:
            try:
                ast = parse_formula(normalize_lo_formula_for_parse(cell.formula))
                if _has_function_node(ast):
                    function_formula_cells.append(a)
            except Exception:
                pass

    rate = len(report.converted) / len(function_formula_cells) if function_formula_cells else 0.0
    assert rate >= 0.70
    assert len(report.converted) == len(function_formula_cells), report.skipped


def test_budget_expected_conversions():
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    expected = payload["expected_conversions"]
    model = _load_fixture()
    output, _report = build_converted_output_model(model)
    for addr, want in expected.items():
        got = output.cells[addr].formula
        assert got == want, f"{addr}: {got!r} != {want!r}"
