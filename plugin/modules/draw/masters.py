# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Impress/Draw master slide tools."""

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.framework.tool_base import ToolBase


def _get_slide(doc, page_index=None):
    """Resolve a slide by index or active."""
    pages = doc.getDrawPages()
    if page_index is not None:
        if page_index < 0 or page_index >= pages.getCount():
            raise ToolExecutionError("Page index %d out of range." % page_index)
        return pages.getByIndex(page_index)
    controller = doc.getCurrentController()
    if hasattr(controller, "getCurrentPage"):
        return controller.getCurrentPage()
    return pages.getByIndex(0)


class ListMasterSlides(ToolBase):
    """List all master slides in a Draw/Impress document."""

    name = "list_master_slides"
    intent = "navigate"
    description = (
        "List all master slides (master pages) in the document "
        "with name and dimensions."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    doc_types = ["draw", "impress"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        masters = doc.getMasterPages()
        result = []
        for i in range(masters.getCount()):
            m = masters.getByIndex(i)
            entry = {
                "index": i,
                "name": m.Name if hasattr(m, "Name") else "",
            }
            try:
                entry["width_mm"] = m.Width // 100
                entry["height_mm"] = m.Height // 100
            except Exception:
                pass
            result.append(entry)
        return {"status": "ok", "master_slides": result, "count": len(result)}
class GetSlideMaster(ToolBase):
    """Get which master slide is assigned to a slide."""

    name = "get_slide_master"
    intent = "navigate"
    description = (
        "Get the master slide assigned to a specific slide. "
        "Returns the master slide name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
        },
        "required": [],
    }
    doc_types = ["draw", "impress"]

    def execute(self, ctx, **kwargs):
        page = _get_slide(ctx.doc, kwargs.get("page_index"))
        master = page.MasterPage
        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "master_name": master.Name if hasattr(master, "Name") else "",
        }
class SetSlideMaster(ToolBase):
    """Assign a master slide to a slide."""

    name = "set_slide_master"
    intent = "edit"
    description = (
        "Assign a master slide to a specific slide by master name. "
        "Use list_master_slides to see available masters."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (active slide if omitted).",
            },
            "master_name": {
                "type": "string",
                "description": "Name of the master slide to assign.",
            },
        },
        "required": ["master_name"],
    }
    doc_types = ["draw", "impress"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        page = _get_slide(doc, kwargs.get("page_index"))
        master_name = kwargs["master_name"]

        masters = doc.getMasterPages()
        target = None
        for i in range(masters.getCount()):
            m = masters.getByIndex(i)
            if hasattr(m, "Name") and m.Name == master_name:
                target = m
                break

        if target is None:
            available = []
            for i in range(masters.getCount()):
                m = masters.getByIndex(i)
                available.append(m.Name if hasattr(m, "Name") else "")
            return self._tool_error(
                "Master '%s' not found." % master_name,
                available=available,
            )

        page.MasterPage = target
        return {
            "status": "ok",
            "page_index": kwargs.get("page_index"),
            "master_name": master_name,
        }
