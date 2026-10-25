# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula dependency graph: precedents, topological order, cycle detection."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from plugin.calc.spreadsheet_import.models import FORMULA_LIKE_TYPES, CellRecord, SheetModel

if TYPE_CHECKING:
    from collections.abc import Iterator


_RANGE_REF_PATTERN = re.compile(r"\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?", re.IGNORECASE)


def extract_range_refs(formula: str) -> list[str]:
    """Extract same-sheet range/cell tokens (``A1`` or ``A1:B2``) left-to-right."""
    if not formula:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _RANGE_REF_PATTERN.finditer(formula.upper()):
        c1, r1, c2, r2 = match.group(1), match.group(2), match.group(3), match.group(4)
        if c2 is not None and r2 is not None:
            ref = f"{c1}{r1}:{c2}{r2}"
        else:
            ref = f"{c1}{r1}"
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def formula_like_addresses(cells: dict[str, CellRecord]) -> list[str]:
    """Addresses of cells that participate in the formula dependency DAG."""
    return sorted(addr for addr, cell in cells.items() if cell.type in FORMULA_LIKE_TYPES)


def build_dependency_graph(model: SheetModel) -> dict[str, list[str]]:
    """Map each formula-like cell to in-scope precedent addresses it depends on."""
    scope = frozenset(model.cells)
    graph: dict[str, list[str]] = {}
    for addr in formula_like_addresses(model.cells):
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

    for root in sorted(nodes):
        if root in index:
            continue
        index[root] = counter
        low[root] = counter
        counter += 1
        scc_stack.append(root)
        on_stack.add(root)
        root_neighbors = iter(sorted({p for p in graph.get(root, ()) if p in nodes}))
        work_stack: list[tuple[str, Iterator[str]]] = [(root, root_neighbors)]

        while work_stack:
            node, neighbors_iter = work_stack[-1]
            for prec in neighbors_iter:
                if prec not in index:
                    index[prec] = counter
                    low[prec] = counter
                    counter += 1
                    scc_stack.append(prec)
                    on_stack.add(prec)
                    prec_neighbors = iter(sorted({p for p in graph.get(prec, ()) if p in nodes}))
                    work_stack.append((prec, prec_neighbors))
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
