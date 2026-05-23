#!/usr/bin/env python3
# WriterAgent — AST-based debug code stripping tool
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""AST-based utility to strip debug statements from production code.

Removes calls to print, log.debug, log.info, and grammar_obs from Python files.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys


EXCLUDED_STRIP_PATTERNS: list[str] = [
    "plugin/testing_runner.py",
    "plugin/tests/",
    "tests/",
    "plugin/contrib/smolagents/monitoring.py",
]


def should_skip_strip(rel_path: str) -> bool:
    """Determine if a project-relative Python file should be skipped during stripping.

    Skipping specific files avoids breaking test runners or silent web-research console traces.
    """
    for pattern in EXCLUDED_STRIP_PATTERNS:
        if pattern.endswith("/"):
            if rel_path.startswith(pattern):
                return True
        else:
            if rel_path == pattern:
                return True
    return False


def _is_deal_decorator(node: ast.AST) -> bool:
    """Determine if an AST node is a decorator under the 'deal' namespace (e.g. @deal.pre)."""
    curr = node
    if isinstance(curr, ast.Call):
        curr = curr.func
    while isinstance(curr, ast.Attribute):
        curr = curr.value
    return isinstance(curr, ast.Name) and curr.id == "deal"


def strip_production_code(bundle_path: str, dry_run: bool = False) -> None:
    """Remove print, log.debug, log.info, and grammar_obs calls from Python files in the bundle.

    Uses AST to find line ranges and removes them while preserving comments.
    """
    action = "Dry run: would strip" if dry_run else "Stripping"
    print(f"  {action} debug/obs code from {bundle_path} using AST...")

    for root, _, filenames in os.walk(bundle_path):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel_path = os.path.relpath(path, bundle_path).replace(os.sep, "/")
            if should_skip_strip(rel_path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                    lines = content.splitlines(keepends=True)

                tree = ast.parse(content)

                # 1. Build parent map and identify all nodes to remove
                parent_map: dict[ast.AST, ast.AST] = {}
                for node in ast.walk(tree):
                    for child in ast.iter_child_nodes(node):
                        parent_map[child] = node

                nodes_to_remove: list[ast.AST] = []

                class FindVisitor(ast.NodeVisitor):
                    def visit_Expr(self, node: ast.Expr) -> None:
                        if isinstance(node.value, ast.Call):
                            call = node.value
                            func_name = None
                            if isinstance(call.func, ast.Name):
                                func_name = call.func.id
                            elif isinstance(call.func, ast.Attribute):
                                if isinstance(call.func.value, ast.Name) and call.func.value.id in ("log", "logger"):
                                    func_name = f"{call.func.value.id}.{call.func.attr}"

                            if func_name in (
                                "print", "pprint",
                                "log.debug", "log.info",
                                "logger.debug", "logger.info",
                                "grammar_obs", "_grammar_obs"
                            ):
                                nodes_to_remove.append(node)
                        self.generic_visit(node)

                    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                        for dec in node.decorator_list:
                            if _is_deal_decorator(dec):
                                nodes_to_remove.append(dec)
                        self.generic_visit(node)

                    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                        for dec in node.decorator_list:
                            if _is_deal_decorator(dec):
                                nodes_to_remove.append(dec)
                        self.generic_visit(node)

                    def visit_Try(self, node: ast.Try) -> None:
                        # Check if any statement in the body is an import of 'deal' or defines 'deal'
                        for stmt in node.body:
                            if isinstance(stmt, ast.Import) and any(alias.name == "deal" for alias in stmt.names):
                                nodes_to_remove.append(node)
                                return
                            if isinstance(stmt, ast.ImportFrom) and stmt.module == "deal":
                                nodes_to_remove.append(node)
                                return
                            if isinstance(stmt, ast.Assign):
                                for target in stmt.targets:
                                    if isinstance(target, ast.Name) and target.id == "deal":
                                        nodes_to_remove.append(node)
                                        return
                            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "deal":
                                nodes_to_remove.append(node)
                                return
                        self.generic_visit(node)

                    def visit_Import(self, node: ast.Import) -> None:
                        if any(alias.name == "deal" for alias in node.names):
                            nodes_to_remove.append(node)
                        self.generic_visit(node)

                    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                        if node.module == "deal":
                            nodes_to_remove.append(node)
                        self.generic_visit(node)

                    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                        if isinstance(node.target, ast.Name) and node.target.id == "deal":
                            nodes_to_remove.append(node)
                        self.generic_visit(node)

                    def visit_Assign(self, node: ast.Assign) -> None:
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == "deal":
                                nodes_to_remove.append(node)
                        self.generic_visit(node)

                FindVisitor().visit(tree)
                if not nodes_to_remove:
                    continue

                # 2. Decide for each node: delete or replace with pass?
                replacements: dict[int, str] = {}  # line_index -> new_text
                to_delete: set[int] = set()        # line_index

                # Helper to find which list a node belongs to
                def get_container(node: ast.AST) -> list[ast.AST] | None:
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

                for node in nodes_to_remove:
                    start = getattr(node, "lineno")
                    end = getattr(node, "end_lineno", start)
                    idx = start - 1
                    original_line = lines[idx]
                    indent = original_line[:len(original_line) - len(original_line.lstrip())]

                    if dry_run:
                        rel_p = os.path.relpath(path, bundle_path)
                        snippet = original_line.strip()
                        if end > start:
                            snippet += f" ... (spans {end - start + 1} lines)"
                        print(f"    [DryRun] {rel_p}: L{start}-{end}: {snippet}")
                        continue

                    # Determine if we NEED a pass
                    container = get_container(node)
                    needs_pass = False
                    if container and not isinstance(parent_map.get(node), ast.Module):
                        # Count how many in this container are NOT being removed
                        remaining = [s for s in container if s not in nodes_to_remove]
                        if not remaining:
                            # This block will be empty. We need EXACTLY ONE pass.
                            # We'll pick the first node in the container that's being removed.
                            first_removed = next(s for s in container if s in nodes_to_remove)
                            if node == first_removed:
                                needs_pass = True

                    if needs_pass:
                        replacements[idx] = f"{indent}pass  # stripped\n"
                    else:
                        to_delete.add(idx)

                    # Mark multi-line parts for deletion
                    for i in range(start, end):
                        to_delete.add(i)

                if dry_run:
                    continue

                # 3. Reconstruct the file
                new_lines: list[str] = []
                for i, line in enumerate(lines):
                    if i in to_delete and i not in replacements:
                        continue
                    if i in replacements:
                        new_lines.append(replacements[i])
                    else:
                        new_lines.append(line)

                with open(path, "w", encoding="utf-8") as f:
                    f.write("".join(new_lines))

            except Exception as e:
                # Some files might be vendored or have weird syntax; skip with warning
                if "match" not in str(e):  # Ignore expected match/case issues if using older python to build
                    print(f"    SKIPPING {fn}: {e}")

    print("  Done: Stripped debug/obs calls from bundle.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Strip debug/obs statements from python files in a directory.")
    parser.add_argument("bundle_path", help="Path to the directory containing python files to strip")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be stripped without deleting")
    args = parser.parse_args()

    if not os.path.isdir(args.bundle_path):
        print(f"Error: {args.bundle_path} is not a valid directory.", file=sys.stderr)
        return 1

    strip_production_code(args.bundle_path, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
