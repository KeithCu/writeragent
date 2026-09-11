# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval tool catalog is live ToolRegistry.get_schemas (same as sidebar)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_catalog import apply_schema_patches, _headless_registry, build_eval_tool_schemas
from eval_worlds import CalcWorld, DrawWorld, WriterWorld
from string_eval_tools import dispatch_string_tool


def _schema_name(row: dict) -> str:
    fn = row.get("function") if isinstance(row.get("function"), dict) else None
    if fn:
        return str(fn.get("name") or "")
    return str(row.get("name") or "")


def _names(kind: str) -> set[str]:
    return {_schema_name(s) for s in build_eval_tool_schemas(kind=kind) if _schema_name(s)}


def test_eval_catalog_matches_registry_get_schemas() -> None:
    registry = _headless_registry()
    for kind in ("writer", "calc", "draw"):
        live = {
            _schema_name(s)
            for s in registry.get_schemas("openai", doc_type=kind, filter_doc_type=True)
            if _schema_name(s)
        }
        assert _names(kind) == live


def test_writer_catalog_has_production_names() -> None:
    names = _names("writer")
    assert {
        "get_document_content",
        "apply_document_content",
        "search_in_document",
        "apply_style",
        "get_guidance",
    } <= names
    assert any("delegate" in n for n in names)
    assert len(names) > 8


def test_draw_catalog_has_core_names() -> None:
    names = _names("draw")
    # shape_upsert / shape_connect are specialized (delegate), not main-chat core.
    assert {"get_draw_tree", "list_pages", "delegate_to_specialized_draw_toolset"} <= names
    assert len(names) > 8


def test_calc_catalog_has_write_formula() -> None:
    names = _names("calc")
    # sort_range is specialized; main chat uses write_formula_range / read / summary.
    assert {
        "write_formula_range",
        "read_cell_range",
        "get_sheet_summary",
        "delegate_to_specialized_calc_toolset",
    } <= names
    assert len(names) > 8


def test_draw_shapes_domain_advertises_upsert() -> None:
    names = {_schema_name(s) for s in build_eval_tool_schemas(kind="draw", active_domain="shapes")}
    assert {"shape_upsert", "shape_connect", "fill_draw_fields", "specialized_workflow_finished"} <= names
    assert "shape_upsert" not in _names("draw")
    assert "fill_draw_fields" not in _names("draw")


def test_calc_ranges_domain_advertises_sort() -> None:
    names = {_schema_name(s) for s in build_eval_tool_schemas(kind="calc", active_domain="ranges")}
    assert {"sort_range", "specialized_workflow_finished"} <= names
    assert "sort_range" not in _names("calc")


def test_apply_schema_patches_rewrites_sort_range_description() -> None:
    schemas = build_eval_tool_schemas(kind="calc", active_domain="ranges")
    patched = apply_schema_patches(
        schemas, {"sort_range": {"description": "PATCHED_SORT_DESC"}}
    )
    found = False
    for row in patched:
        fn = row.get("function") if isinstance(row.get("function"), dict) else row
        if fn.get("name") == "sort_range":
            assert fn["description"] == "PATCHED_SORT_DESC"
            found = True
    assert found
    # Original catalog object is not mutated (deepcopy).
    original = next(
        row
        for row in schemas
        if (row.get("function") or row).get("name") == "sort_range"
    )
    orig_fn = original.get("function") or original
    assert orig_fn["description"] != "PATCHED_SORT_DESC"


def test_apply_schema_patches_rewrites_values_param() -> None:
    schemas = build_eval_tool_schemas(kind="calc")
    patched = apply_schema_patches(
        schemas,
        {"write_formula_range": {"parameters": {"values": "PATCHED_VALUES_DESC"}}},
    )
    fn = next(
        (row.get("function") or row)
        for row in patched
        if (row.get("function") or row).get("name") == "write_formula_range"
    )
    assert fn["parameters"]["properties"]["values"]["description"] == "PATCHED_VALUES_DESC"


def test_unsupported_core_names() -> None:
    writer = WriterWorld("hi")
    draw = DrawWorld()
    calc = CalcWorld("A\t1")
    for state, name in (
        (writer, "get_guidance"),
        (draw, "shape_delete"),
        (calc, "list_sheets"),
    ):
        data = json.loads(dispatch_string_tool(state, name, "{}"))
        assert data["status"] == "error"
        assert data["code"] == "unsupported_in_eval"
