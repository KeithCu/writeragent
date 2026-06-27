#!/usr/bin/env python3
# WriterAgent — AST-based grammar_obs stripping tool
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""AST-based utility to strip ``grammar_obs(...)`` call sites from production bundles.

Only removes standalone expression-statement calls to ``grammar_obs`` or ``_grammar_obs``.
Imports, re-exports, the ``grammar_obs.py`` module, and ``emit_grammar_status`` are left intact.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

GRAMMAR_OBS_CALL_NAMES: frozenset[str] = frozenset({"grammar_obs", "_grammar_obs"})

EXCLUDED_STRIP_PATTERNS: list[str] = [
    "plugin/testing_runner.py",
    "plugin/tests/",
    "tests/",
]


def should_skip_strip(rel_path: str) -> bool:
    """Determine if a project-relative Python file should be skipped during stripping."""
    for pattern in EXCLUDED_STRIP_PATTERNS:
        if pattern.endswith("/"):
            if rel_path.startswith(pattern):
                return True
        elif rel_path == pattern:
            return True
    return False


def _is_grammar_obs_call(node: ast.Expr) -> bool:
    """True if ``node`` is an expression-statement call to grammar_obs / _grammar_obs."""
    if not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return isinstance(func, ast.Name) and func.id in GRAMMAR_OBS_CALL_NAMES


