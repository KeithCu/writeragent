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
    description = "List all bookmarks in the document with their anchor text preview. Includes both user bookmarks and _mcp_ heading bookmarks."
    parameters = {"type": "object", "properties": {}, "required": []}

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
                result.append(
                    {
                        "name": name,
                        "text": anchor_text[:100] if anchor_text else "",
                    }
                )
            return {"status": "ok", "bookmarks": result, "count": len(result)}
        except Exception as e:
            return {"status": "error", "message": str(e)}


class CleanupBookmarks(ToolWriterBookmarkBase):
    name = "cleanup_bookmarks"
    description = "Remove all _mcp_* bookmarks from the document. Use when bookmarks become stale after major edits."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bm_svc = ctx.services.writer_bookmarks
        removed = bm_svc.cleanup_mcp_bookmarks(ctx.doc)
        return {"status": "ok", "removed": removed}


class CreateBookmark(ToolWriterBookmarkBase):
    name = "create_bookmark"
    description = "Create a new bookmark at the current cursor or selection in Writer. If text is selected, the bookmark will span the selection."
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The unique name for the new bookmark.",
            }
        },
        "required": ["name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("name")
        if not name:
            return self._tool_error("Bookmark name is required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if bookmarks.hasByName(name):
                return self._tool_error(f"A bookmark named '{name}' already exists.")

            ctrl = doc.getCurrentController()
            if not ctrl:
                return self._tool_error("No current controller found.")

            view_cursor = ctrl.getViewCursor()
            if not view_cursor:
                return self._tool_error("No view cursor found.")

            text = view_cursor.getText()
            if not text:
                return self._tool_error("Cannot get text from current cursor position.")

            bookmark = doc.createInstance("com.sun.star.text.Bookmark")
            bookmark.Name = name

            # insertTextContent signature: (XTextRange xRange, XTextContent xContent, boolean bAbsorb)
            # If bAbsorb is True, the text content replaces or spans the current selection.
            # If False, it's inserted as a point. We'll use True so if there's a selection, it's spanned.
            text.insertTextContent(view_cursor, bookmark, True)

            return {"status": "ok", "message": f"Bookmark '{name}' created."}
        except Exception as e:
            return self._tool_error(f"Failed to create bookmark: {str(e)}")


class DeleteBookmark(ToolWriterBookmarkBase):
    name = "delete_bookmark"
    description = "Delete an existing bookmark by its name."
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the bookmark to delete.",
            }
        },
        "required": ["name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("name")
        if not name:
            return self._tool_error("Bookmark name is required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if not bookmarks.hasByName(name):
                return self._tool_error(f"Bookmark '{name}' not found.")

            bm = bookmarks.getByName(name)
            anchor = bm.getAnchor()
            text = anchor.getText()

            text.removeTextContent(bm)

            return {"status": "ok", "message": f"Bookmark '{name}' deleted."}
        except Exception as e:
            return self._tool_error(f"Failed to delete bookmark: {str(e)}")


class RenameBookmark(ToolWriterBookmarkBase):
    name = "rename_bookmark"
    description = "Rename an existing bookmark."
    parameters = {
        "type": "object",
        "properties": {
            "old_name": {
                "type": "string",
                "description": "The current name of the bookmark.",
            },
            "new_name": {
                "type": "string",
                "description": "The new name for the bookmark.",
            },
        },
        "required": ["old_name", "new_name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        old_name = kwargs.get("old_name")
        new_name = kwargs.get("new_name")

        if not old_name or not new_name:
            return self._tool_error("Both old_name and new_name are required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if not bookmarks.hasByName(old_name):
                return self._tool_error(f"Bookmark '{old_name}' not found.")

            if bookmarks.hasByName(new_name):
                return self._tool_error(f"A bookmark named '{new_name}' already exists.")

            bm = bookmarks.getByName(old_name)
            bm.setName(new_name)

            return {"status": "ok", "message": f"Bookmark renamed from '{old_name}' to '{new_name}'."}
        except Exception as e:
            return self._tool_error(f"Failed to rename bookmark: {str(e)}")


class GetBookmark(ToolWriterBookmarkBase):
    name = "get_bookmark"
    description = "Get details about a specific bookmark, including the text it spans."
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the bookmark.",
            }
        },
        "required": ["name"],
    }

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("name")
        if not name:
            return self._tool_error("Bookmark name is required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if not bookmarks.hasByName(name):
                return self._tool_error(f"Bookmark '{name}' not found.")

            bm = bookmarks.getByName(name)
            anchor = bm.getAnchor()
            text_content = anchor.getString()

            return {
                "status": "ok",
                "bookmark": {
                    "name": name,
                    "text": text_content,
                },
            }
        except Exception as e:
            return self._tool_error(f"Failed to get bookmark details: {str(e)}")


class ResolveBookmark(ToolWriterBookmarkBase):
    """Resolve a bookmark to its paragraph index and heading text."""

    name = "resolve_bookmark"
    intent = "navigate"
    description = (
        "Resolve a bookmark to its current paragraph index and text. Most tools accept 'bookmark:NAME' as locator directly -- use resolve_bookmark only when you need the raw paragraph index."
    )
    parameters = {
        "type": "object",
        "properties": {
            "bookmark_name": {
                "type": "string",
                "description": "Bookmark name (e.g. _mcp_a1b2c3d4).",
            },
        },
        "required": ["bookmark_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        bookmark_name = kwargs.get("bookmark_name", "")
        if not bookmark_name:
            return self._tool_error("bookmark_name is required.")

        doc = ctx.doc
        if not hasattr(doc, "getBookmarks"):
            return self._tool_error("Document does not support bookmarks.")

        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(bookmark_name):
            hint = "Bookmark '%s' not found." % bookmark_name
            if bookmark_name.startswith("_mcp_"):
                hint += " It may have been deleted or the document changed. Use heading_text:<text> locator for resilient heading addressing, or call get_document_tree to refresh bookmarks."
                existing = [n for n in bookmarks.getElementNames() if n.startswith("_mcp_")]
                if existing:
                    hint += " Existing bookmarks: %s" % ", ".join(existing[:10])
            return self._tool_error(hint)

        bm = bookmarks.getByName(bookmark_name)
        anchor = bm.getAnchor()

        # Find paragraph index
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)

        result = {
            "status": "ok",
            "bookmark": bookmark_name,
            "paragraph_index": para_idx,
        }

        # Get heading text if available
        if 0 <= para_idx < len(para_ranges):
            element = para_ranges[para_idx]
            if element.supportsService("com.sun.star.text.Paragraph"):
                try:
                    result["text"] = element.getString()
                    result["outline_level"] = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass

        return result
