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
import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

from plugin.contrib.smolagents.local_python_executor import InterpreterError, LocalPythonExecutor
from plugin.scripting.payload_codec import (
    child_pack_result,
    child_unpack_data,
    describe_wire_value,
    is_multi_data,
    is_split_grid,
)
from plugin.scripting.timeout_limits import python_exec_timeout_default
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
    "webview",
    "jedi",
    "PyQt6",
    "PyQt6.QtWebEngineWidgets",
    "qtpy",
)


def is_module_imported(code_str: str, module_name: str) -> bool:
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


def optional_module(name: str) -> Any | None:
    if name in sys.modules:
        return sys.modules[name]
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def apply_auto_imports(code: str) -> tuple[str, int]:
    """Prepend imports from AUTO_IMPORTS if missing and available. Returns (new_code, lines_added)."""
    prepended_lines = []
    for module_name, import_stmt in AUTO_IMPORTS.items():
        if not is_module_imported(code, module_name):
            if optional_module(module_name) is not None:
                prepended_lines.append(import_stmt)

    if not prepended_lines:
        return code, 0

    return "\n".join(prepended_lines) + "\n" + code, len(prepended_lines)


def serialize_result(obj: Any) -> Any:
    """Convert numpy/pandas and containers to JSON-safe values (split_grid for large numeric/mixed arrays)."""
    try:
        return _serialize_result_impl(obj)
    except Exception:
        log.exception(
            "venv_sandbox serialize_result failed for value %s",
            describe_wire_value(obj),
        )
        raise


def _serialize_result_impl(obj: Any) -> Any:
    np_mod = optional_module("numpy")
    if np_mod is not None:
        if isinstance(obj, (np_mod.ndarray, np_mod.integer, np_mod.floating, np_mod.bool_)):
            return child_pack_result(obj)
    pd_mod = optional_module("pandas")
    if pd_mod is not None:
        if isinstance(obj, pd_mod.DataFrame):
            return child_pack_result(obj.to_dict(orient="records"))
        if isinstance(obj, pd_mod.Series):
            return child_pack_result(obj.to_numpy())
    if isinstance(obj, (dict, list, tuple)):
        return child_pack_result(obj)
    return obj


def run_sandboxed_code(code: str, data: Any | None = None, *, timeout_sec: int | None = None) -> dict[str, Any]:
    """Run *code* in a fresh LocalPythonExecutor (new namespace per call)."""
    if timeout_sec is None:
        timeout_sec = python_exec_timeout_default()
    # Automatically prepend imports if they are available in the environment and not explicitly imported
    code, _ = apply_auto_imports(code)

    executor = LocalPythonExecutor(
        additional_authorized_imports=list(VENV_AUTHORIZED_IMPORTS),
        timeout_seconds=timeout_sec,
    )
    # Upstream only merges BASE_PYTHON_TOOLS (sum, len, …) after send_tools(); without this,
    # static_tools stays None and builtins like sum() are rejected.
    executor.send_tools({})
    if data is not None:
        if is_split_grid(data):
            log.debug("venv_sandbox injecting data %s", describe_wire_value(data))
        unpacked = child_unpack_data(data)
        variables: dict[str, Any] = {"data": unpacked}
        variables["data_list"] = unpacked if is_multi_data(data) else [unpacked]
        executor.send_variables(variables)
    try:
        code_output = executor(code)
        result = executor.state.get("result", code_output.output)
        serialized = serialize_result(result)
        if is_split_grid(serialized):
            log.debug("venv_sandbox worker result %s", describe_wire_value(serialized))
        return {
            "status": "ok",
            "result": serialized,
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
