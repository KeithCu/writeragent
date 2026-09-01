# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dispatch string-harness tools onto Writer/Draw/Calc worlds (no LibreOffice)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from plugin.framework.errors import safe_json_loads

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from eval_worlds import (  # noqa: F401 — re-export for existing imports
    CalcStringState,
    CalcWorld,
    DrawDocState,
    DrawWorld,
    StringDocState,
    WriterWorld,
    _unsupported,
    a1_to_col_row,
    normalize_apply_content,
)

# Legacy helper name used by tests / tools_lo comments.
_a1_to_col_row = a1_to_col_row
_normalize_apply_content = normalize_apply_content


def _writer_find_text(state: WriterWorld, args: dict[str, Any]) -> dict[str, Any]:
    return state.find_text(
        str(args.get("search") or args.get("pattern") or ""),
        start=int(args.get("start") or 0),
        limit=args.get("limit"),
        case_sensitive=bool(args.get("case_sensitive", True)),
    )


_WRITER_IMPL: dict[str, Callable[[WriterWorld, dict[str, Any]], dict[str, Any]]] = {
    "get_document_content": lambda s, a: s.get_document_content(**a),
    "apply_document_content": lambda s, a: s.apply_document_content(**a),
    "search_in_document": lambda s, a: s.search_in_document(**a),
    "find_text": _writer_find_text,
    "add_comment": lambda s, a: s.add_comment(**a),
}

_DRAW_IMPL: dict[str, Callable[[DrawWorld, dict[str, Any]], dict[str, Any]]] = {
    "shape_upsert": lambda s, a: s.shape_upsert(**a),
    "shape_connect": lambda s, a: s.shape_connect(**a),
    "shape_group": lambda s, a: s.shape_group(**a),
    "get_draw_tree": lambda s, a: s.get_draw_tree(**a),
    "shape_summary": lambda s, a: s.get_draw_summary(**a),
}

_CALC_IMPL: dict[str, Callable[[CalcWorld, dict[str, Any]], dict[str, Any]]] = {
    "sort_range": lambda s, a: s.sort_range(**a),
    "write_formula_range": lambda s, a: s.write_formula_range(**a),
    "write_cell_range": lambda s, a: s.write_formula_range(**a),
    "get_sheet_summary": lambda s, a: s.get_sheet_summary(**a),
    "read_cell_range": lambda s, a: s.read_cell_range(**a),
}


def dispatch_string_tool(
    state: WriterWorld | DrawWorld | CalcWorld, name: str, arguments_json: str
) -> str:
    """Execute one tool by name; return JSON string for the assistant message."""
    try:
        args = safe_json_loads(arguments_json)
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        if isinstance(state, CalcWorld):
            impl = _CALC_IMPL.get(name)
            res = impl(state, args) if impl else _unsupported(name)
        elif isinstance(state, DrawWorld):
            impl_d = _DRAW_IMPL.get(name)
            res = impl_d(state, args) if impl_d else _unsupported(name)
        elif isinstance(state, WriterWorld):
            impl_w = _WRITER_IMPL.get(name)
            res = impl_w(state, args) if impl_w else _unsupported(name)
        else:
            res = {"status": "error", "message": f"Unknown state type for tool {name}"}
    except Exception as exc:
        res = {"status": "error", "message": str(exc)}
    return json.dumps(res, ensure_ascii=False)
