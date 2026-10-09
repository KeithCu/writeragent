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

import sys

from plugin.framework.constants import AUTO_IMPORTS
from plugin.scripting.sandbox import (
    BASE_BUILTIN_MODULES,
    CALC_AUTHORIZED_IMPORTS,
    DANGEROUS_MODULES,
    VENV_AUTHORIZED_IMPORTS,
    _VENV_STDLIB,
)

from plugin.framework.deal_shim import UNDER_CROSSHAIR, ascii_bounded, deal

# Stdlib roots from VENV_AUTHORIZED_IMPORTS beyond BASE_BUILTIN_MODULES.
_VENV_STDLIB_EXTRA: frozenset[str] = frozenset(_VENV_STDLIB)

# Dropped from the LLM allowed-packages line (substring, so a future
# plugin.scripting.duckdb_sql entry would stay out too). Still importable:
# session_duckdb() is the guarded helper; raw import duckdb is unguarded.
_PROMPT_OMIT_PACKAGE_MARKERS: tuple[str, ...] = ("duckdb",)

# Compact blurb "networking" list and common blocked modules.
_NETWORK_BLOCKED_COMMON: tuple[str, ...] = (
    "requests",
    "urllib",
    "urllib3",
    "http",
    "httpx",
    "ssl",
)

# socket is blocked via DANGEROUS_MODULES and is listed here explicitly.
_PROMPT_NETWORK_BLOCKED: tuple[str, ...] = ("socket",) + _NETWORK_BLOCKED_COMMON

# Not whitelisted — common LLM mistakes (guidance only; blocked at import check).
_VENV_COMMON_BLOCKED: tuple[str, ...] = _NETWORK_BLOCKED_COMMON + (
    "pickle",
    "sqlite3",
    "logging",
    "importlib",
    "ctypes",
    "threading",
)

PYTHON_VENV_SANDBOX_CONTEXT_PREFIX = (
    "PYTHON VENV SANDBOX: You are running in a Python sandbox (AST import whitelist) inside a "
    "same-user venv subprocess — not a second OS account, and not LibreOffice/UNO. "
    "Pass inputs via data/data_range; assign outputs (any type: string, list, NumPy array, "
    "or dictionary with structured keys) to the 'result' variable. Prefer NumPy arrays in 'result' for faster serialization. "
    "Note: While the sandboxed venv has NumPy/Pandas, the LibreOffice host environment does not. "
    "Therefore, the specialized_workflow_finished tool API only accepts basic Python types (strings, lists, numbers, dicts)."
)

INPROCESS_SANDBOX_CONTEXT_PREFIX = (
    "PYTHON IN-PROCESS SANDBOX: You are running in LibreOffice's embedded stdlib-only Python sandbox "
    "(not the user venv). Helpers lp/set_range read and write sheet cells; imports are stdlib-only."
)


@deal.post(lambda result: isinstance(result, tuple) and len(result) > 0)
def venv_authorized_top_level_modules() -> tuple[str, ...]:
    """Top-level module names allowed in the venv worker sandbox."""
    # Return real top-level module names, not dotted paths (plugin.scripting.*).
    # Intermediate nodes such as 'plugin' are not authorized top-level imports.
    roots: set[str] = set(BASE_BUILTIN_MODULES)
    for entry in VENV_AUTHORIZED_IMPORTS:
        if entry.startswith("plugin."):
            continue
        top = entry.split(".", 1)[0]
        roots.add(top)
    return tuple(sorted(roots))


def _known_stdlib_names() -> frozenset[str]:
    """Stdlib top-level names used to split the prompt's allowed lists.

    ``sys.stdlib_module_names`` (3.10+) tracks the running interpreter, so a
    stdlib name added to ``VENV_AUTHORIZED_IMPORTS`` is not labeled a package.
    Older LibreOffice Pythons use ``_VENV_STDLIB_EXTRA`` (locked to that set
    by ``test_venv_stdlib_extra_matches_authorized_stdlib``).
    """
    names = getattr(sys, "stdlib_module_names", None)
    if isinstance(names, frozenset):
        # getattr does not preserve the frozenset[str] parameter.
        return frozenset(name for name in names if isinstance(name, str))
    return frozenset(BASE_BUILTIN_MODULES) | _VENV_STDLIB_EXTRA


def _venv_stdlib_modules() -> tuple[str, ...]:
    stdlib = _known_stdlib_names()
    authorized_stdlib = {name for name in venv_authorized_top_level_modules() if name in stdlib}
    return tuple(sorted(set(BASE_BUILTIN_MODULES) | authorized_stdlib))


def _venv_package_modules() -> tuple[str, ...]:
    stdlib = set(_venv_stdlib_modules())
    return tuple(sorted(m for m in venv_authorized_top_level_modules() if m not in stdlib))


def _venv_writeragent_helpers() -> tuple[str, ...]:
    """WriterAgent scripting helpers exposed to sandbox code."""
    helpers: set[str] = set()
    for entry in VENV_AUTHORIZED_IMPORTS:
        if entry.startswith("writeragent.") and not entry.endswith(".*"):
            helpers.add(entry)
    return tuple(sorted(helpers))


