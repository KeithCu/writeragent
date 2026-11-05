# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress/Draw master slide tools.

Master collection order is not slide order, master names are not guaranteed
unique (``slide_master_set`` assigns the first name match), and the
assignment is per slide (``page.MasterPage``); the same quirks apply to
master chrome in ``headers_footers.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSlideMastersBase
from plugin.draw.bridge import DrawBridge
from plugin.framework.errors import is_disposal
from plugin.framework.errors import name_miss_details

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def iter_masters(doc: Any) -> list[tuple[int, Any, str]]:
    """Enumerate master pages as (index, master_page, name)."""
    if doc is None or not hasattr(doc, "getMasterPages"):
        return []
    try:
        masters = doc.getMasterPages()
    except Exception as exc:
        if is_disposal(exc):
            raise
        return []
    result: list[tuple[int, Any, str]] = []
    try:
        count = int(masters.getCount())
    except Exception as exc:
        if is_disposal(exc):
            raise
        return []
    for i in range(count):
        try:
            m = masters.getByIndex(i)
            name = str(getattr(m, "Name", "") or "")
            result.append((i, m, name))
        except Exception as exc:
            if is_disposal(exc):
                raise
            break
    return result


def find_master(doc: Any, *, name: str | None = None, index: int | None = None) -> Any | None:
    """Find a master page by name or index."""
    if name is not None:
        for _idx, m, m_name in iter_masters(doc):
            if m_name == name:
                return m
        return None
    if index is not None:
        for idx, m, _m_name in iter_masters(doc):
            if idx == index:
                return m
        return None
    return None


class ListMasterSlides(ToolDrawSlideMastersBase):
    """List all master slides in a Draw/Impress document."""

    name: str | None = "slide_master_list"
    description: str = "List all master slides (master pages) in the document with name and dimensions."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        result = []
        for i, m, name in iter_masters(ctx.doc):
            result.append({
                "index": i,
                "name": name,
                "width_mm": m.Width // 100,
                "height_mm": m.Height // 100,
            })
        return {"status": "ok", "master_slides": result, "count": len(result)}


class GetSlideMaster(ToolDrawSlideMastersBase):
    """Get which master slide is assigned to a slide."""

    name: str | None = "slide_master_get"
    description: str = "Get the master slide assigned to a specific slide. Returns the master slide name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        return {"status": "ok", "page": actual_idx, "master": getattr(page.MasterPage, "Name", "")}


class SetSlideMaster(ToolDrawSlideMastersBase):
    """Assign a master slide to a slide."""

    name: str | None = "slide_master_set"
    description: str = "Assign a master slide to a specific slide by master name. Use slide_master_list to see available masters."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}, "master": {"type": "string", "description": "Name of the master slide to assign."}}, "required": ["master"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        master_name = str(kwargs.get("master") or "").strip()
        if not master_name:
            return self._tool_error("master is required.")

        page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

        all_masters = iter_masters(ctx.doc)
        target = next((m for _i, m, name in all_masters if name == master_name), None)

        if target is None:
            available = [name for _i, _m, name in all_masters]
            return self._tool_error(
                "Master '%s' not found." % master_name,
                available=available,
                **name_miss_details(master_name, available, "list_master_slides"),
            )

        page.MasterPage = target
        return {"status": "ok", "page": actual_idx, "master": target.Name}
