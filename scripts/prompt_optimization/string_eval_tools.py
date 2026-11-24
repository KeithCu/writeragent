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

from plugin.framework.json_utils import safe_json_loads
from plugin.framework.tool import ToolContext, prepare_tool_call

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from eval_catalog import headless_registry
from eval_worlds import CalcWorld, DrawWorld, WriterWorld, unsupported_in_eval

World = WriterWorld | DrawWorld | CalcWorld

# Per world type: document kind, then production tool name -> world method. A method gets the
# arguments prepare_tool_call accepted, so it does not repeat that function's checks.
_IMPL: dict[type, tuple[str, dict[str, Callable[..., dict[str, Any]]]]] = {
    WriterWorld: (
        "writer",
        {
            "get_content": WriterWorld.get_content,
            "apply_content": WriterWorld.apply_content,
            "search": WriterWorld.search,
            "comment_add": WriterWorld.comment_add,
            "apply_style": WriterWorld.apply_style,
        },
    ),
    DrawWorld: (
        "draw",
        {
            "shape_upsert": DrawWorld.shape_upsert,
            "shape_connect": DrawWorld.shape_connect,
            "shape_group": DrawWorld.shape_group,
            "shape_get_tree": DrawWorld.get_tree,
            "shape_get_summary": DrawWorld.get_draw_summary,
        },
    ),
    CalcWorld: (
        "calc",
        {
            "sort_range": CalcWorld.sort_range,
            "write_range": CalcWorld.write_range,
            "get_sheet_summary": CalcWorld.get_sheet_summary,
            "read_range": CalcWorld.read_range,
        },
    ),
}


def _run(state: World, name: str, args: dict[str, Any]) -> dict[str, Any]:
    kind, impls = _IMPL[type(state)]
    impl = impls.get(name)
    if impl is None:
        return unsupported_in_eval(name)
    ctx = ToolContext(doc=None, ctx=None, doc_type=kind, services=None, caller="chat")
    # ToolRegistry.execute would run the real UNO tool, and it (not prepare_tool_call)
    # attaches ignored_parameters to a successful result.
    prepared = prepare_tool_call(headless_registry().get(name), ctx, name, args)
    if prepared.error is not None:
        return prepared.error
    result = impl(state, **prepared.kwargs)
    if prepared.ignored:
        result.setdefault("ignored_parameters", prepared.ignored)
    return result


def dispatch_string_tool(state: World, name: str, arguments_json: str) -> str:
    """Execute one tool by name; return JSON string for the assistant message."""
    args = safe_json_loads(arguments_json)
    if not isinstance(args, dict):
        args = {}
    try:
        res = _run(state, name, args)
    except Exception as exc:
        res = {"status": "error", "message": str(exc)}
    return json.dumps(res, ensure_ascii=False)
