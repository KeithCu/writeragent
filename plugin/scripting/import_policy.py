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
    "scientific and stdlib modules (the user's venv typically includes NumPy, pandas, SciPy, and "
    "more—do not waste turns probing what is installed; use them). This sandbox has no networking, "
    "no filesystem or process escape, and no direct document access. Pass inputs via data/data_range; "
    "assign outputs to result."
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
        "Pre-imported (do not write import lines): np, pd, sp, math. "
        "DO NOT import numpy, pandas, sympy, or math. "
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
        parts.append(
            "Whitelisted packages (numpy, pandas, scipy, sklearn, matplotlib, seaborn, sympy, "
            "statsmodels, networkx, PIL, cv2, …) must be installed in the user venv."
        )
    else:
        stdlib = _join_modules(_venv_stdlib_modules())
        packages = _join_modules(_venv_package_modules())
        common = _join_modules(_VENV_COMMON_BLOCKED)
        parts.append(f"Allowed stdlib in this sandbox: {stdlib}.")
        parts.append(f"Allowed packages in this sandbox (+ submodules where applicable): {packages}.")
        parts.append(f"Always blocked in this sandbox: {blocked_security}.")
        parts.append(f"Common not-whitelisted (will fail): {common}, and anything else not listed above.")
        parts.append("Whitelisted packages must be installed in the user venv or import fails at runtime.")

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
