# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Venv / in-process Python sandbox import policy for LLM prompts.

Derived from ``VENV_AUTHORIZED_IMPORTS``, ``BASE_BUILTIN_MODULES``, and
``DANGEROUS_MODULES`` — single source of truth for agent-facing import guidance.
"""

from __future__ import annotations

from plugin.scripting.sandbox_imports import (
    BASE_BUILTIN_MODULES,
    CALC_AUTHORIZED_IMPORTS,
    DANGEROUS_MODULES,
    VENV_AUTHORIZED_IMPORTS,
)

# Stdlib roots from VENV_AUTHORIZED_IMPORTS beyond BASE_BUILTIN_MODULES.
_VENV_STDLIB_EXTRA: frozenset[str] = frozenset(
    {
        "copy",
        "csv",
        "dataclasses",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "json",
        "operator",
        "platform",
        "pprint",
        "string",
        "textwrap",
        "typing",
    }
)

# Not whitelisted — common LLM mistakes (guidance only; blocked at import check).
_VENV_COMMON_BLOCKED: tuple[str, ...] = (
    "requests",
    "urllib",
    "urllib3",
    "http",
    "httpx",
    "ssl",
    "pickle",
    "sqlite3",
    "logging",
    "importlib",
    "ctypes",
    "threading",
)

PYTHON_VENV_SANDBOX_CONTEXT_PREFIX = (
    "PYTHON VENV SANDBOX: You are running in a powerful Python sandbox with access to many "
    "scientific and stdlib modules (NumPy, pandas, SciPy, and "
    "more). This sandbox has no networking, no filesystem or process escape, and no direct document "
    "access. Pass inputs via data/data_range; assign outputs (any type: string, list, NumPy array, "
    "or dictionary with structured keys) to the 'result' variable. Prefer NumPy arrays in 'result' for faster serialization. "
    "Note: While the sandboxed venv has NumPy/Pandas, the LibreOffice host environment does not. "
    "Therefore, the specialized_workflow_finished tool API only accepts basic Python types (strings, lists, numbers, dicts)."
)

INPROCESS_SANDBOX_CONTEXT_PREFIX = (
    "PYTHON IN-PROCESS SANDBOX: You are running in LibreOffice's embedded stdlib-only Python sandbox "
    "(not the user venv). This sandbox has no NumPy/pandas and no networking or host escape. "
    "Helpers lp/set_range read and write sheet cells."
)


def venv_authorized_top_level_modules() -> tuple[str, ...]:
    """Top-level module names allowed in the venv worker sandbox."""
    roots: set[str] = set(BASE_BUILTIN_MODULES)
    for entry in VENV_AUTHORIZED_IMPORTS:
        if entry.endswith(".*"):
            roots.add(entry[:-2])
        else:
            roots.add(entry)
    return tuple(sorted(roots))


def _venv_stdlib_modules() -> tuple[str, ...]:
    return tuple(sorted(set(BASE_BUILTIN_MODULES) | _VENV_STDLIB_EXTRA))


def _venv_package_modules() -> tuple[str, ...]:
    stdlib = set(_venv_stdlib_modules())
    return tuple(sorted(m for m in venv_authorized_top_level_modules() if m not in stdlib))


def venv_blocked_modules() -> tuple[str, ...]:
    """Explicitly dangerous modules plus common not-whitelisted mistakes."""
    return tuple(sorted(set(DANGEROUS_MODULES) | set(_VENV_COMMON_BLOCKED)))


def inprocess_authorized_modules() -> tuple[str, ...]:
    """Modules allowed in LO embedded execute_python_script sandbox."""
    return tuple(sorted(set(BASE_BUILTIN_MODULES) | set(CALC_AUTHORIZED_IMPORTS)))


def _join_modules(modules: tuple[str, ...]) -> str:
    return ", ".join(modules)


def format_venv_import_policy_for_prompt(*, compact: bool = False) -> str:
    """Sandbox context prefix first, then import rules for LLM prompts."""
    auto_imports = (
        "Pre-imported (do not write import lines): np, pd, sp, math, xl. "
        "DO NOT import numpy, pandas, sympy, math, or plugin.scripting.calc_functions. "
        "Use xl.* for Calc-parity helpers (SUMIF, XLOOKUP, FILTER, etc.). "
        "Prefer np/sp/pd and scipy over hand-rolled Python."
    )
    blocked_security = _join_modules(tuple(sorted(DANGEROUS_MODULES)))
    blocked_network = _join_modules(
        tuple(m for m in _VENV_COMMON_BLOCKED if m in ("requests", "urllib", "urllib3", "http", "httpx", "ssl", "socket"))
    )
    if "socket" not in blocked_network:
        blocked_network = f"socket, {blocked_network}" if blocked_network else "socket"

    parts = [PYTHON_VENV_SANDBOX_CONTEXT_PREFIX, auto_imports]

    if compact:
        parts.append(
            f"Blocked in this sandbox: host escape ({blocked_security}); "
            f"networking ({blocked_network}); other imports not on the whitelist fail."
        )
    else:
        stdlib = _join_modules(_venv_stdlib_modules())
        packages = _join_modules(_venv_package_modules())
        common = _join_modules(_VENV_COMMON_BLOCKED)
        parts.append(f"Allowed stdlib in this sandbox: {stdlib}.")
        parts.append(f"Allowed packages in this sandbox (+ submodules where applicable): {packages}.")
        parts.append(f"Always blocked in this sandbox: {blocked_security}.")
        parts.append(f"Common not-whitelisted (will fail): {common}, and anything else not listed above.")

    return " ".join(parts)


def format_inprocess_import_policy_for_prompt() -> str:
    """Prompt line for execute_python_script (stdlib in-process sandbox)."""
    allowed = _join_modules(inprocess_authorized_modules())
    blocked = _join_modules(tuple(sorted(DANGEROUS_MODULES)))
    return (
        f"{INPROCESS_SANDBOX_CONTEXT_PREFIX} "
        f"Allowed imports in this sandbox: {allowed}. "
        f"Blocked: {blocked} and anything else not listed."
    )


_MATPLOTLIB_PLOT_HINTS: dict[str, str] = {
    "calc": (
        "PLOTS: plt.plot(...) or result=fig; chart inserts on the active sheet automatically. "
        "Do not call insert_image. Use data_range for sheet data."
    ),
    "writer": (
        "PLOTS: plt.plot(...) or result=fig; then insert_image(image_path=<returned path>). "
        "Use document tools for text/data."
    ),
    "draw": (
        "PLOTS: plt.plot(...) or result=fig; then insert_image(image_path=<returned path>) on the slide/page."
    ),
}


def _resolve_plot_hint_doc_type(*, doc_type: str | None = None, agent_label: str | None = None) -> str | None:
    if agent_label:
        label_map = {"Calc": "calc", "Writer": "writer", "Draw": "draw"}
        doc_type = label_map.get(agent_label, doc_type)
    if doc_type in ("impress",):
        doc_type = "draw"
    return doc_type


def format_matplotlib_plot_hint(*, doc_type: str | None = None, agent_label: str | None = None) -> str:
    """Return a single-sentence plot egress hint for the active app, or \"\" if unknown."""
    resolved = _resolve_plot_hint_doc_type(doc_type=doc_type, agent_label=agent_label)
    if not resolved:
        return ""
    return _MATPLOTLIB_PLOT_HINTS.get(resolved, "")


def format_units_helper_hint() -> str:
    """Return guidance to prefer trusted units helpers over raw pint imports."""
    return (
        "For unit conversion and dimensional analysis, prefer the units tool or run_units helper "
        "over raw import pint. Raw pint remains available for custom registries, contexts, and definitions."
    )