def _omit_from_prompt_packages(name: str) -> bool:
    """True when an importable package must not appear in the =PY blurb."""
    lowered = name.lower()
    return any(marker in lowered for marker in _PROMPT_OMIT_PACKAGE_MARKERS)


@deal.post(lambda result: isinstance(result, tuple) and len(result) > 0)
def venv_blocked_modules() -> tuple[str, ...]:
    """Explicitly dangerous modules plus common not-whitelisted mistakes."""
    return tuple(sorted(set(DANGEROUS_MODULES) | set(_VENV_COMMON_BLOCKED)))


@deal.post(lambda result: isinstance(result, tuple) and len(result) > 0)
def inprocess_authorized_modules() -> tuple[str, ...]:
    """Modules allowed in LO embedded execute_python_script sandbox."""
    return CALC_AUTHORIZED_IMPORTS


def _join_modules(modules: tuple[str, ...]) -> str:
    return ", ".join(modules)


# _DEAL_ALIAS_MOD and _DEAL_ALIAS_STMT constrain string lengths of module names
# and import statements passed into _auto_import_alias during formal contract
# verification to avoid exponential SMT exploration while accommodating all
# AUTO_IMPORTS entries.
_DEAL_ALIAS_MOD = 32 if UNDER_CROSSHAIR else 64
_DEAL_ALIAS_STMT = 48 if UNDER_CROSSHAIR else 128


def _deal_auto_import_alias_ok(module_name: object, import_stmt: object) -> bool:
    return ascii_bounded(module_name, _DEAL_ALIAS_MOD) and ascii_bounded(
        import_stmt, _DEAL_ALIAS_STMT
    )


@deal.pre(lambda module_name, import_stmt: _deal_auto_import_alias_ok(module_name, import_stmt))
@deal.post(lambda result: isinstance(result, str))
def _auto_import_alias(module_name: str, import_stmt: str) -> str:
    # crosshair: off
    # rsplit/" as " walk still slow (check-all 33668189572: Prev 8:35 despite 32/48 ascii bounds). Doable later: closed AUTO_IMPORTS enum.
    marker = " as "
    if marker in import_stmt:
        return import_stmt.rsplit(marker, 1)[-1].strip()
    return module_name


def _auto_imports_prompt_lists() -> tuple[str, str]:
    """Alias list and module list for LLM prose, derived from AUTO_IMPORTS."""
    aliases: list[str] = []
    modules: list[str] = []
    for module_name, import_stmt in AUTO_IMPORTS.items():
        aliases.append(_auto_import_alias(module_name, import_stmt))
        modules.append(module_name)
    if not modules:
        return "", ""
    if len(modules) == 1:
        do_not = modules[0]
    else:
        do_not = ", ".join(modules[:-1]) + f", or {modules[-1]}"
    return ", ".join(aliases), do_not


@deal.post(lambda result: isinstance(result, str) and result.startswith(PYTHON_VENV_SANDBOX_CONTEXT_PREFIX))
def format_venv_import_policy_for_prompt(*, compact: bool = False) -> str:
    """Sandbox context prefix first, then import rules for LLM prompts."""
    aliases, do_not_import = _auto_imports_prompt_lists()
    auto_imports = (
        f"Pre-imported (do not write import lines): {aliases}. "
        "When =PY has data range args, xl(\"%Pn%\") is also injected (binding-only Excel bridge; not a live sheet read). "
        f"DO NOT import {do_not_import}. "
        "Prefer np/sp/pd/st and scipy over hand-rolled Python; use dt for dates, plt for charts."
    )
    blocked_security = _join_modules(tuple(sorted(DANGEROUS_MODULES)))
    blocked_network = _join_modules(_PROMPT_NETWORK_BLOCKED)

    parts = [PYTHON_VENV_SANDBOX_CONTEXT_PREFIX, auto_imports]

    if compact:
        parts.append(
            f"Blocked in this sandbox: host escape ({blocked_security}); "
            f"networking ({blocked_network}); other imports not on the whitelist fail."
        )
    else:
        stdlib = _join_modules(_venv_stdlib_modules())
        # duckdb stays on VENV_AUTHORIZED_IMPORTS. Listing it here would steer
        # default chat toward raw import duckdb instead of session_duckdb().
        packages_list = [m for m in _venv_package_modules() if not _omit_from_prompt_packages(m)]
        # Show writeragent.scripting helpers once (excluding plugin.* twins).
        helpers = [h for h in _venv_writeragent_helpers() if not _omit_from_prompt_packages(h)]
        packages = _join_modules(tuple(sorted(set(packages_list) | set(helpers))))
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
        "Do not call image_insert. Use data_range for sheet data."
    ),
    "writer": (
        "PLOTS: plt.plot(...) or result=fig; then image_insert(image_path=<returned path>). "
        "Use document tools for text/data."
    ),
    "draw": (
        "PLOTS: plt.plot(...) or result=fig; then image_insert(image_path=<returned path>) on the slide/page."
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
        "For unit conversion and dimensional analysis, use Run Python Script Units Helpers "
        "or the run_units helper."
    )
