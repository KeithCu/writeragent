#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rename throwaway bare ``_`` bindings to ``_unused`` / ``_unused2`` / ....

Only rewrites Store/arg/except binding sites under first-party ``plugin/``
(skipping ``contrib/`` and ``lib/``) and ``compute_service/``. Leaves gettext
``_("…")``, private ``_foo``, and ``match``/``case`` wildcards alone.

Examples::

  python scripts/fix_bare_underscore_throwaways.py --dry-run
  python scripts/fix_bare_underscore_throwaways.py
  python scripts/fix_bare_underscore_throwaways.py --check   # make typecheck; exit 1 on any hit
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {"contrib", "lib", "__pycache__"}


def iter_targets(root: Path) -> list[Path]:
    out: list[Path] = []
    for base in (root / "plugin", root / "compute_service"):
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(p in SKIP_PARTS for p in path.parts):
                continue
            out.append(path)
    return sorted(out)


class _Collector(ast.NodeVisitor):
    """Collect (lineno, col_offset) of throwaway ``_`` bindings; skip Match*."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, int]] = []
        self._in_match = 0

    def _add_name(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store):
            self.hits.append((node.lineno, node.col_offset))

    def _walk_target(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            self._add_name(node)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                self._walk_target(elt)
        elif isinstance(node, ast.Starred):
            self._walk_target(node.value)
        else:
            self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        # Patterns may use '_' as wildcard — leave them alone.
        self.visit(node.subject)
        for case in node.cases:
            self._in_match += 1
            self.visit(case.pattern)
            if case.guard is not None:
                self.visit(case.guard)
            self._in_match -= 1
            for stmt in case.body:
                self.visit(stmt)

    def visit_Name(self, node: ast.Name) -> None:
        if self._in_match:
            return
        if node.id == "_" and isinstance(node.ctx, ast.Store):
            self.hits.append((node.lineno, node.col_offset))

    def visit_arg(self, node: ast.arg) -> None:
        if node.arg == "_":
            self.hits.append((node.lineno, node.col_offset))

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name == "_":
            # No name offset on ExceptHandler — sentinel for " as _" on the line.
            self.hits.append((node.lineno, -1))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            self._walk_target(t)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._walk_target(node.target)
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self._walk_target(node.target)
        self.visit(node.iter)
        for s in node.body:
            self.visit(s)
        for s in node.orelse:
            self.visit(s)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._walk_target(node.target)
        self.visit(node.iter)
        for s in node.body:
            self.visit(s)
        for s in node.orelse:
            self.visit(s)

    def visit_withitem(self, node: ast.withitem) -> None:
        self.visit(node.context_expr)
        if node.optional_vars is not None:
            self._walk_target(node.optional_vars)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self._walk_target(node.target)
        self.visit(node.iter)
        for if_ in node.ifs:
            self.visit(if_)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._walk_target(node.target)
        self.visit(node.value)


def _rename_on_line(line: str, cols: list[int]) -> str:
    """Replace bare ``_`` at given column offsets (and except `` as _``) with ``_unusedN``."""
    if not cols:
        return line
    cols_sorted = sorted(set(cols))
    spans: list[tuple[int, int, str]] = []
    n = 0
    for col in cols_sorted:
        n += 1
        name = "_unused" if n == 1 else f"_unused{n}"
        if col < 0:
            idx = line.find(" as _")
            if idx < 0:
                continue
            start = idx + 4
            if start < len(line) and line[start] == "_" and (
                start + 1 == len(line) or not (line[start + 1].isalnum() or line[start + 1] == "_")
            ):
                spans.append((start, start + 1, name))
            continue
        if col >= len(line) or line[col] != "_":
            continue
        if col + 1 < len(line) and (line[col + 1].isalnum() or line[col + 1] == "_"):
            continue
        if col > 0 and (line[col - 1].isalnum() or line[col - 1] == "_"):
            continue
        spans.append((col, col + 1, name))

    for start, end, name in sorted(spans, key=lambda s: s[0], reverse=True):
        line = line[:start] + name + line[end:]
    return line


def rewrite_source(source: str) -> tuple[str | None, list[tuple[int, str, str]]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return None, [(-1, "", f"SYNTAX_ERROR: {exc}")]

    coll = _Collector()
    coll.visit(tree)
    if not coll.hits:
        return None, []

    by_line: dict[int, list[int]] = {}
    for lineno, col in coll.hits:
        by_line.setdefault(lineno, []).append(col)

    lines = source.splitlines(keepends=True)
    diffs: list[tuple[int, str, str]] = []
    changed = False
    for lineno, cols in sorted(by_line.items()):
        if lineno < 1 or lineno > len(lines):
            continue
        raw = lines[lineno - 1]
        nl = ""
        body = raw
        if body.endswith("\r\n"):
            nl, body = "\r\n", body[:-2]
        elif body.endswith("\n"):
            nl, body = "\n", body[:-1]
        elif body.endswith("\r"):
            nl, body = "\r", body[:-1]
        new_body = _rename_on_line(body, cols)
        if new_body != body:
            changed = True
            diffs.append((lineno, body, new_body))
            lines[lineno - 1] = new_body + nl

    if not changed:
        return None, []
    return "".join(lines), diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    ap.add_argument("--check", action="store_true", help="Print changes without writing; exit 1 if any file needs one")
    ap.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repo root (default: parent of scripts/)",
    )
    args = ap.parse_args(argv)
    root = args.root.resolve()
    check: bool = args.check
    dry_run: bool = args.dry_run or check

    updated = 0
    for path in iter_targets(root):
        original = path.read_text(encoding="utf-8")
        rewritten, diffs = rewrite_source(original)
        if rewritten is None:
            if diffs and diffs[0][0] < 0:
                print(f"SKIP {path.relative_to(root)}: {diffs[0][2]}", file=sys.stderr)
            continue
        rel = path.relative_to(root)
        print(f"{'WOULD UPDATE' if dry_run else 'UPDATED'}: {rel}")
        for lineno, old, new in diffs:
            print(f"  L{lineno}: {old.strip()}")
            print(f"       -> {new.strip()}")
        if not dry_run:
            path.write_text(rewritten, encoding="utf-8")
        updated += 1
    print(f"{'Would update' if dry_run else 'Updated'} {updated} file(s)")
    if check and updated:
        print("Bare `_` is gettext; run scripts/fix_bare_underscore_throwaways.py to rename these.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
