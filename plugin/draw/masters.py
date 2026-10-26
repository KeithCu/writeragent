# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress/Draw master slide tools.

Master collection order is not slide order, master names are not guaranteed
unique (``set_slide_master`` assigns the first name match), and the
assignment is per slide (``page.MasterPage``); the same quirks apply to
master chrome in ``headers_footers.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawSlideMastersBase
from plugin.draw.bridge import resolve_shape_page

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _master_pages(doc: Any) -> list[Any]:
    masters = doc.getMasterPages()
    return [masters.getByIndex(i) for i in range(masters.getCount())]


class ListMasterSlides(ToolDrawSlideMastersBase):
    """List all master slides in a Draw/Impress document."""

    name: str | None = "list_master_slides"
    intent: str | None = "navigate"
    description: str = "List all master slides (master pages) in the document with name and dimensions."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        result = []
        for i, m in enumerate(_master_pages(doc)):
            entry = {
                "index": i,
                "name": getattr(m, "Name", ""),
                "width_mm": m.Width // 100,
                "height_mm": m.Height // 100,
            }
            result.append(entry)
        return {"status": "ok", "master_slides": result, "count": len(result)}


class GetSlideMaster(ToolDrawSlideMastersBase):
    """Get which master slide is assigned to a slide."""

    name: str | None = "get_slide_master"
    intent: str | None = "navigate"
    description: str = "Get the master slide assigned to a specific slide. Returns the master slide name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err:
            return self._tool_error(err)
        assert page is not None
        master = page.MasterPage
        name = getattr(master, "Name", "")
        return {"status": "ok", "page": actual_idx, "master": name}


class SetSlideMaster(ToolDrawSlideMastersBase):
    """Assign a master slide to a slide."""

    name: str | None = "set_slide_master"
    intent: str | None = "edit"
    description: str = "Assign a master slide to a specific slide by master name. Use list_master_slides to see available masters."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (active slide if omitted)."}, "master": {"type": "string", "description": "Name of the master slide to assign."}}, "required": ["master"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        master_name = str(kwargs.get("master") or "").strip()
        if not master_name:
            return self._tool_error("master is required.")

        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err:
            return self._tool_error(err)
        assert page is not None

        masters = _master_pages(ctx.doc)
        target = next((m for m in masters if getattr(m, "Name", "") == master_name), None)

        if target is None:
            available = [getattr(m, "Name", "") for m in masters]
            return self._tool_error("Master '%s' not found." % master_name, available=available)

        page.MasterPage = target
        return {"status": "ok", "page": actual_idx, "master": target.Name}
