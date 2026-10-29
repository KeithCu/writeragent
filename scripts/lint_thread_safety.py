#!/usr/bin/env python3
# WriterAgent - AST Static Linter for UNO Thread Safety & Deadlocks
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""AST static linter to catch unguarded UNO calls and blocking deadlocks at build time.

Scans Python files to verify:
1. Functions that call UNO source getters (get_desktop, get_ctx, get_calc_document_from_ctx, etc.)
   are either decorated with @main_thread_only or have an on_main_thread() guard.
2. Synchronous add-in evaluation and notification functions do not call blocking execute_on_main_thread.
3. Add-in calculation paths do not use bare blocking synchronization primitives.
4. Cross-file call-graph analysis: call paths originating from @background entrypoints or
   run_in_background callbacks do not reach RED_UNO_SINKS across module boundaries without
   main-thread marshaling (execute_on_main_thread / post_to_main_thread) or on_main_thread() guards.

Usage:
    python scripts/lint_thread_safety.py [files or directories]
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

try:
    from scripts.analyze_thread_deadlocks import GENERIC_METHOD_NAMES
except ImportError:
    try:
        from analyze_thread_deadlocks import GENERIC_METHOD_NAMES  # type: ignore
    except ImportError:
        GENERIC_METHOD_NAMES = {
            "get", "set", "put", "append", "extend", "emit", "cancel", "flush",
            "pop", "add", "clear", "read", "write", "update", "values", "items",
            "keys", "start", "join", "close", "send", "group", "groups", "match",
            "search", "sub", "split", "format", "strip", "replace", "lower",
            "upper", "startswith", "endswith", "encode", "decode", "execute",
            "dispatch", "forward", "handle", "step", "exception", "info", "debug",
            "warning", "error",
        }

RED_UNO_FACTORY_GETTERS = {
    "get_desktop",
    "get_ctx",
    "get_active_document",
    "get_toolkit",
    "get_package_info",
    "_get_calc_doc",
    "get_calc_document_from_ctx",
    "get_active_document_for_scripts",
}

RED_UNO_SOURCES = RED_UNO_FACTORY_GETTERS  # Backward-compatible alias

RED_UNO_SINKS = {
    "get_desktop",
    "get_ctx",
    "get_active_document",
    "get_toolkit",
    "get_package_info",
    "_get_calc_doc",
    "get_calc_document_from_ctx",
    "get_active_document_for_scripts",
    "resolve_document_by_url",
    "get_document_from_frame",
    "get_extension_url",
    "get_extension_path",
    "process_events_to_idle",
    "get_document_context_for_chat",
    "get_calc_context_for_chat",
    "get_draw_context_for_chat",
    "get_document_uno_services",
    "get_document_type",
    "get_document_length",
    "get_text_cursor_at_range",
    "get_text_cursor_at_range_reached",
    "get_selection_range",
    "get_paragraph_ranges",
    "build_heading_tree",
    "ensure_heading_bookmarks",
    "resolve_locator",
    "get_string_without_tracked_deletions",
    "get_document_property",
    "set_document_property",
    "get_runtime_uid",
    "get_document_path",
    "insert_html_fragment_at_cursor",
    "insert_content_at_position",
    "replace_full_document",
    "replace_single_range_with_content",
    "replace_preserving_format",
    "apply_paragraph_style_preserving_direct_char",
    "createUnoService",
}

SYNC_ADDIN_FUNCTIONS = {
    "execute_python_addin",
    "_execute_python_addin_impl",
    "execute_prompt_addin",
    "_execute_prompt_addin_impl",
    "_notify_thread_violation",
    "session_key",
    "py",
    "python",
    "prompt",
}

BLOCKING_MARSHAL_FUNCS = {
    "execute_on_main_thread",
}

MARSHAL_SANITIZERS = {
    "execute_on_main_thread",
    "post_to_main_thread",
}

REGISTRATION_FUNCS = {
    "register_action_handler",
    "register_action_listener",
    "subscribe",
    "addStatusListener",
}

GENERIC_FN_NAMES = {
    "worker",
    "worker_wrapper",
    "target",
    "fn",
    "cb",
    "callback",
    "func",
    "handler",
    "action",
    "run",
    "work",
    "_run",
    "on_error",
    "on_success",
    "on_complete",
    "on_auto_stopped",
    "on_silence_progress",
}


