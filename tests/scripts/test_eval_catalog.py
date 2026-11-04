# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval tool catalog is live ToolRegistry.get_schemas (same as sidebar)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import pytest

import eval_catalog
from eval_catalog import (
    TOOL_PRESETS,
    apply_schema_density,
    apply_schema_patches,
    _headless_registry,
    build_eval_tool_schemas,
    filter_eval_tool_schemas,
    parse_tools_spec,
    prepare_eval_tool_schemas,
    resolve_tool_allowlist,
    schema_tool_name,
)
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


def _delegate_domain_enum(schemas: list[dict], tool_name: str) -> list[str] | None:
    for row in schemas:
        fn = row.get("function") if isinstance(row.get("function"), dict) else row
        if fn.get("name") != tool_name:
            continue
        props = (fn.get("parameters") or {}).get("properties") or {}
        domain = props.get("domain") or {}
        enum = domain.get("enum")
        return list(enum) if isinstance(enum, list) else None
    return None


def test_eval_schemas_match_sidebar_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eval headless registry uses the same hooks/filters as plugin.main bootstrap."""
    import plugin.app as app
    import plugin.bootstrap as bootstrap
    import plugin.main as main
    from plugin.bootstrap import BootstrapState
    from unittest.mock import MagicMock
    import tempfile

    eval_catalog._registry = None
    monkeypatch.setenv("WRITERAGENT_EVAL_HARNESS", "1")
    monkeypatch.setattr("plugin.framework.queue_executor.MainThread.on_python_main_thread", lambda: True)
    monkeypatch.setattr(main, "register_core_handlers", lambda: None)
    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", lambda fn: None)

    def _eval_like_modules(services: Any) -> list[Any]:
        from plugin.calc import CalcModule
        from plugin.chatbot import ChatbotModule
        from plugin.draw import DrawModule
        from plugin.writer import WriterModule

        WriterModule().initialize(services)
        CalcModule().initialize(services)
        DrawModule().initialize(services)
        ChatbotModule().initialize(services)
        services.get("tools").auto_discover_package("plugin.doc")
        return []

    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", _eval_like_modules)

    eval_catalog._registry = None
    try:
        bootstrap._bootstrap_state = BootstrapState.UNINITIALIZED
        app._services = None
        app._tools = None
        app._modules = []
        ctx = MagicMock()
        path_settings = ctx.getServiceManager.return_value.createInstanceWithContext.return_value
        path_settings.UserConfig = tempfile.mkdtemp(prefix="wa-eval-sidebar-")
        app.bootstrap(ctx)
        sidebar = app.get_tools()
        assert sidebar is not None
        eval_reg = _headless_registry()
        for kind in ("writer", "calc", "draw"):
            kwargs: dict = {"doc_type": kind, "filter_doc_type": True}
            sidebar_rows = sidebar.get_schemas("openai", **kwargs)
            eval_rows = eval_reg.get_schemas("openai", **kwargs)
            assert sidebar_rows == eval_rows
            delegate_name = f"delegate_to_specialized_{kind}_toolset"
            assert _delegate_domain_enum(sidebar_rows, delegate_name) == _delegate_domain_enum(
                eval_rows, delegate_name
            )
            assert _delegate_domain_enum(eval_rows, delegate_name) is not None
    finally:
        eval_catalog._registry = None


def test_eval_prompts_build_without_libreoffice() -> None:
    from scripts.prompt_optimization.eval_prompts import get_writer_eval_chat_system_prompt

    prompt = get_writer_eval_chat_system_prompt()
    assert "delegate_to_specialized_writer_toolset" in prompt
    assert "[Eval harness]" in prompt


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


def test_parse_tools_spec_presets_and_names() -> None:
    assert parse_tools_spec(None) == (None, None)
    assert parse_tools_spec("") == ("full", None)
    assert parse_tools_spec("full") == ("full", None)
    assert parse_tools_spec("calc_minimal") == ("calc_minimal", None)
    assert parse_tools_spec("write_formula_range, get_sheet_summary") == (
        None,
        ["write_formula_range", "get_sheet_summary"],
    )


def test_resolve_allowlist_kind_and_specialized() -> None:
    calc_min = list(TOOL_PRESETS["calc_minimal"][1] or ())
    assert resolve_tool_allowlist("calc_minimal", kind="calc") == calc_min
    # Writer tasks keep the full catalog when the preset is Calc-only.
    assert resolve_tool_allowlist("calc_minimal", kind="writer") is None
    assert resolve_tool_allowlist("writer_minimal", kind="writer") == list(
        TOOL_PRESETS["writer_minimal"][1] or ()
    )
    assert resolve_tool_allowlist("full", kind="calc") is None
    # Inner specialized catalogs are never name-filtered.
    assert (
        resolve_tool_allowlist("calc_minimal", kind="calc", active_domain="ranges")
        is None
    )
    assert resolve_tool_allowlist(
        "write_formula_range,get_sheet_summary", kind="calc"
    ) == ["write_formula_range", "get_sheet_summary"]


def test_filter_eval_tool_schemas_preserves_order_and_errors() -> None:
    schemas = build_eval_tool_schemas(kind="calc")
    names = ["get_sheet_summary", "write_formula_range"]
    filtered = filter_eval_tool_schemas(schemas, names)
    assert [schema_tool_name(row) for row in filtered] == names
    with pytest.raises(ValueError, match="Unknown eval tool name"):
        filter_eval_tool_schemas(schemas, ["not_a_real_tool"])
    with pytest.raises(ValueError, match="empty"):
        filter_eval_tool_schemas(schemas, [])


def test_calc_minimal_and_writer_minimal_are_production_subsets() -> None:
    calc = {schema_tool_name(s) for s in build_eval_tool_schemas(kind="calc")}
    writer = {schema_tool_name(s) for s in build_eval_tool_schemas(kind="writer")}
    assert set(TOOL_PRESETS["calc_minimal"][1] or ()) <= calc
    assert set(TOOL_PRESETS["calc_core"][1] or ()) <= calc
    assert set(TOOL_PRESETS["writer_minimal"][1] or ()) <= writer
    prepared = prepare_eval_tool_schemas(kind="calc", tools_spec="calc_minimal")
    assert [schema_tool_name(s) for s in prepared] == list(
        TOOL_PRESETS["calc_minimal"][1] or ()
    )
    # Writer kind + calc preset: no filter.
    writer_full = [schema_tool_name(s) for s in build_eval_tool_schemas(kind="writer")]
    writer_kept = [
        schema_tool_name(s)
        for s in prepare_eval_tool_schemas(kind="writer", tools_spec="calc_minimal")
    ]
    assert writer_kept == writer_full


def test_apply_schema_density_skinny_blanks_descriptions() -> None:
    schemas = build_eval_tool_schemas(kind="calc")
    fat = next(
        row
        for row in schemas
        if schema_tool_name(row) == "write_formula_range"
    )
    fat_fn = fat.get("function") or fat
    assert len(str(fat_fn.get("description") or "")) > 20
    skinny = apply_schema_density(schemas, "skinny")
    sk_fn = next(
        (row.get("function") or row)
        for row in skinny
        if schema_tool_name(row) == "write_formula_range"
    )
    assert sk_fn["name"] == "write_formula_range"
    assert sk_fn["description"] == ""
    props = (sk_fn.get("parameters") or {}).get("properties") or {}
    assert "range" in props
    assert props["range"].get("type") == (fat_fn.get("parameters") or {}).get(
        "properties", {}
    ).get("range", {}).get("type")
    assert props["range"].get("description") == ""
    # Original catalog is not mutated.
    assert len(str(fat_fn.get("description") or "")) > 20
    assert apply_schema_density(schemas, "full") is schemas
    with pytest.raises(ValueError, match="Unknown schema density"):
        apply_schema_density(schemas, "chunky")


def test_prepare_keeps_specialized_inner_schemas() -> None:
    outer = prepare_eval_tool_schemas(kind="calc", tools_spec="calc_minimal")
    assert "sort_range" not in {schema_tool_name(s) for s in outer}
    inner = prepare_eval_tool_schemas(
        kind="calc",
        active_domain="ranges",
        tools_spec="calc_minimal",
        schema_density="skinny",
    )
    names = {schema_tool_name(s) for s in inner}
    assert {"sort_range", "specialized_workflow_finished"} <= names
    sort_fn = next(
        (row.get("function") or row)
        for row in inner
        if schema_tool_name(row) == "sort_range"
    )
    assert sort_fn["description"] == ""


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
