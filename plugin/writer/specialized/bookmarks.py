# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""BookmarkService and tools for Writer documents."""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any

from plugin.doc.document_helpers import is_cacheable_doc_key
from plugin.doc.paragraph_search import find_paragraph_for_range, get_paragraph_ranges
from plugin.framework.service import ServiceBase
from plugin.framework.uno_listeners import BaseDocumentEventListener
from ..specialized_base import ToolWriterBookmarkBase

log = logging.getLogger("writeragent.writer.nav.bookmarks")

# In-flight strip records keyed by DocumentService.doc_key (uid: survives Save As).
_PENDING_STRIPS: dict[str, _PendingStrip] = {}
_SAVE_LISTENERS: dict[str, _BookmarkSaveListener] = {}
# True while a save-hook strip/restore runs so ensure does not restore mid-write.
_SAVE_HOOK_DEPTH = 0


class _PendingStrip:
    __slots__ = ("names", "was_modified", "doc")

    def __init__(self, names: dict[int, str], was_modified: bool, doc: Any) -> None:
        self.names = dict(names)
        self.was_modified = bool(was_modified)
        self.doc = doc


@contextlib.contextmanager
def _in_save_hook():
    global _SAVE_HOOK_DEPTH
    _SAVE_HOOK_DEPTH += 1
    try:
        yield
    finally:
        _SAVE_HOOK_DEPTH -= 1


class _BookmarkSaveListener(BaseDocumentEventListener):
    """Strip ``_mcp_`` heading bookmarks just before the file is written.

    Nelson ``4c92ea12`` / #2644: insert used to dirty the document, leave
    undo steps, and persist agent locators into ``.odt`` / ``.docx``. Strip
    on ``OnSave*`` (not only ``OnSaveDone``) so the written file is clean;
    restore the same names after ``*Done``.
    """

    def __init__(self, svc: BookmarkService, doc_key: str) -> None:
        super().__init__()
        self._svc = svc
        self._doc_key = doc_key

    def on_document_event(self, Event: Any) -> None:
        try:
            name = getattr(Event, "EventName", "") or ""
        except Exception:
            return
        source = getattr(Event, "Source", None)
        # storeToURL ("Save a Copy") is OnCopyTo / OnCopyToDone, not OnSaveTo.
        # UI Save a Copy may still emit OnSaveTo; handle both.
        if name in ("OnPrepareSave", "OnSave", "OnSaveAs", "OnSaveTo", "OnCopyTo"):
            with _in_save_hook():
                self._svc.strip_for_save(source)
        elif name in ("OnSaveDone", "OnSaveAsDone"):
            with _in_save_hook():
                self._svc.restore_after_save(source, saved=True)
        elif name in ("OnSaveToDone", "OnCopyToDone"):
            with _in_save_hook():
                self._svc.restore_after_save(source, saved=False)
        elif name == "OnUnload":
            self._svc._teardown_save_listener(self._doc_key, source)

    def on_disposing(self, Source: Any) -> None:
        self._svc._teardown_save_listener(self._doc_key, Source)


