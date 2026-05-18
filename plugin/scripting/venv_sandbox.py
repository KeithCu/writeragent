# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Venv worker sandbox: path setup for vendored smolagents + LocalPythonExecutor.

Used by worker_harness.py (venv child adds repo root to sys.path for ``plugin.*`` imports).
Import policy is only VENV_AUTHORIZED_IMPORTS passed to LocalPythonExecutor—no find_spec pre-checks.
"""

from __future__ import annotations

import ast
import importlib
import sys
from typing import Any

from plugin.contrib.smolagents.local_python_executor import InterpreterError, LocalPythonExecutor
from plugin.framework.constants import AUTO_IMPORTS

# Curated by WriterAgent (see docs/enabling_numpy_in_libreoffice.md)—not "whatever is in the venv".
VENV_AUTHORIZED_IMPORTS: tuple[str, ...] = (
    "platform",
    "numpy",
    "numpy.*",
    "pandas",
    "pandas.*",
    "scipy",
    "scipy.*",
    "sklearn",
    "sklearn.*",
    "matplotlib",
    "matplotlib.*",
    "seaborn",
    "seaborn.*",
    "sympy",
    "sympy.*",
    "statsmodels",
    "statsmodels.*",
    "networkx",
    "networkx.*",
    "PIL",
    "PIL.*",
    "cv2",
    "json",
    "csv",
    "decimal",
    "fractions",
    "functools",
    "operator",
    "string",
    "textwrap",
    "enum",
    "dataclasses",
    "typing",
    "copy",
    "pprint",
)


def _is_module_imported(code_str: str, module_name: str) -> bool:
    """Check if ``module_name`` is imported in any form in ``code_str``."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        # Fallback to simple substring match in case of syntax error.
        return f"import {module_name}" in code_str or f"from {module_name}" in code_str

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name or alias.name.startswith(module_name + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == module_name or (node.module and node.module.startswith(module_name + ".")):
                return True
    return False


def _optional_module(name: str) -> Any | None:
    if name in sys.modules:
        return sys.modules[name]
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def serialize_result(obj: Any) -> Any:
    """Convert numpy/pandas and containers to JSON-safe values."""
    np_mod = _optional_module("numpy")
    if np_mod is not None:
        if isinstance(obj, np_mod.ndarray):
            return obj.tolist()
        if isinstance(obj, (np_mod.integer,)):
            return int(obj)
        if isinstance(obj, (np_mod.floating,)):
            return float(obj)
        if isinstance(obj, np_mod.bool_):
            return bool(obj)
    pd_mod = _optional_module("pandas")
    if pd_mod is not None:
        if isinstance(obj, pd_mod.DataFrame):
            return obj.to_dict(orient="records")
        if isinstance(obj, pd_mod.Series):
            return obj.tolist()
    if isinstance(obj, (list, tuple)):
        return [serialize_result(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): serialize_result(v) for k, v in obj.items()}
    return obj


def run_sandboxed_code(code: str, data: Any | None = None, *, timeout_sec: int = 120) -> dict[str, Any]:
    """Run *code* in a fresh LocalPythonExecutor (new namespace per call)."""
    # Automatically prepend imports if they are available in the environment and not explicitly imported
    prepended_lines = []
    for module_name, import_stmt in AUTO_IMPORTS.items():
        if not _is_module_imported(code, module_name):
            if _optional_module(module_name) is not None:
                prepended_lines.append(import_stmt)

    if prepended_lines:
        code = "\n".join(prepended_lines) + "\n" + code

    executor = LocalPythonExecutor(
        additional_authorized_imports=list(VENV_AUTHORIZED_IMPORTS),
        timeout_seconds=timeout_sec,
    )
    # Upstream only merges BASE_PYTHON_TOOLS (sum, len, …) after send_tools(); without this,
    # static_tools stays None and builtins like sum() are rejected.
    executor.send_tools({})
    if data is not None:
        executor.send_variables({"data": data})
    try:
        code_output = executor(code)
        result = executor.state.get("result", code_output.output)
        return {
            "status": "ok",
            "result": serialize_result(result),
            "stdout": code_output.logs or "",
        }
    except InterpreterError as e:
        return {
            "status": "error",
            "message": str(e),
            "stdout": str(executor.state.get("_print_outputs", "")),
        }
    except Exception as e:
        import traceback

        return {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
            "stdout": "",
        }
