# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Venv / in-process Python sandbox import policy for LLM prompts.

Derived from ``DANGEROUS_MODULES``, ``NETWORK_BLOCKED_MODULES``, and
``FILE_BLOCKED_MODULES`` — single source of truth for agent-facing import guidance.
"""

from __future__ import annotations

from plugin.framework.constants import AUTO_IMPORTS
from plugin.framework.deal_shim import UNDER_CROSSHAIR, ascii_bounded, deal
from plugin.scripting.sandbox import (
    DANGEROUS_MODULES,
    FILE_BLOCKED_MODULES,
    NETWORK_BLOCKED_MODULES,
    build_sandbox_policy,
    policy_blocked_imports,
)

PYTHON_VENV_SANDBOX_CONTEXT_PREFIX = (
    "PYTHON VENV SANDBOX: You are running in a Python sandbox (AST import blocklist) inside a "
    "same-user venv subprocess — not a second OS account, and not LibreOffice/UNO. "
    "Pass inputs via data/data_range; assign outputs (any type: string, list, NumPy array, "
    "or dictionary with structured keys) to the 'result' variable. Prefer NumPy arrays in 'result' for faster serialization. "
    "Note: While the sandboxed venv has NumPy/Pandas, the LibreOffice host environment does not. "
    "Therefore, the specialized_workflow_finished tool API only accepts basic Python types (strings, lists, numbers, dicts)."
)


@deal.post(lambda result: isinstance(result, tuple) and len(result) > 0)
def venv_blocked_modules() -> tuple[str, ...]:
    """Default blocked modules from sandbox policy."""
    return tuple(sorted(policy_blocked_imports(build_sandbox_policy())))


def _host_escape_blocked_list() -> str:
    escapes = [m for m in DANGEROUS_MODULES if m not in ("plugin", "writeragent.framework")]
    return ", ".join(escapes) + "; plugin internals"


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
    host_escape = _host_escape_blocked_list()
    networking = ", ".join(NETWORK_BLOCKED_MODULES)
    file_writes = ", ".join(FILE_BLOCKED_MODULES)

    parts = [PYTHON_VENV_SANDBOX_CONTEXT_PREFIX, auto_imports]

    if compact:
        parts.append(
            f"Blocked in this sandbox: host escape ({host_escape}); "
            f"networking ({networking}); "
            f"file writes ({file_writes}, plus the open() builtin). "
            "All other imports are allowed. Extra modules in Settings override this list; each also allows name.*."
        )
    else:
        parts.append(f"Blocked host escape (unless Allow OS access): {host_escape}.")
        parts.append(f"Blocked networking (unless Allow network access): {networking}.")
        parts.append(f"Blocked file writes (unless Allow file access): {file_writes} (plus the open() builtin).")
        parts.append("All other imports are allowed. Extra modules in Settings override this list; each also allows name.*.")

    return " ".join(parts)




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