class BookmarkService(ServiceBase):
    """Manage _mcp_ bookmarks on headings for stable addressing."""

    name = "writer_bookmarks"

    def __init__(self, services: Any = None) -> None:
        # Optional so unit/UNO tests can still call BookmarkService().
        # ServiceRegistry passes the registry; document is used for
        # ignore_cache_invalidation() and doc_key().
        self._doc_svc = getattr(services, "document", None) if services is not None else None

    def _doc_key(self, doc: Any):
        if self._doc_svc is not None:
            return self._doc_svc.doc_key(doc)
        from plugin.doc.document_helpers import _compute_doc_key

        return _compute_doc_key(doc)

    def _ignore_cache(self):
        if self._doc_svc is not None:
            return self._doc_svc.ignore_cache_invalidation()
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def _untracked(self, doc: Any):
        """Lock undo, ignore cache invalidation, restore isModified.

        insertTextContent / removeTextContent mark the document modified and
        push undo steps. Outline reads must not do either: a user who only
        asked for the heading tree would see "Save changes?" and Ctrl+Z
        would undo a bookmark instead of their last edit.
        """
        was = False
        try:
            was = bool(doc.isModified())
        except Exception:
            was = False
        manager = None
        try:
            manager = doc.getUndoManager()
            manager.lock()
        except Exception:
            manager = None
        try:
            with self._ignore_cache():
                yield
        finally:
            if manager is not None:
                try:
                    manager.unlock()
                except Exception:
                    log.warning(
                        "BookmarkService: undo manager unlock failed; the undo "
                        "manager may be left locked",
                        exc_info=True,
                    )
            try:
                doc.setModified(was)
            except Exception:
                log.debug("BookmarkService: setModified(%s) failed", was, exc_info=True)

    def get_mcp_bookmark_map(self, doc: Any):
        """Return {para_index: bookmark_name} for all _mcp_ bookmarks."""
        result: dict[int, str] = {}
        try:
            if not hasattr(doc, "getBookmarks"):
                return result
            bookmarks = doc.getBookmarks()
            names = bookmarks.getElementNames()
            if not names:
                return result
            para_ranges = get_paragraph_ranges(doc)
            text_obj = doc.getText()
            for name in names:
                if not name.startswith("_mcp_"):
                    continue
                bm = bookmarks.getByName(name)
                anchor = bm.getAnchor()
                para_idx = find_paragraph_for_range(anchor, para_ranges, text_obj)
                if para_idx >= 0:
                    result[para_idx] = name
        except Exception:
            log.exception("Failed to get MCP bookmark map")

        return result

    def ensure_heading_bookmarks(self, doc: Any):
        """Ensure every heading has an _mcp_ bookmark. Returns map."""
        if _SAVE_HOOK_DEPTH == 0:
            self._restore_abandoned(doc)
        existing_map = self.get_mcp_bookmark_map(doc)

        text = doc.getText()
        enum = text.createEnumeration()
        para_index = 0
        bookmark_map = {}
        needs_bookmark = []

        while enum.hasMoreElements():
            element = enum.nextElement()
            if element.supportsService("com.sun.star.text.Paragraph"):
                outline_level = 0
                try:
                    outline_level = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass
                if outline_level > 0:
                    if para_index in existing_map:
                        bookmark_map[para_index] = existing_map[para_index]
                    else:
                        needs_bookmark.append((para_index, element.getStart()))
            para_index += 1

        if needs_bookmark:
            with self._untracked(doc):
                for para_idx, start_range in needs_bookmark:
                    bm_name = "_mcp_%s" % uuid.uuid4().hex[:8]
                    if self._insert_named_bookmark(doc, text, bm_name, start_range):
                        bookmark_map[para_idx] = bm_name

        # No doc.store(): this runs inside READ tools. Locators stay in
        # memory and are stripped from the next user Save.
        self._ensure_save_listener(doc)
        return bookmark_map

    def find_nearest_heading_bookmark(self, para_index: int, bookmark_map: Any):
        """Find nearest heading bookmark at or before para_index."""
        best_idx = -1
        for idx in bookmark_map:
            if idx <= para_index and idx > best_idx:
                best_idx = idx
        if best_idx >= 0:
            return {"bookmark": bookmark_map[best_idx], "heading_para_index": best_idx}
        return None

    def _remove_mcp_bookmarks(self, doc: Any):
        """Remove all ``_mcp_*`` bookmarks. No undo/modified/cache handling."""
        removed = 0
        if not hasattr(doc, "getBookmarks"):
            return removed
        bookmarks = doc.getBookmarks()
        names = bookmarks.getElementNames()
        text = doc.getText()
        for name in names:
            if name.startswith("_mcp_"):
                try:
                    bm = bookmarks.getByName(name)
                    text.removeTextContent(bm)
                    removed += 1
                except Exception:
                    pass
        return removed

    def cleanup_mcp_bookmarks(self, doc: Any):
        """Remove all _mcp_* bookmarks from the document."""
        removed = 0
        try:
            with self._untracked(doc):
                removed = self._remove_mcp_bookmarks(doc)
        except Exception:
            log.exception("Failed to cleanup bookmarks")
        return removed

    def _insert_named_bookmark(self, doc: Any, text: Any, name: str, start_range: Any):
        """Insert a point bookmark. Skip and log on name collision."""
        try:
            if hasattr(doc, "getBookmarks") and doc.getBookmarks().hasByName(name):
                log.info("Heading bookmark %s already exists; skip insert", name)
                return False
        except Exception:
            pass
        try:
            bookmark = doc.createInstance("com.sun.star.text.Bookmark")
            bookmark.Name = name
            cursor = text.createTextCursorByRange(start_range)
            text.insertTextContent(cursor, bookmark, False)
            return True
        except Exception:
            log.exception("Failed to insert heading bookmark %s", name)
            return False

    def _pending_key(self, doc: Any):
        if doc is None:
            return None
        key = self._doc_key(doc)
        if key in _PENDING_STRIPS:
            return key
        from plugin.framework.uno_context import uno_same

        for stored_key, rec in _PENDING_STRIPS.items():
            if rec.doc is not None and uno_same(doc, rec.doc):
                return stored_key
        return None

    def _restore_abandoned(self, doc: Any) -> None:
        """LibreOffice has no OnSaveFailed; Done never firing leaves locators missing."""
        if self._pending_key(doc) is None:
            return
        self.restore_after_save(doc, saved=False)

    def strip_for_save(self, doc: Any) -> None:
        """Remove ``_mcp_`` bookmarks just before the file is written."""
        if doc is None:
            return
        key = self._pending_key(doc)
        if key is None:
            key = self._doc_key(doc)
            if not is_cacheable_doc_key(key):
                return
            was = False
            try:
                was = bool(doc.isModified())
            except Exception:
                was = False
            _PENDING_STRIPS[key] = _PendingStrip(self.get_mcp_bookmark_map(doc), was, doc)
        rec = _PENDING_STRIPS.get(key)
        if rec is None or not rec.names:
            return
        with self._untracked(doc):
            self._remove_mcp_bookmarks(doc)

    def restore_after_save(self, doc: Any, saved: bool) -> None:
        """Re-insert remembered names. ``saved=True`` keeps a successful Save clean."""
        key = self._pending_key(doc)
        if key is None:
            return
        rec = _PENDING_STRIPS.pop(key, None)
        if rec is None:
            return
        target = doc if doc is not None else rec.doc
        if target is None:
            return
        with self._untracked(target):
            self._reinsert_named(target, rec.names)
        try:
            if saved:
                target.setModified(False)
            else:
                target.setModified(rec.was_modified)
        except Exception:
            log.debug("BookmarkService: post-restore setModified failed", exc_info=True)

    def _reinsert_named(self, doc: Any, names: dict[int, str]) -> None:
        if not names:
            return
        text = doc.getText()
        paras = get_paragraph_ranges(doc)
        for para_idx, name in names.items():
            if para_idx < 0 or para_idx >= len(paras):
                continue
            element = paras[para_idx]
            try:
                if not element.supportsService("com.sun.star.text.Paragraph"):
                    continue
                start_range = element.getStart()
            except Exception:
                continue
            self._insert_named_bookmark(doc, text, name, start_range)

    def _ensure_save_listener(self, doc: Any) -> None:
        if not hasattr(doc, "addDocumentEventListener"):
            return
        key = self._doc_key(doc)
        if not is_cacheable_doc_key(key) or key in _SAVE_LISTENERS:
            return
        listener = _BookmarkSaveListener(self, key)
        try:
            doc.addDocumentEventListener(listener)
        except Exception:
            log.exception("Failed to attach heading-bookmark save listener")
            return
        _SAVE_LISTENERS[key] = listener

    def _teardown_save_listener(self, key: str, source: Any = None) -> None:
        _PENDING_STRIPS.pop(key, None)
        listener = _SAVE_LISTENERS.pop(key, None)
        if listener is None or source is None:
            return
        if hasattr(source, "removeDocumentEventListener"):
            try:
                source.removeDocumentEventListener(listener)
            except Exception:
                pass