def strip_grammar_obs_calls(bundle_path: str, dry_run: bool = False) -> None:
    """Remove ``grammar_obs(...)`` / ``_grammar_obs(...)`` expression statements from Python files.

    Uses AST line ranges (including multi-line calls). Inserts ``pass`` when stripping would
    leave an otherwise empty block.
    """
    action = "Dry run: would strip" if dry_run else "Stripping"
    print(f"  {action} grammar_obs calls from {bundle_path} using AST...")

    for root, _, filenames in os.walk(bundle_path):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel_path = os.path.relpath(path, bundle_path).replace(os.sep, "/")
            if should_skip_strip(rel_path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                    lines = content.splitlines(keepends=True)

                tree = ast.parse(content)

                parent_map: dict[ast.AST, ast.AST] = {}
                for node in ast.walk(tree):
                    for child in ast.iter_child_nodes(node):
                        parent_map[child] = node

                nodes_to_remove: list[ast.Expr] = []

                class FindVisitor(ast.NodeVisitor):
                    def visit_Expr(self, node: ast.Expr) -> None:
                        if _is_grammar_obs_call(node):
                            nodes_to_remove.append(node)
                        self.generic_visit(node)

                FindVisitor().visit(tree)
                if not nodes_to_remove:
                    continue

                replacements: dict[int, str] = {}
                to_delete: set[int] = set()

                def get_container(node: ast.AST) -> list[ast.stmt] | None:
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
                    start_line = node.lineno
                    end_line = getattr(node, "end_lineno", None) or start_line
                    first_idx = start_line - 1
                    last_idx = end_line - 1
                    original_line = lines[first_idx]
                    indent = original_line[: len(original_line) - len(original_line.lstrip())]

                    if dry_run:
                        rel_p = os.path.relpath(path, bundle_path)
                        snippet = original_line.strip()
                        if end_line > start_line:
                            snippet += f" ... (spans {end_line - start_line + 1} lines)"
                        print(f"    [DryRun] {rel_p}: L{start_line}-{end_line}: {snippet}")
                        continue

                    container = get_container(node)
                    needs_pass = False
                    if container and not isinstance(parent_map.get(node), ast.Module):
                        remaining = [s for s in container if s not in nodes_to_remove]
                        if not remaining:
                            first_removed = next(s for s in container if s in nodes_to_remove)
                            if node is first_removed:
                                needs_pass = True

                    if needs_pass:
                        replacements[first_idx] = f"{indent}pass  # stripped grammar_obs\n"
                        for idx in range(first_idx + 1, last_idx + 1):
                            to_delete.add(idx)
                    else:
                        for idx in range(first_idx, last_idx + 1):
                            to_delete.add(idx)

                if dry_run:
                    continue

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
                if "match" not in str(e):
                    print(f"    SKIPPING {fn}: {e}")

    print("  Done: Stripped grammar_obs calls from bundle.")


def strip_main_thread_only_decorators(bundle_path: str, dry_run: bool = False) -> None:
    """Remove ``@main_thread_only`` decorators from python files."""
    action = "Dry run: would strip" if dry_run else "Stripping"
    print(f"  {action} main_thread_only decorators from {bundle_path} using AST...")

    for root, _, filenames in os.walk(bundle_path):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel_path = os.path.relpath(path, bundle_path).replace(os.sep, "/")
            if should_skip_strip(rel_path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                    lines = content.splitlines(keepends=True)

                if "main_thread_only" not in content:
                    continue

                tree = ast.parse(content)

                decorators_to_remove: list[ast.AST] = []

                class FindVisitor(ast.NodeVisitor):
                    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                        self.check_decorators(node)
                        self.generic_visit(node)

                    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                        self.check_decorators(node)
                        self.generic_visit(node)

                    def check_decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                        for dec in node.decorator_list:
                            if isinstance(dec, ast.Name) and dec.id == "main_thread_only":
                                decorators_to_remove.append(dec)

                FindVisitor().visit(tree)
                if not decorators_to_remove:
                    continue

                to_delete: set[int] = set()

                for node in decorators_to_remove:
                    start_line = node.lineno
                    end_line = getattr(node, "end_lineno", None) or start_line
                    first_idx = start_line - 1
                    last_idx = end_line - 1
                    original_line = lines[first_idx]

                    if dry_run:
                        rel_p = os.path.relpath(path, bundle_path)
                        snippet = original_line.strip()
                        print(f"    [DryRun] {rel_p}: L{start_line}-{end_line}: {snippet}")
                        continue

                    for idx in range(first_idx, last_idx + 1):
                        to_delete.add(idx)

                if dry_run:
                    continue

                new_lines: list[str] = []
                for i, line in enumerate(lines):
                    if i in to_delete:
                        continue
                    new_lines.append(line)

                with open(path, "w", encoding="utf-8") as f:
                    f.write("".join(new_lines))

            except Exception as e:
                if "match" not in str(e):
                    print(f"    SKIPPING {fn}: {e}")

    print("  Done: Stripped main_thread_only decorators from bundle.")


def replace_thread_guard_implementation(bundle_path: str, dry_run: bool = False) -> None:
    """Replace plugin/framework/thread_guard.py with a minimal, no-op stub implementation."""
    target_file = os.path.join(bundle_path, "plugin", "framework", "thread_guard.py")
    if not os.path.exists(target_file):
        return

    stubs = '''# Minimal stubs for production/release bundles to remove runtime check overhead.
GUARD_ON = False

def assert_main_thread(what: str) -> None:
    pass

def main_thread_only(fn):
    return fn

def set_background_task(name: str) -> None:
    pass

def get_background_task_name() -> str | None:
    return None

def set_designated_main_thread(thread) -> None:
    pass

def get_designated_main_thread():
    return None

def on_main_thread() -> bool:
    return True

def _wrap_uno(obj):
    return obj

def _unwrap_uno(obj):
    return obj

def guard_uno(obj):
    return obj
'''
    action = "Dry run: would replace" if dry_run else "Replacing"
    print(f"  {action} {target_file} with minimal stubs...")
    if not dry_run:
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(stubs)


def strip_production_code(bundle_path: str, dry_run: bool = False) -> None:
    """Release-bundle entry point: strip ``grammar_obs`` call sites, ``main_thread_only`` decorators, and stub ``thread_guard.py``."""
    strip_grammar_obs_calls(bundle_path, dry_run=dry_run)
    strip_main_thread_only_decorators(bundle_path, dry_run=dry_run)
    replace_thread_guard_implementation(bundle_path, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Strip debugging and observation features from python files in a directory.")
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
