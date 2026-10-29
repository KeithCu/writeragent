# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula dependency graph: precedents, topological order, cycle detection."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING

from plugin.calc.spreadsheet_import.models import FORMULA_LIKE_TYPES, SheetModel

if TYPE_CHECKING:
    from collections.abc import Iterator


def build_dependency_graph(model: SheetModel) -> dict[str, list[str]]:
    """Map each formula-like cell to in-scope precedent addresses it depends on."""
    scope = frozenset(model.cells)
    graph: dict[str, list[str]] = {}
    for addr in sorted(addr for addr, cell in model.cells.items() if cell.type in FORMULA_LIKE_TYPES):
        cell = model.cells[addr]
        deps = [p for p in cell.precedents if p in scope and model.cells[p].type != "empty"]
        graph[addr] = deps
    return graph


def topological_formula_order(graph: dict[str, list[str]]) -> tuple[list[str], list[list[str]]]:
    """Return (acyclic_topo_order, circular_groups).

    Only true cycle members are listed in *circular_groups* (later phases report
    ``CIRCULAR_REF`` for those). Cells that merely depend on a cycle are omitted
    from both *circular_groups* and *acyclic_topo_order* (they cannot be ordered,
    but convert normally as downstream formulas).
    """
    nodes = set(graph)
    # Edge: precedent -> dependent (topo sort: precedents first).
    successors: dict[str, set[str]] = defaultdict(set)
    in_degree: dict[str, int] = {node: 0 for node in nodes}

    for dependent, precedents in graph.items():
        # Deduplicate precedents so multiple references to the same cell count as one edge
        # and do not over-increment in_degree into phantom cycles.
        for prec in set(precedents):
            if prec not in nodes:
                continue
            successors[prec].add(dependent)
            in_degree[dependent] += 1

    queue: deque[str] = deque(sorted(n for n in nodes if in_degree[n] == 0))
    ordered: list[str] = []

    while queue:
        node = queue.popleft()
        ordered.append(node)
        for succ in sorted(successors.get(node, ())):
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)

    if len(ordered) == len(nodes):
        return ordered, []

    # Kahn sort leaves both true cycle members and their downstream dependents in
    # the residual. Use Tarjan SCC to identify only true circular groups (size > 1
    # or self-referencing singletons), leaving downstream dependents un-flagged.
    remaining = nodes - set(ordered)
    cycle_groups = [
        group
        for group in _strongly_connected_groups(remaining, graph)
        if len(group) > 1 or group[0] in graph.get(group[0], ())
    ]
    return ordered, cycle_groups


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
        # Same neighbor set as the old root and prec pushes. graph.get and
        # `p in nodes` stay: a precedent can be on the sheet and still not be
        # a formula-graph node.
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
    """Populate ``formula_order`` and ``circular_groups`` on *model* in place."""
    graph = build_dependency_graph(model)
    order, cycles = topological_formula_order(graph)
    model.formula_order = order
    model.circular_groups = cycles
    return model
