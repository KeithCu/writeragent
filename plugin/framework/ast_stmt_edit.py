# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared AST edits for expression-statement call sites (release strip + Excel PY).

Used by:

* [`scripts/strip_code.py`](../../scripts/strip_code.py) — remove ``grammar_obs(...)``
  statements from production bundles.
* [`plugin/calc/excel_py_convert/to_dag.py`](../calc/excel_py_convert/to_dag.py) —
  turn discarded ``xl(...)`` statements (e.g. under ``if``) into ``pass`` /
  delete them so they do not become ``data`` or fail-closed dynamic refs.

Rules (one source of truth):

* Only ``ast.Expr`` whose value is the matched call (not assignments / args).
* Multi-line via ``lineno`` / ``end_lineno`` (0-based inclusive line ranges).
* If a non-``Module`` suite would be empty after removals, keep one indented
  ``pass`` — otherwise ``if cond:`` with an empty body is a SyntaxError.
* If siblings remain in the suite, delete the statement lines entirely.

Python 3.10+ compatible (build/release tooling). Stdlib ``ast`` only.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


from plugin.framework.deal_shim import deal


@deal.post(lambda result: isinstance(result, bool))
def is_name_call_expr(node: ast.Expr, names: frozenset[str]) -> bool:
    """True if *node* is an expression-statement call to one of *names*."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return isinstance(func, ast.Name) and func.id in names


def _discover_expr_statements(
    source: str,
    should_remove: Callable[[ast.Expr], bool],
    *,
    skip_last_module_expr: bool = False,
) -> tuple[ast.AST, list[ast.Expr]]:
    """Parse once; return ``(tree, matching Expr nodes)``.

    Shared by :func:`iter_matching_expr_statements` and
    :func:`remove_expr_statements` so there is one matching implementation.
    """
    tree = ast.parse(source or "")

    skip_node: ast.AST | None = None
    if skip_last_module_expr and isinstance(tree, ast.Module) and tree.body:
        last = tree.body[-1]
        if isinstance(last, ast.Expr):
            skip_node = last

    found: list[ast.Expr] = []

    class FindVisitor(ast.NodeVisitor):
        def visit_Expr(self, node: ast.Expr) -> None:
            if skip_node is not None and node is skip_node:
                self.generic_visit(node)
                return
            if should_remove(node):
                found.append(node)
            self.generic_visit(node)

    FindVisitor().visit(tree)
    return tree, found


def iter_matching_expr_statements(
    source: str,
    should_remove: Callable[[ast.Expr], bool],
    *,
    skip_last_module_expr: bool = False,
) -> list[ast.Expr]:
    """Discover matching expression statements in *source*.

    When *skip_last_module_expr* is True, never yield ``module.body[-1]`` if it
    is an ``ast.Expr`` (Excel/Jupyter last-expression egress — keep that ``xl``
    so it can rewrite to ``data``).
    """
    _tree, found = _discover_expr_statements(
        source,
        should_remove,
        skip_last_module_expr=skip_last_module_expr,
    )
    return found


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent_map: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[child] = node
    return parent_map


def _get_container(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> list[ast.stmt] | None:
    """Suite list (``body`` / ``orelse`` / ``finalbody`` / except body) containing *node*."""
    parent = parent_map.get(node)
    if not parent:
        return None
    for attr in ("body", "orelse", "finalbody"):
        if hasattr(parent, attr):
            container = getattr(parent, attr)
            if isinstance(container, list) and node in container:
                return container
    if isinstance(parent, ast.Try):
        for handler in parent.handlers:
            if node in handler.body:
                return handler.body
    return None


@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str) and isinstance(result[1], int) and result[1] >= 0)
def remove_expr_statements(
    source: str,
    should_remove: Callable[[ast.Expr], bool],
    *,
    pass_comment: str = "",
    skip_last_module_expr: bool = False,
) -> tuple[str, int]:
    """Remove matching expression-statement nodes from *source*.

    Uses ``_discover_expr_statements`` — the same function
    :func:`iter_matching_expr_statements` wraps — then applies line edits.
    Dry-run callers use ``iter``; edit callers use this. One matching path.
    Returns ``(new_source, removed_count)``.
    """
    src = source or ""
    tree, nodes_to_remove = _discover_expr_statements(
        src,
        should_remove,
        skip_last_module_expr=skip_last_module_expr,
    )
    if not nodes_to_remove:
        return src, 0

    lines = src.splitlines(keepends=True)
    parent_map = _parent_map(tree)
    remove_set = set(nodes_to_remove)
    replacements: dict[int, str] = {}
    to_delete: set[int] = set()

    for node in nodes_to_remove:
        start_line = node.lineno
        end_line = getattr(node, "end_lineno", None) or start_line
        first_idx = start_line - 1
        last_idx = end_line - 1
        if first_idx < 0 or last_idx >= len(lines):
            continue
        original_line = lines[first_idx]
        indent = original_line[: len(original_line) - len(original_line.lstrip())]

        # Empty non-module suite → keep ``pass`` or ``if cond:`` is a SyntaxError.
        # (Same rule as release grammar_obs stripping in scripts/strip_code.py.)
        container = _get_container(node, parent_map)
        needs_pass = False
        if container and not isinstance(parent_map.get(node), ast.Module):
            remaining = [s for s in container if s not in remove_set]
            if not remaining:
                first_removed = next(s for s in container if s in remove_set)
                if node is first_removed:
                    needs_pass = True

        if needs_pass:
            comment = f"  # {pass_comment}" if pass_comment else ""
            replacements[first_idx] = f"{indent}pass{comment}\n"
            for idx in range(first_idx + 1, last_idx + 1):
                to_delete.add(idx)
        else:
            for idx in range(first_idx, last_idx + 1):
                to_delete.add(idx)

    new_lines: list[str] = []
    for i, line in enumerate(lines):
        if i in to_delete and i not in replacements:
            continue
        if i in replacements:
            new_lines.append(replacements[i])
        else:
            new_lines.append(line)

    return "".join(new_lines), len(nodes_to_remove)
