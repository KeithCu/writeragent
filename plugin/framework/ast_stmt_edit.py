# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared AST edits for statement stripping (release strip) and AST rewriting.

Used by [`scripts/strip_code.py`](../../scripts/strip_code.py) to remove
assert statements, ``grammar_obs(...)``, logger ``.debug``/``.info``, and most
``print``/``pprint`` statements from production bundles.
Also exports AST offset and span helpers to runtime conversion modules.

Rules (one source of truth):

* Multi-line via ``lineno`` / ``end_lineno`` (1-based, matching ``ast``).
* If a non-``Module`` suite would be empty after removals, keep one indented
  ``pass`` — otherwise ``if cond:`` with an empty body is a SyntaxError.
* If siblings remain in the suite, delete the statement lines entirely.

Python 3.10+ compatible (build/release tooling). Stdlib ``ast`` only.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, ascii_bounded, deal


@deal.pre(lambda node, names: isinstance(names, frozenset) and len(names) <= DEAL_MAX_CMD_ARGS and all(ascii_bounded(n, DEAL_MAX_TOKEN) for n in names))
@deal.post(lambda result: isinstance(result, bool))
def is_name_call_expr(node: ast.Expr, names: frozenset[str]) -> bool:
    """True if *node* is an expression-statement call to one of *names*."""
    # crosshair: off
    # getattr: CrossHair may synthesize Expr() without .value; real ast.Expr always has it.
    value = getattr(node, "value", None) if isinstance(node, ast.Expr) else None
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    return isinstance(func, ast.Name) and func.id in names


def _discover_statements(
    source: str,
    should_remove: Callable[[ast.stmt], bool],
    *,
    skip_last_module_expr: bool = False,
) -> tuple[list[ast.stmt], dict[ast.AST, ast.AST]]:
    """Parse once; return ``(matching stmt nodes, child -> parent map)``.

    Shared by :func:`iter_matching_statements` and
    :func:`remove_statements` so there is one matching implementation.
    """
    tree = ast.parse(source or "")

    skip_node: ast.AST | None = None
    if skip_last_module_expr and isinstance(tree, ast.Module) and tree.body:
        last = tree.body[-1]
        if isinstance(last, ast.Expr):
            skip_node = last

    found: list[ast.stmt] = []
    parents: dict[ast.AST, ast.AST] = {}

    class FindVisitor(ast.NodeVisitor):
        def generic_visit(self, node: ast.AST) -> None:
            if isinstance(node, ast.stmt) and node is not skip_node and should_remove(node):
                found.append(node)
            for child in ast.iter_child_nodes(node):
                parents[child] = node
                self.visit(child)

    FindVisitor().visit(tree)
    return found, parents


def iter_matching_statements(source: str, should_remove: Callable[[ast.stmt], bool], *, skip_last_module_expr: bool = False) -> list[ast.stmt]:
    """Discover matching statements in *source*."""
    found, _parents = _discover_statements(source, should_remove, skip_last_module_expr=skip_last_module_expr)
    return found


def iter_matching_expr_statements(source: str, should_remove: Callable[[ast.Expr], bool], *, skip_last_module_expr: bool = False) -> list[ast.Expr]:
    """Discover matching expression statements in *source*.

    When *skip_last_module_expr* is True, never yield ``module.body[-1]`` if it
    is an ``ast.Expr`` (Excel/Jupyter last-expression egress — keep that ``xl``
    so it can rewrite to ``data``).
    """
    found, _parents = _discover_statements(
        source,
        lambda node: isinstance(node, ast.Expr) and should_remove(node),
        skip_last_module_expr=skip_last_module_expr,
    )
    return [s for s in found if isinstance(s, ast.Expr)]


def _cut_same_line_span(raw: str, start: int, end: int) -> str:
    """Remove ``raw[start:end]`` and one neighboring ``;`` separator.

    Indent before the cut is kept when the keeper sits only on the right
    (``    print(1); keep()`` must not lose the suite indent).
    """
    prefix = raw[:start]
    suffix = raw[end:]
    indent = prefix[: len(prefix) - len(prefix.lstrip(" \t"))]
    left = prefix.rstrip()
    if left.endswith(";"):
        left = left[:-1].rstrip()
    right = suffix.lstrip(" \t")
    if right.startswith(";"):
        right = right[1:].lstrip(" \t")
    if left.strip() and right.strip():
        return left + "; " + right
    if left.strip():
        return left + right
    if right.strip():
        newline = "\n" if right.endswith("\n") else ""
        body = right[:-1] if newline else right
        return indent + body.lstrip() + newline
    return left + right


