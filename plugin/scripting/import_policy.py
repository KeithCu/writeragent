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
from plugin.framework.deal_shim import deal
from plugin.scripting.sandbox import (
    DANGEROUS_MODULES,
    FILE_BLOCKED_MODULES,
    NETWORK_BLOCKED_MODULES,
    _PROCESS_MODULES,
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

_ALLOW_ALL_NOTE = (
    "All other non-internal imports are allowed. Extra modules in Settings override this list; "
    "each also allows name.*."
)


@deal.post(lambda result: isinstance(result, tuple) and len(result) > 0)
def venv_blocked_modules() -> tuple[str, ...]:
    """Default blocked modules from sandbox policy."""
    return tuple(sorted(policy_blocked_imports(build_sandbox_policy())))


def _auto_import_alias(module_name: str, import_stmt: str) -> str:
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
    do_not = ", ".join(modules[:-1]) + f", or {modules[-1]}"
    return ", ".join(aliases), do_not


def format_venv_import_policy_for_prompt(*, compact: bool = False) -> str:
    """Sandbox context prefix first, then import rules for LLM prompts."""
    aliases, do_not_import = _auto_imports_prompt_lists()
    auto_imports = (
        f"Pre-imported (do not write import lines): {aliases}. "
        "When =PY has data range args, xl(\"%Pn%\") is also injected (binding-only Excel bridge; not a live sheet read). "
        f"DO NOT import {do_not_import}. "
        "Prefer np/sp/pd/st and scipy over hand-rolled Python; use dt for dates, plt for charts."
    )
    host_escape = (
        ", ".join(
            m
            for m in DANGEROUS_MODULES
            if m not in ("plugin", "writeragent.framework") and m not in _PROCESS_MODULES
        )
        + "; plugin internals"
    )
    processes = ", ".join(m for m in DANGEROUS_MODULES if m in _PROCESS_MODULES)
    networking = ", ".join(NETWORK_BLOCKED_MODULES)
    file_writes = ", ".join(FILE_BLOCKED_MODULES)

    parts = [PYTHON_VENV_SANDBOX_CONTEXT_PREFIX, auto_imports]

    if compact:
        parts.append(
            f"Blocked in this sandbox: host escape ({host_escape}); "
            f"running programs ({processes}, raw os) unless Allow OS access; "
            f"networking ({networking}); "
            f"file read/write access ({file_writes}, open, pathlib, and os file reads) unless Allow file access. {_ALLOW_ALL_NOTE}"
        )
    else:
        parts.append(f"Blocked host escape (by default): {host_escape}.")
        parts.append(f"Blocked running programs (unless Allow OS access): {processes}, raw os.")
        parts.append(f"Blocked networking (unless Allow network access): {networking}.")
        parts.append(
            f"Blocked file read/write access (unless Allow file access): {file_writes}, "
            "open (builtin, io, codecs, _pyio), pathlib reads and mutations, and os file reads (listdir, scandir, stat, walk)."
        )
        parts.append(_ALLOW_ALL_NOTE)

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