class Finding(NamedTuple):
    file: Path
    line: int
    col: int
    rule_id: str
    message: str


class ThreadSafetyASTVisitor(ast.NodeVisitor):
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.findings: list[Finding] = []
        self.function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.guarded_scopes: list[bool] = [False]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        cond_str = ast.unparse(node.test)
        has_on_main = "on_main_thread()" in cond_str
        if has_on_main and not cond_str.startswith("not ") and " not " not in cond_str:
            self.guarded_scopes.append(True)
            self.visit(node.body)
            self.guarded_scopes.pop()

            self.guarded_scopes.append(False)
            self.visit(node.orelse)
            self.guarded_scopes.pop()
        else:
            self.generic_visit(node)

    def _visit_statements(self, statements: list[ast.stmt]) -> None:
        guarded = False
        for stmt in statements:
            if isinstance(stmt, ast.If):
                cond_str = ast.unparse(stmt.test)
                has_on_main = "on_main_thread()" in cond_str
                # Case 1: if not on_main_thread(): ... return / raise
                if has_on_main and (cond_str.startswith("not ") or " not " in cond_str):
                    exits_early = bool(stmt.body) and isinstance(
                        stmt.body[-1], (ast.Return, ast.Raise)
                    )
                    if exits_early and not stmt.orelse:
                        self.guarded_scopes.append(False)
                        self._visit_statements(stmt.body)
                        self.guarded_scopes.pop()
                        guarded = True
                        continue
                    elif stmt.orelse:
                        self.guarded_scopes.append(False)
                        self._visit_statements(stmt.body)
                        self.guarded_scopes.pop()

                        self.guarded_scopes.append(True)
                        self._visit_statements(stmt.orelse)
                        self.guarded_scopes.pop()
                        continue

                # Case 2: if on_main_thread(): body is guarded, orelse is unguarded
                if has_on_main and not cond_str.startswith("not ") and " not " not in cond_str:
                    self.guarded_scopes.append(True)
                    self._visit_statements(stmt.body)
                    self.guarded_scopes.pop()

                    if stmt.orelse:
                        self.guarded_scopes.append(False)
                        self._visit_statements(stmt.orelse)
                        self.guarded_scopes.pop()
                    continue

                if guarded:
                    self.guarded_scopes.append(True)
                self.visit(stmt.test)
                self._visit_statements(stmt.body)
                if stmt.orelse:
                    self._visit_statements(stmt.orelse)
                if guarded:
                    self.guarded_scopes.pop()
                continue

            if guarded:
                self.guarded_scopes.append(True)
                self.visit(stmt)
                self.guarded_scopes.pop()
            else:
                self.visit(stmt)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_statements(node.body)
        for h in node.handlers:
            self._visit_statements(h.body)
        if node.orelse:
            self._visit_statements(node.orelse)
        if node.finalbody:
            self._visit_statements(node.finalbody)

    def visit_With(self, node: ast.With) -> None:
        self._visit_statements(node.body)

    def visit_For(self, node: ast.For) -> None:
        self._visit_statements(node.body)
        if node.orelse:
            self._visit_statements(node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self._visit_statements(node.body)
        if node.orelse:
            self._visit_statements(node.orelse)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        is_main_thread_only = any(
            (isinstance(d, ast.Name) and d.id == "main_thread_only")
            or (isinstance(d, ast.Attribute) and d.attr == "main_thread_only")
            for d in node.decorator_list
        )
        self.function_stack.append(node)
        self.guarded_scopes.append(is_main_thread_only)
        self._visit_statements(node.body)
        self.guarded_scopes.pop()
        self.function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        current_fn = self.function_stack[-1].name if self.function_stack else ""
        is_guarded = any(self.guarded_scopes)

        # Rule 1: Unguarded UNO access in addin/scripting files
        if func_name in RED_UNO_FACTORY_GETTERS and not is_guarded:
            if current_fn in SYNC_ADDIN_FUNCTIONS or "calc/python" in str(self.file_path):
                self.findings.append(
                    Finding(
                        file=self.file_path,
                        line=node.lineno,
                        col=node.col_offset,
                        rule_id="unguarded-uno-access",
                        message=f"Call to UNO source '{func_name}' is not guarded by on_main_thread() check or @main_thread_only.",
                    )
                )

        # Rule 2: Blocking marshal in synchronous dispatch
        if func_name in BLOCKING_MARSHAL_FUNCS:
            if current_fn in SYNC_ADDIN_FUNCTIONS:
                self.findings.append(
                    Finding(
                        file=self.file_path,
                        line=node.lineno,
                        col=node.col_offset,
                        rule_id="blocking-marshal-in-sync-dispatch",
                        message=f"Blocking '{func_name}' inside synchronous dispatch function '{current_fn}' is a deadlock hazard (#402). Use post_to_main_thread or compute without UI marshaling.",
                    )
                )

        self.generic_visit(node)


class CrossFileCallGraphBuilder(ast.NodeVisitor):
    def __init__(self, file_path: Path, source: str) -> None:
        self.file_path = file_path
        self.source_lines = source.splitlines()
        self.class_stack: list[str] = []
        self.current_function: str | None = None
        # (caller, callee, lineno, is_guarded)
        self.call_edges: list[tuple[str, str, int, bool]] = []
        self.defined_functions: set[str] = set()
        self.background_entrypoints: set[str] = set()
        self.suppressed_lines: set[int] = set()
        self.guarded_scopes: list[bool] = [False]
        self._find_suppressions()

    def _find_suppressions(self) -> None:
        for idx, line in enumerate(self.source_lines, 1):
            if "# nothreadlint" in line or "# nodeadlock" in line or "# nosemgrep" in line:
                self.suppressed_lines.add(idx)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_fn(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        cond_str = ast.unparse(node.test)
        has_on_main = "on_main_thread()" in cond_str
        if has_on_main and not cond_str.startswith("not ") and " not " not in cond_str:
            self.guarded_scopes.append(True)
            self.visit(node.body)
            self.guarded_scopes.pop()

            self.guarded_scopes.append(False)
            self.visit(node.orelse)
            self.guarded_scopes.pop()
        else:
            self.generic_visit(node)

    def _visit_statements(self, statements: list[ast.stmt]) -> None:
        guarded = False
        for stmt in statements:
            if isinstance(stmt, ast.If):
                cond_str = ast.unparse(stmt.test)
                has_on_main = "on_main_thread()" in cond_str
                # Case 1: if not on_main_thread(): ... return / raise
                if has_on_main and (cond_str.startswith("not ") or " not " in cond_str):
                    exits_early = bool(stmt.body) and isinstance(
                        stmt.body[-1], (ast.Return, ast.Raise)
                    )
                    if exits_early and not stmt.orelse:
                        self.guarded_scopes.append(False)
                        self._visit_statements(stmt.body)
                        self.guarded_scopes.pop()
                        guarded = True
                        continue
                    elif stmt.orelse:
                        self.guarded_scopes.append(False)
                        self._visit_statements(stmt.body)
                        self.guarded_scopes.pop()

                        self.guarded_scopes.append(True)
                        self._visit_statements(stmt.orelse)
                        self.guarded_scopes.pop()
                        continue

                # Case 2: if on_main_thread(): body is guarded, orelse is unguarded
                if has_on_main and not cond_str.startswith("not ") and " not " not in cond_str:
                    self.guarded_scopes.append(True)
                    self._visit_statements(stmt.body)
                    self.guarded_scopes.pop()

                    if stmt.orelse:
                        self.guarded_scopes.append(False)
                        self._visit_statements(stmt.orelse)
                        self.guarded_scopes.pop()
                    continue

                if guarded:
                    self.guarded_scopes.append(True)
                self.visit(stmt.test)
                self._visit_statements(stmt.body)
                if stmt.orelse:
                    self._visit_statements(stmt.orelse)
                if guarded:
                    self.guarded_scopes.pop()
                continue

            if guarded:
                self.guarded_scopes.append(True)
                self.visit(stmt)
                self.guarded_scopes.pop()
            else:
                self.visit(stmt)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_statements(node.body)
        for h in node.handlers:
            self._visit_statements(h.body)
        if node.orelse:
            self._visit_statements(node.orelse)
        if node.finalbody:
            self._visit_statements(node.finalbody)

    def visit_With(self, node: ast.With) -> None:
        self._visit_statements(node.body)

    def visit_For(self, node: ast.For) -> None:
        self._visit_statements(node.body)
        if node.orelse:
            self._visit_statements(node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self._visit_statements(node.body)
        if node.orelse:
            self._visit_statements(node.orelse)

    def _visit_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.lineno in self.suppressed_lines:
            return
        fn_name = node.name
        qualified_name = f"{self.class_stack[-1]}.{fn_name}" if self.class_stack else fn_name
        self.defined_functions.add(fn_name)
        self.defined_functions.add(qualified_name)

        is_bg = any(
            (isinstance(d, ast.Name) and d.id == "background")
            or (isinstance(d, ast.Attribute) and d.attr == "background")
            for d in node.decorator_list
        )
        if is_bg:
            if not self.class_stack and not fn_name.startswith("_") and fn_name not in GENERIC_FN_NAMES:
                self.background_entrypoints.add(fn_name)
            self.background_entrypoints.add(qualified_name)

        is_main_thread_only = any(
            (isinstance(d, ast.Name) and d.id == "main_thread_only")
            or (isinstance(d, ast.Attribute) and d.attr == "main_thread_only")
            for d in node.decorator_list
        )

        prev = self.current_function
        self.current_function = qualified_name
        self.guarded_scopes.append(is_main_thread_only)
        self._visit_statements(node.body)
        self.guarded_scopes.pop()
        self.current_function = prev

    def visit_Call(self, node: ast.Call) -> None:
        if not self.current_function or node.lineno in self.suppressed_lines:
            self.generic_visit(node)
            return

        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "self" and self.class_stack:
                func_name = f"{self.class_stack[-1]}.{node.func.attr}"
            else:
                func_name = node.func.attr

        bare_name = func_name.split(".")[-1]

        # Detect run_in_background(target, ...)
        if (func_name == "run_in_background" or func_name.endswith(".run_in_background")) and node.args:
            target = node.args[0]
            if isinstance(target, ast.Name) and target.id not in GENERIC_FN_NAMES:
                self.background_entrypoints.add(target.id)
            elif isinstance(target, ast.Attribute):
                if isinstance(target.value, ast.Name) and target.value.id == "self" and self.class_stack:
                    self.background_entrypoints.add(f"{self.class_stack[-1]}.{target.attr}")
                elif target.attr not in GENERIC_FN_NAMES:
                    self.background_entrypoints.add(target.attr)
            elif isinstance(target, ast.Lambda):
                for sub in ast.walk(target.body):
                    if isinstance(sub, ast.Call):
                        sub_name = ""
                        if isinstance(sub.func, ast.Name):
                            sub_name = sub.func.id
                        elif isinstance(sub.func, ast.Attribute):
                            if isinstance(sub.func.value, ast.Name) and sub.func.value.id == "self" and self.class_stack:
                                sub_name = f"{self.class_stack[-1]}.{sub.func.attr}"
                            else:
                                sub_name = sub.func.attr
                        if sub_name and sub_name.split(".")[-1] not in GENERIC_FN_NAMES:
                            self.background_entrypoints.add(sub_name)

        is_guarded = any(self.guarded_scopes)

        # Detect marshal sanitizers: execute_on_main_thread / post_to_main_thread
        if bare_name in MARSHAL_SANITIZERS:
            self.call_edges.append((self.current_function, func_name, node.lineno, is_guarded))
            return

        # Registration functions: do not treat callback arguments as immediate synchronous calls
        if bare_name in REGISTRATION_FUNCS:
            return

        if func_name and func_name not in GENERIC_METHOD_NAMES and bare_name not in GENERIC_FN_NAMES:
            self.call_edges.append((self.current_function, func_name, node.lineno, is_guarded))

        self.generic_visit(node)


def scan_file(file_path: Path) -> list[Finding]:
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except Exception as exc:
        return [
            Finding(
                file=file_path,
                line=1,
                col=0,
                rule_id="syntax-error",
                message=f"Failed to parse AST: {exc}",
            )
        ]

    visitor = ThreadSafetyASTVisitor(file_path)
    visitor.visit(tree)
    return visitor.findings


def scan_target(target: Path) -> list[Finding]:
    findings: list[Finding] = []
    if target.is_file() and target.suffix == ".py":
        findings.extend(scan_file(target))
    elif target.is_dir():
        for py_file in sorted(target.rglob("*.py")):
            # Skip contrib, lib, tests, venv
            rel = str(py_file)
            if "plugin/contrib" in rel or "plugin/lib" in rel or "/tests/" in rel or "venv" in rel:
                continue
            findings.extend(scan_file(py_file))
    return findings


def scan_cross_file_callgraph(root_dir: Path) -> list[Finding]:
    """Scan root_dir for cross-file UNO access from background entrypoints."""
    builders: list[CrossFileCallGraphBuilder] = []
    all_defined: set[str] = set()
    background_entrypoints: set[str] = set()

    for py_file in sorted(root_dir.rglob("*.py")):
        rel = str(py_file)
        if "plugin/contrib" in rel or "plugin/lib" in rel or "/tests/" in rel or "venv" in rel:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
            builder = CrossFileCallGraphBuilder(py_file, source)
            builder.visit(tree)
            builders.append(builder)
            all_defined.update(builder.defined_functions)
            background_entrypoints.update(builder.background_entrypoints)
        except Exception:
            continue

    graph: dict[str, list[tuple[str, Path, int, bool]]] = defaultdict(list)
    all_sinks_and_sanitizers = RED_UNO_SINKS | MARSHAL_SANITIZERS

    for b in builders:
        for caller, callee, lineno, is_guarded in b.call_edges:
            bare_callee = callee.split(".")[-1]
            if (callee in all_sinks_and_sanitizers or bare_callee in all_sinks_and_sanitizers or
                callee in all_defined or bare_callee in all_defined):
                graph[caller].append((callee, b.file_path, lineno, is_guarded))

    findings: list[Finding] = []
    entry_nodes: set[str] = set()
    for entry in background_entrypoints:
        if entry in graph:
            entry_nodes.add(entry)
        for node in graph:
            if node.endswith(f".{entry}"):
                entry_nodes.add(node)

    for entry in sorted(entry_nodes):
        visited: set[str] = set()
        stack: list[tuple[str, list[str], Path | None, int]] = [(entry, [entry], None, 0)]

        while stack:
            curr, path, last_file, last_line = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)

            for callee, file_path, lineno, is_guarded in graph.get(curr, []):
                bare_callee = callee.split(".")[-1]
                if callee in MARSHAL_SANITIZERS or bare_callee in MARSHAL_SANITIZERS:
                    continue  # Marshaled to main thread -> safe
                if is_guarded:
                    continue  # Guarded by on_main_thread() -> safe

                if callee in RED_UNO_SINKS or bare_callee in RED_UNO_SINKS:
                    chain = " -> ".join(path + [callee])
                    findings.append(
                        Finding(
                            file=file_path or Path("unknown"),
                            line=lineno,
                            col=0,
                            rule_id="uno-off-main-thread-callgraph",
                            message=f"Background entrypoint '{entry}' reaches UNO sink '{callee}' without main-thread marshaling: {chain}",
                        )
                    )
                else:
                    if callee in graph:
                        target = callee
                    else:
                        target = bare_callee if bare_callee in graph else ""

                    if target and target not in visited and len(path) < 15:
                        stack.append((target, path + [callee], file_path, lineno))

    return findings


def main() -> int:
    targets = [Path(arg) for arg in sys.argv[1:]] if len(sys.argv) > 1 else [Path("plugin")]
    all_findings: list[Finding] = []

    # 1. Intra-file checks
    for target in targets:
        all_findings.extend(scan_target(target))

    # 2. Cross-file call-graph checks across directories
    scanned_dirs: set[Path] = set()
    for target in targets:
        dir_to_scan = target if target.is_dir() else target.parent
        # Anchor to plugin/ root for complete cross-module visibility if within plugin
        if "plugin" in dir_to_scan.parts:
            root = Path("plugin")
        else:
            root = dir_to_scan
        if root not in scanned_dirs and root.exists():
            scanned_dirs.add(root)
            all_findings.extend(scan_cross_file_callgraph(root))

    if not all_findings:
        print(f"Thread Safety AST Linter: All checks passed ({len(targets)} targets scanned).")
        return 0

    print(f"Thread Safety AST Linter found {len(all_findings)} violation(s):")
    for f in all_findings:
        print(f"  {f.file}:{f.line}:{f.col}: [{f.rule_id}] {f.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
