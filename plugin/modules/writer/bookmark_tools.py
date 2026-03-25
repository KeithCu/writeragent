# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Bookmark listing/cleanup adapted from nelson-mcp (MPL 2.0):
# nelson-mcp/plugin/modules/writer_nav/tools/bookmarks.py
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Bookmark tools for the specialized bookmarks domain."""

from plugin.modules.writer.base import ToolWriterBookmarkBase


class ListBookmarks(ToolWriterBookmarkBase):
    name = "list_bookmarks"
    intent = "navigate"
    description = (
        "List all bookmarks in the document with their anchor text preview. "
        "Includes both user bookmarks and _mcp_ heading bookmarks."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getBookmarks"):
            return {"status": "ok", "bookmarks": [], "count": 0}
        try:
            bookmarks = doc.getBookmarks()
            names = bookmarks.getElementNames()
            result = []
            for name in names:
                bm = bookmarks.getByName(name)
                anchor_text = bm.getAnchor().getString()
                result.append({
                    "name": name,
                    "preview": anchor_text[:100] if anchor_text else "",
                })
            return {"status": "ok", "bookmarks": result, "count": len(result)}
        except Exception as e:
            return {"status": "error", "message": str(e)}


class CleanupBookmarks(ToolWriterBookmarkBase):
    name = "cleanup_bookmarks"
    intent = "navigate"
    description = (
        "Remove all _mcp_* bookmarks from the document. "
        "Use when bookmarks become stale after major edits."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bm_svc = ctx.services.writer_bookmarks
        removed = bm_svc.cleanup_mcp_bookmarks(ctx.doc)
        return {"status": "ok", "removed": removed}