# ── Bookmark Tools ────────────────────────────────────────────────────


class BookmarkList(ToolWriterBookmarkBase):
    name = "bookmark_list"
    description = "List all bookmarks in the document with their anchor text preview. Includes both user bookmarks and _mcp_ heading bookmarks."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any):
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
                result.append({"name": name, "text": anchor_text[:100] if anchor_text else ""})
            return {"status": "ok", "bookmarks": result, "count": len(result)}
        except Exception as e:
            return self._tool_error(f"Failed to list bookmarks: {str(e)}")


class BookmarkCleanup(ToolWriterBookmarkBase):
    name = "bookmark_cleanup"
    description = "Remove all _mcp_* bookmarks from the document. Use when bookmarks become stale after major edits."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any):
        bm_svc = ctx.services.writer_bookmarks
        removed = bm_svc.cleanup_mcp_bookmarks(ctx.doc)
        return {"status": "ok", "removed": removed}


class BookmarkCreate(ToolWriterBookmarkBase):
    name = "bookmark_create"
    description = "Create a new bookmark at the current cursor or selection in Writer. If text is selected, the bookmark will span the selection."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "The unique name for the new bookmark."}}, "required": ["name"]}
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any):
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


class BookmarkDelete(ToolWriterBookmarkBase):
    name = "bookmark_delete"
    description = "Delete an existing bookmark by its name."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the bookmark to delete."}}, "required": ["name"]}
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any):
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


class BookmarkRename(ToolWriterBookmarkBase):
    name = "bookmark_rename"
    description = "Rename an existing bookmark."
    parameters = {"type": "object", "properties": {"old_name": {"type": "string", "description": "The current name of the bookmark."}, "new_name": {"type": "string", "description": "The new name for the bookmark."}}, "required": ["old_name", "new_name"]}
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any):
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


class BookmarkGet(ToolWriterBookmarkBase):
    name = "bookmark_get"
    description = "Get details about a specific bookmark, including the text it spans."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the bookmark."}}, "required": ["name"]}

    def execute(self, ctx: Any, **kwargs: Any):
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

            return {"status": "ok", "bookmark": {"name": name, "text": text_content}}
        except Exception as e:
            return self._tool_error(f"Failed to get bookmark details: {str(e)}")


class BookmarkResolve(ToolWriterBookmarkBase):
    """Resolve a bookmark to its paragraph index and heading text."""

    name = "bookmark_resolve"
    intent = "navigate"
    is_mutation = False
    description = "Resolve a bookmark to its current paragraph index and text. Most tools accept 'bookmark:NAME' as locator directly -- use resolve_bookmark only when you need the raw paragraph index."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "Bookmark name (e.g. _mcp_a1b2c3d4)."}}, "required": ["name"]}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: Any, **kwargs: Any):
        bookmark_name = kwargs.get("name", "")
        if not bookmark_name:
            return self._tool_error("name is required.")

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

        result = {"status": "ok", "bookmark": bookmark_name, "paragraph_index": para_idx}

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
