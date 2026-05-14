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
        "get_speaker_notes",
        "set_speaker_notes",
        "get_slide_transition",
        "set_slide_transition",
        "get_slide_layout",
        "set_slide_layout",
        "list_master_slides",
        "get_slide_master",
        "set_slide_master",
    }
)


def test_impress_default_tool_schemas_exclude_slide_specialized_apis() -> None:
    from plugin.main import get_tools

    registry = get_tools()
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=_impress_doc())}
    assert _SLIDE_SPECIALIZED_NAMES.isdisjoint(names)


@pytest.mark.parametrize(
    ("domain", "expected_subset"),
    [
        ("speaker_notes", {"get_speaker_notes", "set_speaker_notes"}),
        (
            "slide_transitions",
            {
                "get_slide_transition",
                "set_slide_transition",
                "get_slide_layout",
                "set_slide_layout",
            },
        ),
        ("slide_masters", {"list_master_slides", "get_slide_master", "set_slide_master"}),
    ],
)
def test_impress_active_domain_includes_slide_specialized_tools(domain: str, expected_subset: set[str]) -> None:
    from plugin.main import get_tools

    registry = get_tools()
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=_impress_doc(), active_domain=domain)}
    assert "specialized_workflow_finished" in names
    assert expected_subset <= names
