# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_ppt_master_not_in_draw_delegate_enum():
    from plugin.draw.specialized import DelegateToSpecializedDraw

    gw = DelegateToSpecializedDraw()
    domains = gw.parameters["properties"]["domain"]["enum"]
    assert "ppt-master" not in domains


def test_sidebar_flags_impress_has_ppt_master():
    from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_PPT_MASTER, get_mode_labels, sidebar_mode_flags_for_doc_type

    flags = sidebar_mode_flags_for_doc_type("impress")
    assert flags.include_ppt_master is True
    assert flags.include_brainstorming is False
    labels = get_mode_labels(**flags.__dict__)
    assert mode_from_label_ppt_master(labels, flags) == CHAT_MODE_PPT_MASTER


def mode_from_label_ppt_master(labels, flags):
    from plugin.chatbot.chat_sidebar_mode import mode_from_label

    return mode_from_label(labels[-1], **flags.__dict__)


def test_sidebar_flags_writer_no_ppt_master():
    from plugin.chatbot.chat_sidebar_mode import sidebar_mode_flags_for_doc_type

    flags = sidebar_mode_flags_for_doc_type("writer")
    assert flags.include_ppt_master is False
    assert flags.include_brainstorming is True


def _impress_doc() -> MagicMock:
    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.presentation.PresentationDocument"

    doc.supportsService = supports
    return doc


def test_impress_default_schemas_exclude_ppt_master_tools():
    from plugin.framework.tool import ToolRegistry
    from plugin.draw.base import ToolDrawPptMasterBase
    from plugin.ppt_master import tools as ppt_master_tools  # noqa: F401 — register subclasses

    doc = _impress_doc()
    registry = ToolRegistry(MagicMock())
    for cls in ToolDrawPptMasterBase.__subclasses__():
        registry.register(cls())
    names = {s["function"]["name"] for s in registry.get_schemas("openai", doc=doc)}
    assert "export_presentation_project" not in names
    assert "get_ppt_master_skill_path" not in names


def test_ppt_master_session_no_longer_merges_draw_tools():
    import inspect

    from plugin.chatbot import ppt_master as pm

    source = inspect.getsource(pm)
    assert "_PPT_MASTER_DRAW_CORE_TOOL_NAMES" not in source
    assert "collect_ppt_master_tools" not in source
    assert "run_ppt_master_venv_turn" in source or "_run_ppt_master_venv_agent" in source
