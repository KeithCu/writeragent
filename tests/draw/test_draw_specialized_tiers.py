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
        "speaker_notes_get",
        "speaker_notes_set",
        "slide_transition_get",
        "slide_transition_set",
        "slide_layout_get",
        "slide_layout_set",
        "slide_master_list",
        "slide_master_get",
        "slide_master_set",
        "page_comment_list",
        "page_comment_add",
        "page_comment_delete",
    }
)


_IMPRESS_DESIGN_CORE = frozenset({"list_designs", "apply_design"})


def test_impress_default_tool_schemas_exclude_slide_specialized_apis() -> None:
    from plugin.app import get_tools

    registry = get_tools()
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=_impress_doc())}
    assert _SLIDE_SPECIALIZED_NAMES.isdisjoint(names)
    assert _IMPRESS_DESIGN_CORE <= names
    assert "set_presentation_design" not in names


@pytest.mark.parametrize(
    ("domain", "expected_subset"),
    [
        ("speaker_notes", {"speaker_notes_get", "speaker_notes_set"}),
        (
            "slide_transitions",
            {
                "slide_transition_get",
                "slide_transition_set",
            },
        ),
        (
            "slide_layouts",
            {
                "slide_layout_get",
                "slide_layout_set",
            },
        ),
        ("slide_masters", {"slide_master_list", "slide_master_get", "slide_master_set"}),
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
            {"shape_align", "shape_distribute", "shape_diagram", "shape_fill_fields"},
        ),
    ],
)
def test_impress_active_domain_includes_slide_specialized_tools(domain: str, expected_subset: set[str]) -> None:
    from plugin.app import get_tools

    registry = get_tools()
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=_impress_doc(), active_domain=domain)}
    assert "specialized_workflow_finished" in names
    assert expected_subset <= names


def test_draw_gateway_description_names_real_domains() -> None:
    from plugin.draw.specialized import DelegateToSpecializedDraw

    desc = DelegateToSpecializedDraw.description or ""
    assert "templates/design" not in desc
    for domain in [
        "shapes",
        "tables",
        "images",
        "charts",
        "forms",
        "math",
        "python",
        "layouts",
        "transitions",
        "masters",
        "notes",
        "headers",
    ]:
        assert domain in desc
    assert "page comments" in desc
