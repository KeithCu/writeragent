# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval tool catalog is larger than the old 3–5 hand-written schemas."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_catalog import build_eval_tool_schemas
from eval_worlds import CalcWorld, DrawWorld, WriterWorld
from string_eval_tools import dispatch_string_tool


def _names(kind: str) -> set[str]:
    return {str(s.get("name")) for s in build_eval_tool_schemas(kind=kind)}


def test_writer_catalog_has_production_names() -> None:
    names = _names("writer")
    assert {"get_document_content", "apply_document_content", "search_in_document"} <= names
    assert len(names) > 5


def test_draw_catalog_has_connect() -> None:
    names = _names("draw")
    assert {"shape_upsert", "shape_connect", "get_draw_tree"} <= names
    assert len(names) > 5


def test_calc_catalog_has_write_formula() -> None:
    names = _names("calc")
    assert {"write_formula_range", "read_cell_range", "get_sheet_summary", "sort_range"} <= names
    assert len(names) > 5


def test_unsupported_core_names() -> None:
    writer = WriterWorld("hi")
    draw = DrawWorld()
    calc = CalcWorld("A\t1")
    for state, name in (
        (writer, "apply_style"),
        (draw, "shape_delete"),
        (calc, "list_sheets"),
    ):
        data = json.loads(dispatch_string_tool(state, name, "{}"))
        assert data["status"] == "error"
        assert data["code"] == "unsupported_in_eval"
