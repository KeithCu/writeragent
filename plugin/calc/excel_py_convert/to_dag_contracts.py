# SPDX-License-Identifier: GPL-3.0-or-later
"""Contracts and verification domains for Excel/Calc PY DAG translation."""

from __future__ import annotations

from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    ascii_bounded,
    str_bounded,
)

# Pytest @deal bounds; do not add a second CrossHair domain back.
_DEAL_CONVERT_LIST = DEAL_MAX_CMD_ARGS
_DEAL_CONVERT_STR = DEAL_MAX_SOURCE
_DEAL_REWRITE_SRC = DEAL_MAX_SOURCE
_DEAL_BINDING_A1_LEN = DEAL_MAX_SOURCE
_DEAL_RESOLVED_LEN = DEAL_MAX_CMD_ARGS
_DEAL_NOTE_LEN = DEAL_MAX_SOURCE
_deal_note_ok = str_bounded
_RESOLVED_KINDS = frozenset(("range", "unresolved", "table_snapshot", "anchor_snapshot"))


def _deal_excel_src_ok(src: object) -> bool:
    return str_bounded(src, DEAL_MAX_SOURCE)


def _deal_convert_dep_ok(dep: object) -> bool:
    return isinstance(dep, str) and ascii_bounded(dep, DEAL_MAX_SOURCE)


def _deal_convert_scripts_ok(model: object) -> bool:  # pyright: ignore[reportUnusedFunction]
    # Scripts share _deal_excel_src_ok with _find_xl_calls.
    scripts = getattr(model, "scripts", None)
    return isinstance(scripts, list) and len(scripts) <= _DEAL_CONVERT_LIST and all(_deal_excel_src_ok(s) for s in scripts)


def _deal_convert_cell_ok(cell: object) -> bool:  # pyright: ignore[reportUnusedFunction]
    """Cell fields ``convert_cell_to_dag`` requires; script_index range is body-checked."""
    script_index = getattr(cell, "script_index", None)
    deps = getattr(cell, "deps", None)
    return type(script_index) is int and isinstance(deps, list) and len(deps) <= _DEAL_CONVERT_LIST and all(_deal_convert_dep_ok(d) for d in deps)
