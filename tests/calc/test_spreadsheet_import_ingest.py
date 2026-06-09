# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import ingest + dependency graph (no LibreOffice)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from plugin.calc.spreadsheet_import.graph import (
    build_dependency_graph,
    extract_cell_refs,
    extract_range_refs,
    filter_refs_to_scope,
    topological_formula_order,
)
from plugin.calc.spreadsheet_import.ingest import classify_cell, ingest_from_arrays
_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "spreadsheet_import_corpus" / "simple_budget_snapshot.json"


def test_classify_empty():
    cell_type, value, formula, error = classify_cell(None, "")
    assert cell_type == "empty"
    assert value is None
    assert formula is None
    assert error is None


def test_classify_constant_number():
    cell_type, value, formula, _ = classify_cell(42.0, "")
    assert cell_type == "constant"
    assert value == 42.0
    assert formula is None


def test_classify_constant_text():
    cell_type, value, formula, _ = classify_cell("hello", "")
    assert cell_type == "constant"
    assert value == "hello"
    assert formula is None


def test_classify_sum_formula():
    cell_type, value, formula, _ = classify_cell(10.0, "=SUM(A1:A3)")
    assert cell_type == "formula"
    assert value == 10.0
    assert formula == "=SUM(A1:A3)"


def test_classify_py_formula():
    cell_type, _, formula, _ = classify_cell(1.0, '=PY("result = 1"; A1)')
    assert cell_type == "py_formula"
    assert formula == '=PY("result = 1"; A1)'


def test_classify_python_alias():
    cell_type, _, formula, _ = classify_cell(2.0, '=PYTHON("result = 2")')
    assert cell_type == "py_formula"
    assert formula == '=PYTHON("result = 2")'


def test_classify_prompt_formula():
    cell_type, _, formula, _ = classify_cell("answer", '=PROMPT("summarize")')
    assert cell_type == "prompt"
    assert formula == '=PROMPT("summarize")'


def test_classify_array_formula():
    cell_type, _, formula, _ = classify_cell(6.0, "{=SUM(A1:A2)}")
    assert cell_type == "array_formula"
    assert formula == "{=SUM(A1:A2)}"


def test_classify_error_display():
    cell_type, value, formula, error = classify_cell("#DIV/0!", "=A1/0")
    assert cell_type == "error"
    assert value == "#DIV/0!"
    assert formula == "=A1/0"
    assert error == "#DIV/0!"


def test_extract_cell_refs():
    refs = extract_cell_refs("=SUM($A$1:B2)+C3")
    assert refs == ["A1", "B2", "C3"]


def test_extract_range_refs():
    refs = extract_range_refs("=SUM($A$1:B2)+C3")
    assert refs == ["A1:B2", "C3"]


def test_filter_refs_to_scope():
    scope = frozenset({"A1", "B1"})
    assert filter_refs_to_scope(["A1", "B1", "Z9", "A1"], scope) == ["A1", "B1"]


def test_topological_chain():
    graph = {"B1": ["A1"], "C1": ["B1"]}
    order, cycles = topological_formula_order(graph)
    assert order == ["B1", "C1"]
    assert cycles == []


def test_topological_diamond():
    graph = {"C1": ["A1", "B1"], "D1": ["C1"]}
    order, cycles = topological_formula_order(graph)
    assert order.index("C1") < order.index("D1")
    assert "C1" in order
    assert cycles == []


def test_topological_two_cell_cycle():
    graph = {"A1": ["B1"], "B1": ["A1"]}
    order, cycles = topological_formula_order(graph)
    assert order == []
    assert len(cycles) == 1
    assert set(cycles[0]) == {"A1", "B1"}


def test_ingest_golden_budget_grid():
    data = [
        ["Item", "Amount", "Tax", "Total"],
        ["Rent", 1000.0, 0.1, 1100.0],
        ["Food", 300.0, 0.1, 330.0],
        ["", 1300.0, "", 1430.0],
    ]
    formulas = [
        ["", "", "", ""],
        ["", "", "=B2*0.1", "=B2+C2"],
        ["", "", "=B3*0.1", "=B3+C3"],
        ["", "=SUM(B2:B3)", "", "=SUM(D2:D3)"],
    ]
    model = ingest_from_arrays(
        sheet_name="Budget",
        start_col=0,
        start_row=0,
        data_array=data,
        formula_array=formulas,
    )
    assert model.used_range == "A1:D4"
    assert model.cells["B2"].type == "constant"
    assert model.cells["C2"].type == "formula"
    assert model.cells["C2"].precedents == ["B2"]
    assert model.cells["D2"].precedents == ["B2", "C2"]
    # B4 has no formula precedents (only constants), so it sorts before C2/C3.
    assert model.formula_order == ["B4", "C2", "C3", "D2", "D3", "D4"]


def test_ingest_precedents_exclude_out_of_scope():
    data = [[1.0, 2.0]]
    formulas = [["=Z99+A1", ""]]
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=data,
        formula_array=formulas,
    )
    assert model.cells["A1"].precedents == []


def test_sheet_model_to_dict_keys():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[1.0]],
        formula_array=[[""]],
    )
    payload = model.to_dict()
    assert set(payload) == {"sheet_name", "used_range", "cells", "formula_order", "circular_groups"}
    assert payload["cells"]["A1"]["type"] == "constant"


def test_fixture_simple_budget_snapshot():
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    model = ingest_from_arrays(
        sheet_name=raw["sheet_name"],
        start_col=raw["start_col"],
        start_row=raw["start_row"],
        data_array=raw["data_array"],
        formula_array=raw["formula_array"],
    )
    assert model.formula_order == raw["expected_formula_order"]
    for addr, expected_type in raw["expected_types"].items():
        assert model.cells[addr].type == expected_type


def test_build_dependency_graph_from_model():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[1.0, 2.0], [3.0, 4.0]],
        formula_array=[["", "=A1+1"], ["=A1*2", ""]],
    )
    graph = build_dependency_graph(model)
    assert graph["B1"] == ["A1"]
    assert graph["A2"] == ["A1"]


@pytest.mark.slow
def test_ingest_performance_100k_cells():
    rows, cols = 5000, 20
    data = [[float(c) if c == 0 else "" for c in range(cols)] for _ in range(rows)]
    formulas = [["" for _ in range(cols)] for _ in range(rows)]
    formulas[0][0] = "=SUM(A2:A5000)"
    t0 = time.perf_counter()
    model = ingest_from_arrays(
        sheet_name="Perf",
        start_col=0,
        start_row=0,
        data_array=data,
        formula_array=formulas,
    )
    elapsed = time.perf_counter() - t0
    assert len(model.cells) == rows * cols
    assert elapsed < 2.0, f"ingest took {elapsed:.2f}s"
