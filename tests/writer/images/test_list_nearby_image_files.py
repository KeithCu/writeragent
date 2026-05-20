"""Tests for list_nearby_image_files (images specialized domain)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.framework.tool import ToolContext, ToolRegistry
from plugin.writer.images.images import ListNearbyImageFiles
from plugin.writer.specialized_base import SpecializedWorkflowFinished
from plugin.doc.document_research_tools import ListNearbyFiles
from tests.chatbot.test_tool_loop import DummyCalcSpecialTool


def test_list_nearby_image_files_calls_backend_with_images_kind():
    tool = ListNearbyImageFiles()
    ctx = ToolContext(MagicMock(), MagicMock(), "writer", MagicMock())

    expected = {"status": "ok", "files": [], "truncated": False}

    with patch("plugin.writer.images.images.list_nearby_files", return_value=expected) as mock_list:
        with patch("plugin.writer.images.images.execute_on_main_thread", side_effect=lambda fn: fn()):
            result = tool.execute(ctx, filter="logo")

    mock_list.assert_called_once_with(ctx.ctx, ctx.doc, filter="logo", file_kind="images")
    assert result == expected


def test_images_domain_includes_list_nearby_image_files_not_document_research_list():
    registry = ToolRegistry(services={})
    registry.register(ListNearbyImageFiles())
    registry.register(DummyCalcSpecialTool())
    registry.register(ListNearbyFiles())
    registry.register(SpecializedWorkflowFinished())

    mock_writer = MagicMock()
    mock_writer.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"

    tools = registry.get_tools(doc=mock_writer, active_domain="images", exclude_tiers=())
    names = {t.name for t in tools}

    assert "list_nearby_image_files" in names
    assert "list_nearby_files" not in names
    assert "specialized_workflow_finished" in names
