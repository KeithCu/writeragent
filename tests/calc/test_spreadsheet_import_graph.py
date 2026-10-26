# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import formula dependency graph (no LibreOffice)."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.graph import (
    build_dependency_graph,
    extract_range_refs,
    topological_formula_order,
)
from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays


def test_extract_range_refs():
    refs = extract_range_refs("=SUM($A$1:B2)+C3")
    assert refs == ["A1:B2", "C3"]


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


def test_downstream_of_cycle_is_not_circular():
    graph = {"A1": ["B1"], "B1": ["A1"], "C1": ["B1"], "D1": ["C1"]}
    order, cycles = topological_formula_order(graph)
    assert cycles == [["A1", "B1"]]
    assert order == []


def test_two_disjoint_cycles_stay_separate():
    graph = {"A1": ["B1"], "B1": ["A1"], "C1": ["D1"], "D1": ["C1"], "E1": []}
    order, cycles = topological_formula_order(graph)
    assert cycles == [["A1", "B1"], ["C1", "D1"]]
    assert order == ["E1"]
    assert "E1" not in {node for cycle in cycles for node in cycle}


def test_duplicate_precedent_edge_is_one_edge():
    graph = {"B1": ["A1", "A1"], "A1": []}
    order, cycles = topological_formula_order(graph)
    assert cycles == []
    assert order == ["A1", "B1"]


def test_self_loop_is_a_cycle():
    graph = {"A1": ["A1"], "B1": []}
    order, cycles = topological_formula_order(graph)
    assert cycles == [["A1"]]
    assert order == ["B1"]


def test_order_respects_edges_and_is_deterministic():
    graph = {
        "A1": [],
        "B1": ["A1"],
        "C1": ["A1"],
        "D1": ["B1", "C1"],
        "E1": ["D1"],
    }
    order1, cycles1 = topological_formula_order(graph)
    order2, cycles2 = topological_formula_order(graph)
    assert order1 == order2
    assert cycles1 == cycles2 == []
    for dependent, precedents in graph.items():
        if dependent in order1:
            dep_idx = order1.index(dependent)
            for prec in precedents:
                if prec in order1:
                    assert order1.index(prec) < dep_idx


def test_deep_cycle_does_not_recurse():
    graph = {f"A{i}": [f"A{i-1}"] for i in range(1, 2000)}
    graph["A0"] = ["A1999"]
    order, cycles = topological_formula_order(graph)
    assert order == []
    assert len(cycles) == 1
    assert len(cycles[0]) == 2000
