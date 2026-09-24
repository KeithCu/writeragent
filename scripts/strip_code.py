#!/usr/bin/env python3
# WriterAgent — AST-based release-bundle stripping tool
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""AST-based utility to strip debug/observability call sites from production bundles.

Release / ``--strip`` / ``--no-tests`` OXT assembly removes:

* ``grammar_obs(...)`` / ``_grammar_obs(...)`` expression statements
* Logger ``.debug(...)`` / ``.info(...)`` expression statements
* ``print(...)`` / ``pprint(...)`` expression statements (except a keep-list)
* ``@deal.*`` decorators (keep ``deal_shim`` imports)
* ``@main_thread_only`` decorators
* Full ``thread_guard.py`` → no-op stubs

Retail keeps ``warning`` / ``error`` / ``exception`` (and keep-listed prints).
Checkout / ``make build`` (no strip) is unchanged.

Tests that assert ``deal.PreContractError`` or ``log.debug``/``log.info`` output must
accept the stripped tree: see ``tests/harness/strip_bundle.py`` (dual-path body guards;
skip log-line asserts when call sites are gone). ``make release`` pytest runs
against a stripped temp tree (``tempfile.mkdtemp``, typically under ``/tmp``) after this stripper.

Why bother (measured 2026-08-10 under ``plugin/``, excluding tests):

* ``.debug`` — ~849 call sites, ~76 KB of source text (eager args still run at WARN)
* ``.info`` — ~292 call sites, ~29 KB
* ``print`` / ``pprint`` — ~49 call sites, ~3 KB (small now; still strip for quiet retail;
  keep-list preserves stderr fallbacks, subprocess IPC, and CLI UX)
* ``@deal.*`` — ~364 decorators, ~34 KB (shim already no-ops; strip skips def-time wrappers)

Imports, logger setup, ``grammar_obs.py``, ``emit_grammar_status``, and
``from plugin.framework.deal_shim import deal`` stay intact.

Line edits / empty-suite ``pass`` live in
[`plugin.framework.ast_stmt_edit`](../plugin/framework/ast_stmt_edit.py) (shared with
Excel PY discarded-``xl()`` stripping).
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from typing import TYPE_CHECKING

from plugin.framework.ast_stmt_edit import (
    is_name_call_expr,
    iter_matching_expr_statements,
    remove_expr_statements,
)

if TYPE_CHECKING:
    from collections.abc import Callable

GRAMMAR_OBS_CALL_NAMES: frozenset[str] = frozenset({"grammar_obs", "_grammar_obs"})
PRINT_CALL_NAMES: frozenset[str] = frozenset({"print", "pprint"})
LOGGER_STRIP_ATTRS: frozenset[str] = frozenset({"debug", "info"})

EXCLUDED_STRIP_PATTERNS: list[str] = [
    "plugin/testing_runner.py",
    "plugin/tests/",
    "tests/",
]

