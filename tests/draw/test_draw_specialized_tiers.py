# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Speaker notes, slide transitions, and master slide tools are specialized (not default Impress schemas)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _impress_doc() -> MagicMock:
    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.presentation.PresentationDocument"

    doc.supportsService = supports
    return doc


_SLIDE_SPECIALIZED_NAMES = frozenset(
    {
        "slide_get_notes",
        "slide_set_notes",
        "slide_get_transition",
        "slide_set_transition",
        "slide_get_layout",
        "slide_set_layout",
        "slide_list_masters",
        "slide_get_master",
        "slide_set_master",
        "page_comment_list",
        "page_comment_add",
        "page_comment_delete",
    }
)


_IMPRESS_DESIGN_CORE = frozenset({"design_list", "design_apply"})


def test_impress_default_tool_schemas_exclude_slide_specialized_apis() -> None:
    from plugin.app import get_tools

    registry = get_tools()
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=_impress_doc())}
    assert _SLIDE_SPECIALIZED_NAMES.isdisjoint(names)
    assert _IMPRESS_DESIGN_CORE <= names
    assert "set_presentation_design" not in names
    assert "transform_document_structure" not in names


@pytest.mark.parametrize(
    ("domain", "expected_subset"),
    [
        ("speaker_notes", {"slide_get_notes", "slide_set_notes"}),
        (
            "slide_transitions",
            {
                "slide_get_transition",
                "slide_set_transition",
            },
        ),
        (
            "slide_layouts",
            {
                "slide_get_layout",
                "slide_set_layout",
            },
        ),
        ("slide_masters", {"slide_list_masters", "slide_get_master", "slide_set_master"}),
        (
            "page_comments",
            {
                "page_comment_list",
                "page_comment_add",
                "page_comment_delete",
            },
        ),
        ("tables", {"table_insert", "table_list", "table_get_cells", "table_set_cell", "table_manage_structure", "table_delete"}),
        ("images", {"image_insert", "image_list", "image_delete", "image_generate"}),
        (
            "shapes",
            {"shape_align", "shape_distribute", "shape_create_diagram", "shape_fill_fields"},
        ),
    ],
)
def test_impress_active_domain_includes_slide_specialized_tools(domain: str, expected_subset: set[str]) -> None:
    from plugin.app import get_tools

    registry = get_tools()
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=_impress_doc(), active_domain=domain)}
    assert "specialized_workflow_finished" in names
    assert expected_subset <= names


def test_drawing_tool_descriptions_name_no_impress_only_tool() -> None:
    from plugin.app import get_tools
    from plugin.framework.doc_type import DRAW_DOCUMENT_SERVICE

    draw_doc = MagicMock()
    draw_doc.supportsService.side_effect = lambda s: s == DRAW_DOCUMENT_SERVICE

    registry = get_tools()
    draw_openai = registry.get_schemas("openai", doc=draw_doc)
    impress_openai = registry.get_schemas("openai", doc=_impress_doc())
    impress_only = {s["function"]["name"] for s in impress_openai} - {s["function"]["name"] for s in draw_openai}
    assert impress_only

    for schema in draw_openai:
        desc = schema["function"]["description"]
        for name in impress_only:
            assert name not in desc, f"{schema['function']['name']} OpenAI description names {name}"

    for schema in registry.get_schemas("mcp", doc=draw_doc):
        desc = schema["description"]
        for name in impress_only:
            assert name not in desc, f"{schema['name']} MCP description names {name}"


def test_impress_tables_domain_omits_writer_only_merge_split() -> None:
    from plugin.app import get_tools

    registry = get_tools()
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=_impress_doc(), active_domain="tables")}
    assert "table_merge_cells" not in names
    assert "table_split_cell" not in names
