# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula dependency graph: precedents, topological order, cycle detection."""

from __future__ import annotations

import re
from collections import defaultdict, deque

from plugin.calc.spreadsheet_import.models import FORMULA_LIKE_TYPES, CellRecord, SheetModel


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

    Formula cells in cycles are listed in *circular_groups* and omitted from
    *acyclic_topo_order* (later phases report ``CIRCULAR_REF`` for those).
    """
    nodes = set(graph)
    # Edge: precedent -> dependent (topo sort: precedents first).
    successors: dict[str, set[str]] = defaultdict(set)
    in_degree: dict[str, int] = {node: 0 for node in nodes}

    for dependent, precedents in graph.items():
        for prec in precedents:
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

    remaining = nodes - set(ordered)
    circular_groups = _extract_cycle_groups(remaining, graph)
    return ordered, circular_groups


def _extract_cycle_groups(remaining: set[str], graph: dict[str, list[str]]) -> list[list[str]]:
    """Group remaining cyclic nodes into connected components (one cycle per group)."""
    # Build undirected adjacency among remaining nodes for component peel.
    adj: dict[str, set[str]] = {n: set() for n in remaining}
    for node in remaining:
        for prec in graph.get(node, []):
            if prec in remaining:
                adj[node].add(prec)
                adj[prec].add(node)

    groups: list[list[str]] = []
    unvisited = set(remaining)
    while unvisited:
        start = min(unvisited)
        stack = [start]
        component: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in component:
                continue
            component.add(cur)
            for nb in adj[cur]:
                if nb not in component:
                    stack.append(nb)
        unvisited -= component
        groups.append(sorted(component))
    return groups


def attach_graph_to_model(model: SheetModel) -> SheetModel:
    """Populate ``formula_order`` and ``circular_groups`` on *model* in place."""
    graph = build_dependency_graph(model)
    order, cycles = topological_formula_order(graph)
    model.formula_order = order
    model.circular_groups = cycles
    return model
