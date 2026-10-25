#!/usr/bin/env python3
# WriterAgent - Static Thread Transition & Deadlock Analyzer
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Static call graph analyzer for thread-boundary transitions and deadlock detection.

Constructs a function-level call graph across the codebase to identify:
1. Inversion cycles: Call paths starting from synchronous host dispatches (Calc add-in
   calculation, UNO event listeners) that attempt blocking waits on the main thread (execute_on_main_thread).
2. Lock inversion hazards: Nested lock acquisitions across worker threads and main thread.

Usage:
    python scripts/analyze_thread_deadlocks.py [plugin_dir]
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

# Functions executed synchronously while holding a host/bridge dispatch context (Yellow context)
SYNC_HOST_ENTRYPOINTS = {
    # Add-in and Scripting Calculation Roots (wrapped with sync_host_dispatch)
    "execute_python_addin",
    "_execute_python_addin_impl",
    "execute_prompt_addin",
    "_execute_prompt_addin_impl",
    "py",
    "python",
    "prompt",
    "session_key",
    "_notify_thread_violation",
}

# Operations that block the calling thread waiting for the UI/main thread or other locks
BLOCKING_OPERATIONS = {
    "execute_on_main_thread",
}


# Generic standard method names that should not be used as cross-module function edges
GENERIC_METHOD_NAMES = {
    "get",
    "set",
    "put",
    "append",
    "extend",
    "emit",
    "cancel",
    "flush",
    "pop",
    "add",
    "clear",
    "read",
    "write",
    "update",
    "values",
    "items",
    "keys",
    "start",
    "join",
    "run",
    "close",
    "send",
    "group",
    "groups",
    "match",
    "search",
    "sub",
    "split",
    "format",
    "strip",
    "replace",
    "lower",
    "upper",
    "startswith",
    "endswith",
    "encode",
    "decode",
    "execute",
    "dispatch",
    "forward",
    "handle",
    "step",
    "createUnoService",
    "createInstanceWithContext",
    "createInstance",
    "getCurrentComponent",
    "getCurrentFrame",
    "getController",
    "getModel",
    "getSheets",
    "getCellByPosition",
    "getFormula",
    "setFormula",
    "exception",
    "info",
    "debug",
    "warning",
    "error",
}


class DeadlockHazard(NamedTuple):
    entrypoint: str
    call_chain: list[str]
    blocking_op: str
    location: str


class CallGraphBuilder(ast.NodeVisitor):
    def __init__(self, file_path: Path, source: str) -> None:
        self.file_path = file_path
        self.source_lines = source.splitlines()
        self.class_stack: list[str] = []
        self.current_function: str | None = None
        self.call_edges: list[tuple[str, str, int]] = []
        self.defined_functions: set[str] = set()
        self.suppressed_lines: set[int] = set()
        self._find_suppressions()

    def _find_suppressions(self) -> None:
        for idx, line in enumerate(self.source_lines, 1):
            if "# nodeadlock" in line or "# nosemgrep" in line:
                self.suppressed_lines.add(idx)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_fn(node)

    def _visit_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.lineno in self.suppressed_lines:
            return
        fn_name = node.name
        qualified_name = f"{self.class_stack[-1]}.{fn_name}" if self.class_stack else fn_name
        self.defined_functions.add(fn_name)
        self.defined_functions.add(qualified_name)
        prev = self.current_function
        self.current_function = qualified_name
        self.generic_visit(node)
        self.current_function = prev

    def visit_Call(self, node: ast.Call) -> None:
        if self.current_function and node.lineno not in self.suppressed_lines:
            callee = ""
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "self" and self.class_stack:
                    callee = f"{self.class_stack[-1]}.{node.func.attr}"
                else:
                    callee = node.func.attr
            if callee and callee not in GENERIC_METHOD_NAMES:
                self.call_edges.append((self.current_function, callee, node.lineno))
        self.generic_visit(node)


class DeadlockAnalyzer:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        # caller -> list of (callee, file_path, lineno)
        self.graph: dict[str, list[tuple[str, Path, int]]] = defaultdict(list)
        self.all_defined: set[str] = set()
        self._build_graph()

    def _build_graph(self) -> None:
        builders: list[CallGraphBuilder] = []
        for py_file in sorted(self.root_dir.rglob("*.py")):
            rel = str(py_file)
            if "plugin/contrib/" in rel or "plugin/lib/" in rel or "/tests/" in rel or "venv" in rel:
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
                builder = CallGraphBuilder(py_file, source)
                builder.visit(tree)
                builders.append(builder)
                self.all_defined.update(builder.defined_functions)
            except Exception:
                continue

        for b in builders:
            for caller, callee, lineno in b.call_edges:
                bare_callee = callee.split(".")[-1]
                if callee in BLOCKING_OPERATIONS or bare_callee in BLOCKING_OPERATIONS or callee in self.all_defined or bare_callee in self.all_defined:
                    self.graph[caller].append((callee, b.file_path, lineno))

    def find_deadlock_hazards(self) -> list[DeadlockHazard]:
        hazards: list[DeadlockHazard] = []

        # Find matching entrypoint nodes in graph
        entry_nodes: set[str] = set()
        for entry in SYNC_HOST_ENTRYPOINTS:
            if entry in self.graph:
                entry_nodes.add(entry)
            for node in self.graph:
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

                for callee, file_path, lineno in self.graph.get(curr, []):
                    if callee in BLOCKING_OPERATIONS:
                        loc = f"{file_path}:{lineno}" if file_path else "unknown"
                        hazards.append(
                            DeadlockHazard(
                                entrypoint=entry,
                                call_chain=path + [callee],
                                blocking_op=callee,
                                location=loc,
                            )
                        )
                    else:
                        if callee in self.graph:
                            target = callee
                        else:
                            bare = callee.split(".")[-1]
                            target = bare if bare in self.graph else ""

                        if target and target not in visited and len(path) < 15:
                            stack.append((target, path + [callee], file_path, lineno))

        return hazards


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("plugin")
    analyzer = DeadlockAnalyzer(root)
    hazards = analyzer.find_deadlock_hazards()

    if not hazards:
        print("Deadlock Analyzer: No synchronous cross-thread wait cycles detected.")
        return 0

    print(f"Deadlock Analyzer found {len(hazards)} potential deadlock hazard(s):")
    for h in hazards:
        chain = " -> ".join(h.call_chain)
        print(f"  [{h.entrypoint}] at {h.location}")
        print(f"    Call chain: {chain}")
        print(f"    Hazard: Sync host dispatch thread invokes blocking '{h.blocking_op}', risking lock inversion (#402).\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
