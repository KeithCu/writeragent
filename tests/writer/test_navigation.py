# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for navigation tools (nav_heading, nav_surroundings, nav_heading_children)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.framework.errors import ToolExecutionError
from plugin.writer.navigation import NavHeading, NavHeadingChildren, NavSurroundings


def _ctx(prox=None, tree=None):
    return SimpleNamespace(doc=object(), services=SimpleNamespace(writer_proximity=prox, writer_tree=tree))


def test_nav_tools_declare_read_only():
    for tool_cls in (NavHeading, NavSurroundings, NavHeadingChildren):
        assert tool_cls().detects_mutation() is False, tool_cls.name


def test_nav_heading_returns_service_error_dict():
    prox = MagicMock()
    prox.navigate_heading.return_value = {"error": "No next heading — already at last heading"}
    result = NavHeading().execute(_ctx(prox=prox), locator="paragraph:0", direction="next")
    assert result["status"] == "error"
    assert "No next heading" in result["message"]


def test_nav_heading_returns_raised_service_error():
    prox = MagicMock()
    prox.navigate_heading.side_effect = ToolExecutionError("Cannot resolve locator: nope")
    result = NavHeading().execute(_ctx(prox=prox), locator="nope", direction="next")
    assert result["status"] == "error"
    assert "Cannot resolve locator" in result["message"]


def test_nav_surroundings_ok_envelope():
    prox = MagicMock()
    prox.get_surroundings.return_value = {"center_para_index": 2}
    result = NavSurroundings().execute(_ctx(prox=prox), locator="paragraph:2")
    assert result == {"status": "ok", "center_para_index": 2}


def test_nav_heading_children_rejects_multiple_selectors():
    tree = MagicMock()
    result = NavHeadingChildren().execute(_ctx(tree=tree), locator="heading:1", bookmark="_mcp_a")
    assert result["status"] == "error"
    assert "only one of locator" in result["message"]
    tree.get_heading_children.assert_not_called()


def test_nav_heading_children_passes_a_single_selector():
    tree = MagicMock()
    tree.get_heading_children.return_value = {"parent": {"text": "A"}, "children": []}
    result = NavHeadingChildren().execute(_ctx(tree=tree), locator="heading:1")
    assert result["status"] == "ok"
    assert tree.get_heading_children.call_args.kwargs["locator"] == "heading:1"
