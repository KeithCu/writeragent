# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula dependency graph: precedents, cycle detection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from plugin.calc.spreadsheet_import.models import FORMULA_LIKE_TYPES, SheetModel

if TYPE_CHECKING:
    from collections.abc import Iterator


def build_dependency_graph(model: SheetModel) -> dict[str, list[str]]:
    """Map each formula-like cell to in-scope precedent addresses it depends on."""
    graph: dict[str, list[str]] = {}
    for addr in sorted(addr for addr, cell in model.cells.items() if cell.type in FORMULA_LIKE_TYPES):
        cell = model.cells[addr]
        graph[addr] = [p for p in cell.precedents if p in model.cells and model.cells[p].type != "empty"]
    return graph


def find_circular_groups(graph: dict[str, list[str]]) -> list[list[str]]:
    """True cycles only: SCC size > 1, or a self-edge.

    A cell that merely depends on a cycle is a singleton SCC with no
    self-edge, so it is omitted and still converts.
    """
    nodes = set(graph)
    return [
        group
        for group in _strongly_connected_groups(nodes, graph)
        if len(group) > 1 or group[0] in graph.get(group[0], ())
    ]


def _strongly_connected_groups(nodes: set[str], graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan SCCs of *nodes*, following the precedent edges inside *nodes*."""
    # Iterative Tarjan SCC using an explicit work stack to prevent recursion limits
    # on sheets with deep formula dependency chains.
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    scc_stack: list[str] = []
    counter = 0
    groups: list[list[str]] = []

    def visit(node: str) -> Iterator[str]:
        nonlocal counter
        index[node] = counter
        low[node] = counter
        counter += 1
        scc_stack.append(node)
        on_stack.add(node)
        # graph.get and `p in nodes` stay: a precedent can be on the sheet and
        # still not be a formula-graph node.
        return iter(sorted({p for p in graph.get(node, ()) if p in nodes}))

    for root in sorted(nodes):
        if root in index:
            continue
        work_stack: list[tuple[str, Iterator[str]]] = [(root, visit(root))]

        while work_stack:
            node, neighbors_iter = work_stack[-1]
            for prec in neighbors_iter:
                if prec not in index:
                    work_stack.append((prec, visit(prec)))
                    break
                if prec in on_stack:
                    low[node] = min(low[node], index[prec])
            else:
                work_stack.pop()
                if work_stack:
                    parent = work_stack[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component: list[str] = []
                    while True:
                        member = scc_stack.pop()
                        on_stack.remove(member)
                        component.append(member)
                        if member == node:
                            break
                    groups.append(component)

    return sorted([sorted(group) for group in groups], key=lambda g: g[0])


def attach_graph_to_model(model: SheetModel) -> SheetModel:
    """Populate ``circular_groups`` on *model* in place."""
    model.circular_groups = find_circular_groups(build_dependency_graph(model))
    return model