# Print/pprint keep-list: load-bearing stderr, subprocess stdout IPC, CLI UX.
# (Logger .debug/.info are still stripped in these files.)
PRINT_KEEP_PATTERNS: list[str] = [
    "plugin/framework/logging.py",
    "plugin/chatbot/audio_recorder.py",
    "plugin/scripting/venv/audio_recorder.py",
    "plugin/scripting/venv/editor_main.py",
    "plugin/scripting/venv_diagnostics.py",
    "plugin/calc/excel_py_convert/cli.py",
    "plugin/lib/latex2mathml/converter.py",
    "plugin/contrib/smolagents/monitoring.py",
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


def should_skip_print_strip(rel_path: str) -> bool:
    """True if *rel_path* is globally excluded or on the print keep-list."""
    if should_skip_strip(rel_path):
        return True
    for pattern in PRINT_KEEP_PATTERNS:
        if pattern.endswith("/"):
            if rel_path.startswith(pattern):
                return True
        elif rel_path == pattern:
            return True
    return False


def _is_grammar_obs_call(node: ast.Expr) -> bool:
    """True if ``node`` is an expression-statement call to grammar_obs / _grammar_obs."""
    return is_name_call_expr(node, GRAMMAR_OBS_CALL_NAMES)


def _is_print_call(node: ast.Expr) -> bool:
    """True if ``node`` is an expression-statement ``print(...)`` / ``pprint(...)``."""
    return is_name_call_expr(node, PRINT_CALL_NAMES)


def _is_logger_debug_or_info_call(node: ast.Expr) -> bool:
    """True if ``node`` is an expression-statement logger ``.debug(...)`` / ``.info(...)``.

    Matches ``log.debug``, ``logger.info``, ``self.logger.debug``,
    ``logging.getLogger(...).info``, and similar Attribute receivers. Does not
    match bare ``debug(...)`` / ``info(...)`` Name calls.
    """
    value = getattr(node, "value", None) if isinstance(node, ast.Expr) else None
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    return isinstance(func, ast.Attribute) and func.attr in LOGGER_STRIP_ATTRS


def _walk_and_strip_expr_statements(
    bundle_path: str,
    *,
    label: str,
    should_remove: Callable[[ast.Expr], bool],
    pass_comment: str,
    dry_run: bool,
    skip_file: Callable[[str], bool] = should_skip_strip,
) -> None:
    """Shared walk: dry-run report or ``remove_expr_statements`` rewrite."""
    action = "Dry run: would strip" if dry_run else "Stripping"
    print(f"  {action} {label} from {bundle_path} using AST...")

    for root, _, filenames in os.walk(bundle_path):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel_path = os.path.relpath(path, bundle_path).replace(os.sep, "/")
            if skip_file(rel_path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()

                if dry_run:
                    nodes = iter_matching_expr_statements(content, should_remove)
                    if not nodes:
                        continue
                    lines = content.splitlines(keepends=True)
                    for node in nodes:
                        start_line = node.lineno
                        end_line = getattr(node, "end_lineno", None) or start_line
                        original_line = lines[start_line - 1]
                        snippet = original_line.strip()
                        if end_line > start_line:
                            snippet += f" ... (spans {end_line - start_line + 1} lines)"
                        print(f"    [DryRun] {rel_path}: L{start_line}-{end_line}: {snippet}")
                    continue

                new_content, removed = remove_expr_statements(
                    content,
                    should_remove,
                    pass_comment=pass_comment,
                )
                if removed:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)

            except Exception as e:
                if "match" not in str(e):
                    print(f"    SKIPPING {fn}: {e}")

    print(f"  Done: Stripped {label} from bundle.")


def strip_grammar_obs_calls(bundle_path: str, dry_run: bool = False) -> None:
    """Remove ``grammar_obs(...)`` / ``_grammar_obs(...)`` expression statements from Python files.

    Uses :func:`plugin.framework.ast_stmt_edit.remove_expr_statements` (AST line ranges,
    including multi-line calls; inserts ``pass`` when stripping would leave an empty block).
    """
    _walk_and_strip_expr_statements(
        bundle_path,
        label="grammar_obs calls",
        should_remove=_is_grammar_obs_call,
        pass_comment="stripped obs call",
        dry_run=dry_run,
    )


def strip_log_debug_info_calls(bundle_path: str, dry_run: bool = False) -> None:
    """Remove logger ``.debug`` / ``.info`` expression statements from Python files.

    Measured 2026-08-10: ~849 debug + ~292 info sites (~105 KB) under ``plugin/``
    (excluding tests). Eager argument evaluation still runs when ``log_level`` is WARN,
    so stripping also avoids wasted JSON/UNO work in retail builds.
    """
    _walk_and_strip_expr_statements(
        bundle_path,
        label="log.debug/log.info calls (~849+~292 sites / ~105 KB as of 2026-08-10)",
        should_remove=_is_logger_debug_or_info_call,
        pass_comment="stripped log",
        dry_run=dry_run,
    )


def strip_print_calls(bundle_path: str, dry_run: bool = False) -> None:
    """Remove ``print`` / ``pprint`` expression statements except :data:`PRINT_KEEP_PATTERNS`.

    Measured 2026-08-10: ~49 sites / ~3 KB under ``plugin/`` (excluding tests). Small,
    but retail builds should not spam stdout; keep-list preserves logging fallbacks,
    audio/editor stderr, venv diagnostics IPC, and CLI helpers.
    """
    _walk_and_strip_expr_statements(
        bundle_path,
        label="print/pprint calls (~49 sites / ~3 KB as of 2026-08-10; keep-list excluded)",
        should_remove=_is_print_call,
        pass_comment="stripped print",
        dry_run=dry_run,
        skip_file=should_skip_print_strip,
    )


def _is_deal_decorator(node: ast.AST) -> bool:
    """True if *node* is a decorator under the ``deal`` namespace (e.g. ``@deal.pre``)."""
    curr: ast.AST = node
    if isinstance(curr, ast.Call):
        curr = curr.func
    while isinstance(curr, ast.Attribute):
        curr = curr.value
    return isinstance(curr, ast.Name) and curr.id == "deal"


def _strip_matching_decorators(
    bundle_path: str,
    *,
    label: str,
    needle: str,
    should_remove: Callable[[ast.AST], bool],
    dry_run: bool,
) -> None:
    """Delete matching decorators from FunctionDef / AsyncFunctionDef / ClassDef lists."""
    action = "Dry run: would strip" if dry_run else "Stripping"
    print(f"  {action} {label} from {bundle_path} using AST...")

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

                if needle not in content:
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

                    def visit_ClassDef(self, node: ast.ClassDef) -> None:
                        self.check_decorators(node)
                        self.generic_visit(node)

                    def check_decorators(
                        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                    ) -> None:
                        for dec in node.decorator_list:
                            if should_remove(dec):
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
                        if end_line > start_line:
                            snippet += f" ... (spans {end_line - start_line + 1} lines)"
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

    print(f"  Done: Stripped {label} from bundle.")


def strip_main_thread_only_decorators(bundle_path: str, dry_run: bool = False) -> None:
    """Remove ``@main_thread_only`` decorators from python files."""
    _strip_matching_decorators(
        bundle_path,
        label="main_thread_only decorators",
        needle="main_thread_only",
        should_remove=lambda dec: isinstance(dec, ast.Name) and dec.id == "main_thread_only",
        dry_run=dry_run,
    )


def strip_deal_decorators(bundle_path: str, dry_run: bool = False) -> None:
    """Remove ``@deal.*`` decorators; keep ``deal_shim`` imports.

    Measured 2026-08-10: ~364 decorators / ~34 KB under ``plugin/`` (excluding tests).
    Retail already no-ops via ``deal_shim``; stripping skips def-time wrapper application
    and shrinks the OXT. Does not strip ``deal_shim.py`` or bare ``import deal``.
    """
    _strip_matching_decorators(
        bundle_path,
        label="@deal.* decorators (~364 sites / ~34 KB as of 2026-08-10)",
        needle="deal.",
        should_remove=_is_deal_decorator,
        dry_run=dry_run,
    )


def replace_thread_guard_implementation(bundle_path: str, dry_run: bool = False) -> None:
    """Replace plugin/framework/thread_guard.py with a minimal, no-op stub implementation."""
    target_file = os.path.join(bundle_path, "plugin", "framework", "thread_guard.py")
    if not os.path.exists(target_file):
        return

    stubs = '''# Minimal stubs for production/release bundles to remove runtime check overhead.
import threading
from contextlib import contextmanager
from typing import Any, Generator

GUARD_ON = False

_bg = threading.local()
_sync_host = threading.local()

@contextmanager
def sync_host_dispatch() -> Generator[None, None, None]:
    prev = getattr(_sync_host, "active", False)
    _sync_host.active = True
    try:
        yield
    finally:
        _sync_host.active = prev

def in_sync_host_dispatch() -> bool:
    return getattr(_sync_host, "active", False)

def assert_main_thread(what: str) -> None:
    pass

def main_thread_only(fn: Any) -> Any:
    return fn

def background(fn: Any) -> Any:
    return fn

def set_background_task(name: str | None) -> None:
    try:
        _bg.task_name = name
    except Exception:
        pass

def get_background_task_name() -> str | None:
    return getattr(_bg, "task_name", None)

def set_designated_main_thread(thread: Any) -> None:
    pass

def get_designated_main_thread() -> Any:
    return None

# Bugfix: on_main_thread() previously returned True unconditionally.
# This caused background threads (e.g. Writer linguistic worker Dummy-21) calling
# wait_while_pumping to falsely believe they were on the main thread and skip
# posting PE2I events to the main thread. Checking threading.current_thread()
# is threading.main_thread() correctly identifies background worker threads in release bundles.
def on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _wrap_uno(obj: Any) -> Any:
    return obj

class _UnoThreadGuardProxy:
    """Stub proxy for release bundles."""
    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._target, name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._target(*args, **kwargs)

    def __eq__(self, other: object) -> bool:
        return self._target == _unwrap_uno(other)

def _unwrap_uno(obj: Any) -> Any:
    if isinstance(obj, _UnoThreadGuardProxy):
        return obj._target
    return obj

def guard_uno(obj: Any) -> Any:
    return obj

__all__ = [
    "guard_uno",
    "assert_main_thread",
    "main_thread_only",
    "background",
    "sync_host_dispatch",
    "in_sync_host_dispatch",
    "set_background_task",
    "get_background_task_name",
    "set_designated_main_thread",
    "get_designated_main_thread",
    "on_main_thread",
    "_wrap_uno",
    "_unwrap_uno",
    "GUARD_ON",
]
'''
    action = "Dry run: would replace" if dry_run else "Replacing"
    print(f"  {action} {target_file} with minimal stubs...")
    if not dry_run:
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(stubs)


def omit_sidebar_test_hooks(bundle_path: str, dry_run: bool = False) -> None:
    """Delete plugin/chatbot/sidebar_test_hooks.py from release trees (no stub left behind)."""
    target_file = os.path.join(bundle_path, "plugin", "chatbot", "sidebar_test_hooks.py")
    if not os.path.exists(target_file):
        return
    action = "Dry run: would delete" if dry_run else "Deleting"
    print(f"  {action} {target_file} (debug-only sidebar hooks)")
    if not dry_run:
        os.remove(target_file)


def strip_production_code(bundle_path: str, dry_run: bool = False) -> None:
    """Release-bundle entry point: strip obs/debug/info/print/deal, ``main_thread_only``, stub ``thread_guard``, omit sidebar test hooks."""
    strip_grammar_obs_calls(bundle_path, dry_run=dry_run)
    strip_log_debug_info_calls(bundle_path, dry_run=dry_run)
    strip_print_calls(bundle_path, dry_run=dry_run)
    strip_main_thread_only_decorators(bundle_path, dry_run=dry_run)
    strip_deal_decorators(bundle_path, dry_run=dry_run)
    replace_thread_guard_implementation(bundle_path, dry_run=dry_run)
    omit_sidebar_test_hooks(bundle_path, dry_run=dry_run)


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
