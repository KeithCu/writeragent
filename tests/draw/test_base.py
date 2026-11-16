"""Tests for shared Draw/Impress tool schema constants."""

from unittest.mock import MagicMock

from plugin.draw.base import DRAW_PAGE_PROPERTY
from plugin.framework.tool import ToolRegistry


def test_optional_page_params_share_draw_page_property() -> None:
    """Every Draw/Impress tool whose optional `page` resolves via DrawBridge.resolve_page shares DRAW_PAGE_PROPERTY."""
    registry = ToolRegistry(MagicMock())
    registry.auto_discover_package("plugin.draw")
    tools = registry.get_tools(filter_doc_type=False, exclude_tiers=())

    excluded = {"page_comment_list", "slide_add"}
    matched: list[str] = []
    for tool in tools:
        if not type(tool).__module__.startswith("plugin.draw."):
            continue
        params = tool.parameters or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        if "page" in props and "page" not in required and tool.name not in excluded:
            assert props["page"] is DRAW_PAGE_PROPERTY, f"{tool.name} does not use DRAW_PAGE_PROPERTY"
            matched.append(tool.name or "")

    assert len(matched) == 24