def _get_container(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> list[ast.stmt] | None:
    """Suite list (``body`` / ``orelse`` / ``finalbody`` / except body) containing *node*."""
    # crosshair: off
    parent = parent_map.get(node)
    if not parent:
        return None
    # body also covers ExceptHandler and match_case.
    for attr in ("body", "orelse", "finalbody"):
        if hasattr(parent, attr):
            container = getattr(parent, attr)
            if isinstance(container, list) and node in container:
                return container
    return None


@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str) and isinstance(result[1], int) and result[1] >= 0)
def remove_statements(source: str, should_remove: Callable[[ast.stmt], bool], *, pass_comment: str = "", skip_last_module_expr: bool = False) -> tuple[str, int]:
    """Remove matching statement nodes from *source*.

    Uses ``_discover_statements`` — the same function
    :func:`iter_matching_statements` wraps — then applies line edits.
    Dry-run callers use ``iter``; edit callers use this. One matching path.
    Returns ``(new_source, removed_count)``.
    """
    # Callable argument plus a parse loop hangs deep check.
    # crosshair: off
    src = source or ""
    nodes_to_remove, parent_map = _discover_statements(src, should_remove, skip_last_module_expr=skip_last_module_expr)
    if not nodes_to_remove:
        return src, 0

    # Exact AST lines. splitlines() also breaks on \x0c, \x0b, U+2028, U+2029
    # (see line_starts_exact), which shifts AST line numbers and deletes the
    # wrong line. line_starts_exact splits only on \r\n, \r, \n.
    _starts = line_starts_exact(src)
    lines = [src[start:end] for start, end in zip(_starts, _starts[1:] + [len(src)])]
    if lines and not lines[-1]:
        lines.pop()  # trailing newline: match splitlines(), which has no empty last element
    remove_set = set(nodes_to_remove)
    replacements: dict[int, str] = {}
    to_delete: set[int] = set()
    # Spans are columns on the original line. Applying the first cut in place
    # shifted the next Expr's col_offset, so ``print(1); print(2); keep()``
    # became ``print(2)`` (keep deleted).
    same_line_spans: dict[int, list[tuple[int, int]]] = {}

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
            start_col = getattr(node, "col_offset", None)
            # ``if cond: debug()`` shares the header line. Replacing the
            # whole line with ``pass`` drops the suite header and is invalid
            # or changes the program. Splice ``pass`` after the colon.
            if start_line == end_line and start_col is not None and start_col > len(indent):
                prefix = lines[first_idx][:start_col].rstrip()
                replacements[first_idx] = f"{prefix} pass{comment}\n"
            else:
                replacements[first_idx] = f"{indent}pass{comment}\n"
            for idx in range(first_idx + 1, last_idx + 1):
                to_delete.add(idx)
        else:
            end_col = getattr(node, "end_col_offset", None)
            start_col = getattr(node, "col_offset", None)
            # ``print(1); keep()`` shares a line. Deleting the whole line drops keep().
            if (
                start_line == end_line
                and start_col is not None
                and end_col is not None
                and first_idx not in to_delete
                and first_idx not in replacements
            ):
                raw = lines[first_idx]
                prefix = raw[:start_col]
                suffix = raw[end_col:]
                if prefix.strip() or suffix.strip():
                    same_line_spans.setdefault(first_idx, []).append((start_col, end_col))
                    continue
            for idx in range(first_idx, last_idx + 1):
                to_delete.add(idx)

    # One rewrite per line, rightmost span first, so earlier columns still
    # address the original text. A line touched by several calls is changed once.
    for idx, spans in same_line_spans.items():
        if idx in to_delete or idx in replacements:
            continue
        raw = lines[idx]
        for start, end in sorted(spans, key=lambda span: span[0], reverse=True):
            if start < 0 or end > len(raw) or start > end:
                continue
            raw = _cut_same_line_span(raw, start, end)
        if raw.strip():
            lines[idx] = raw
        else:
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


@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str) and isinstance(result[1], int) and result[1] >= 0)
def remove_expr_statements(source: str, should_remove: Callable[[ast.Expr], bool], *, pass_comment: str = "", skip_last_module_expr: bool = False) -> tuple[str, int]:
    """Remove matching expression-statement nodes from *source*.

    Uses ``remove_statements`` filtering for ``ast.Expr`` nodes.
    Returns ``(new_source, removed_count)``.
    """
    return remove_statements(
        source,
        lambda node: isinstance(node, ast.Expr) and should_remove(node),
        pass_comment=pass_comment,
        skip_last_module_expr=skip_last_module_expr,
    )


# ast_source_offset splits lines on \r\n, \r, or \n, matching
# Python AST line numbering. str.splitlines() also splits on form
# feed (\x0c), vertical tab (\x0b), U+2028, U+2029, and other
# Unicode breaks the lexer does not. Line-start offsets are
# precomputed for O(1) random-access lookups.
_AST_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def line_starts_exact(src: str) -> list[int]:
    """Find line start character offsets splitting only on \\r\\n, \\r, or \\n."""
    starts = [0]
    for m in _AST_LINE_BREAK_RE.finditer(src):
        starts.append(m.end())
    return starts


def ast_source_offset(
    src: str,
    lineno: int,
    col: int,
    *,
    line_starts: list[int] | None = None,
) -> int:
    """Map AST ``(lineno, col_offset)`` to an absolute character index in *src*.

    On Python 3.8+, ``col_offset`` / ``end_col_offset`` are UTF-8 *byte* offsets
    within the line — not Unicode character indices. Convert before slicing *src*
    so a non-ASCII prefix cannot shift the rewrite window.
    """
    if lineno < 1 or col < 0:
        return -1
    if line_starts is None:
        line_starts = line_starts_exact(src)
    if lineno > len(line_starts):
        return -1
    line_start = line_starts[lineno - 1]
    line_end = line_starts[lineno] if lineno < len(line_starts) else len(src)
    line = src[line_start:line_end]
    raw = line.encode("utf-8")
    if col > len(raw):
        return -1
    # If *col* landed mid-codepoint, back up to a valid UTF-8 boundary.
    while col > 0 and col < len(raw) and (raw[col] & 0xC0) == 0x80:
        col -= 1
    return line_start + len(raw[:col].decode("utf-8"))


def node_span(node: ast.AST, src: str, line_starts: list[int]) -> tuple[int, int]:
    """Map an AST node's position to [start, end) character offsets in *src*."""
    lineno = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    end_lineno = getattr(node, "end_lineno", lineno)
    end_col = getattr(node, "end_col_offset", col)
    if lineno is None or col is None or end_lineno is None or end_col is None:
        return -1, -1
    start = ast_source_offset(src, lineno, col, line_starts=line_starts)
    end = ast_source_offset(src, end_lineno, end_col, line_starts=line_starts)
    return start, end


