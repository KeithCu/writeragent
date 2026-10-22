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
from typing import Any, ClassVar, Iterator

from plugin.doc.document_helpers import is_cacheable_doc_key
from plugin.doc.paragraph_search import confirm_paragraph_index, find_paragraph_for_range, get_paragraph_ranges
from plugin.framework.errors import best_effort, reraise_if_disposed
from plugin.framework.service import ServiceBase
from plugin.framework.uno_listeners import BaseDocumentEventListener
from ..specialized_base import ToolWriterBookmarkBase

log = logging.getLogger("writeragent.writer.nav.bookmarks")


# In-flight strip records keyed by DocumentService.doc_key (uid: survives Save As).
_PENDING_STRIPS: dict[str, _PendingStrip] = {}
_SAVE_LISTENERS: dict[str, _BookmarkSaveListener] = {}
# Doc keys with a save-hook strip/restore running, so ensure does not restore mid-write.
_ACTIVE_SAVE_HOOK_KEYS: set[str] = set()


class _PendingStrip:
    names: dict[int, str]
    was_modified: bool | None
    doc: Any
    __slots__: ClassVar[tuple[str, ...]] = ("names", "was_modified", "doc")

    def __init__(self, names: dict[int, str], was_modified: bool | None, doc: Any) -> None:
        self.names = dict(names)
        self.was_modified = was_modified if was_modified is None else bool(was_modified)
        self.doc = doc


@contextlib.contextmanager
def _in_save_hook(doc_key: str) -> Iterator[None]:
    _ACTIVE_SAVE_HOOK_KEYS.add(doc_key)
    try:
        yield
    finally:
        _ACTIVE_SAVE_HOOK_KEYS.discard(doc_key)


class _BookmarkSaveListener(BaseDocumentEventListener):
    """Strip ``_mcp_`` heading bookmarks just before the file is written.

    Nelson ``4c92ea12`` / #2644: insert used to dirty the document, leave
    undo steps, and persist agent locators into ``.odt`` / ``.docx``. Strip
    on ``OnSave*`` (not only ``OnSaveDone``) so the written file is clean;
    restore the same names after ``*Done``.
    """

    _svc: BookmarkService
    _doc_key: str

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
            with _in_save_hook(self._doc_key):
                self._svc.strip_for_save(source)
        elif name in ("OnSaveDone", "OnSaveAsDone"):
            with _in_save_hook(self._doc_key):
                self._svc.restore_after_save(source, saved=True)
        elif name in ("OnSaveToDone", "OnCopyToDone"):
            with _in_save_hook(self._doc_key):
                self._svc.restore_after_save(source, saved=False)
        elif name == "OnUnload":
            self._svc._teardown_save_listener(self._doc_key, source)

    def on_disposing(self, Source: Any) -> None:
        self._svc._teardown_save_listener(self._doc_key, Source)


class BookmarkService(ServiceBase):
    """Manage _mcp_ bookmarks on headings for stable addressing."""

    name: str | None = "writer_bookmarks"

    _doc_svc: Any

    def __init__(self, services: Any = None) -> None:
        # Optional so unit/UNO tests can still call BookmarkService().
        # ServiceRegistry passes the registry; document is used for
        # ignore_cache_invalidation() and doc_key().
        self._doc_svc = getattr(services, "document", None) if services is not None else None

    def _doc_key(self, doc: Any) -> str:
        if self._doc_svc is not None:
            return self._doc_svc.doc_key(doc)
        from plugin.doc.document_helpers import _compute_doc_key

        return _compute_doc_key(doc)

    def _ignore_cache(self) -> Any:
        if self._doc_svc is not None:
            return self._doc_svc.ignore_cache_invalidation()
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def _untracked(self, doc: Any) -> Iterator[None]:
        """Lock undo, ignore cache invalidation, restore isModified.

        insertTextContent / removeTextContent mark the document modified and
        push undo steps. Outline reads must not do either: a user who only
        asked for the heading tree would see "Save changes?" and Ctrl+Z
        would undo a bookmark instead of their last edit.
        """
        was: bool | None = None
        try:
            was = bool(doc.isModified())
        except Exception:
            was = None
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
            if was is not None:
                try:
                    doc.setModified(was)
                except Exception:
                    log.debug("BookmarkService: setModified(%s) failed", was, exc_info=True)

    def _read_mcp_bookmark_map_and_names(
        self, doc: Any, para_ranges: list[Any] | None = None, strict: bool = False
    ) -> tuple[dict[int, str], list[str]]:
        """Return ({para_index: bookmark_name}, all_bookmark_names).

        A failed read returns ({}, []) unless *strict*; callers reading strictly
        abort without modifying locators.
        """
        result: dict[int, str] = {}
        names_list: list[str] = []
        try:
            bookmarks = doc.getBookmarks()
            names = bookmarks.getElementNames()
            if not names:
                return result, names_list
            names_list = [n for n in names if isinstance(n, str)]
            mcp_names = [n for n in names_list if n.startswith("_mcp_")]
            if not mcp_names:
                return result, names_list
            if para_ranges is None:
                para_ranges = get_paragraph_ranges(doc)
            from plugin.doc.paragraph_search import paragraph_entries

            entries = paragraph_entries(para_ranges)
            text_obj = doc.getText()
            for name in mcp_names:
                bm = bookmarks.getByName(name)
                anchor = bm.getAnchor()
                para_idx = find_paragraph_for_range(anchor, para_ranges, text_obj, entries=entries)
                # A failed placement returns 0, so a stale _mcp_ mark must not be
                # stored as the bookmark for the first paragraph.
                # confirm_paragraph_index keeps 0 only when the anchor is actually
                # there. An unplaced mark is omitted and later removed.
                placed = confirm_paragraph_index(text_obj, anchor, para_ranges, para_idx)
                if placed is not None:
                    result[placed] = name
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("Failed to get MCP bookmark map")
            if not strict:
                return {}, []
            raise

        return result, names_list

    def get_mcp_bookmark_map(
        self, doc: Any, para_ranges: list[Any] | None = None, strict: bool = False
    ) -> dict[int, str]:
        """Return {para_index: bookmark_name} for all _mcp_ bookmarks.

        A failed read returns {} unless *strict*; ensure_heading_bookmarks reads
        strictly so a bad read does not insert or prune locators.
        """
        bm_map, _ = self._read_mcp_bookmark_map_and_names(doc, para_ranges=para_ranges, strict=strict)
        return bm_map

    def ensure_heading_bookmarks(
        self,
        doc: Any,
        heading_para_indices: list[int] | None = None,
        para_ranges: list[Any] | None = None,
    ) -> dict[int, str]:
        """Ensure every heading has an _mcp_ bookmark. Returns map.

        Also removes ``_mcp_`` bookmarks that are no longer on a heading.
        Demoting a heading used to leave the point mark in the document forever.
        """
        key = self._doc_key(doc)
        if key not in _ACTIVE_SAVE_HOOK_KEYS:
            self._restore_abandoned(doc)
        try:
            if para_ranges is None:
                para_ranges = get_paragraph_ranges(doc)
            existing_map, all_names = self._read_mcp_bookmark_map_and_names(
                doc, para_ranges=para_ranges, strict=True
            )
        except Exception as exc:
            reraise_if_disposed(exc)
            log.warning("heading bookmark map unreadable; leaving locators unchanged")
            return {}

        text = doc.getText()
        bookmark_map = {}
        needs_bookmark = []

        if heading_para_indices is not None:
            for para_index in heading_para_indices:
                if para_index in existing_map:
                    bookmark_map[para_index] = existing_map[para_index]
                elif 0 <= para_index < len(para_ranges):
                    needs_bookmark.append((para_index, para_ranges[para_index].getStart()))
        else:
            for para_index, element in enumerate(para_ranges):
                if element.supportsService("com.sun.star.text.Paragraph"):
                    outline_level = 0
                    try:
                        outline_level = element.getPropertyValue("OutlineLevel")
                    except Exception:
                        log.debug("ensure_heading_bookmarks: OutlineLevel unreadable", exc_info=True)
                    if outline_level > 0:
                        if para_index in existing_map:
                            bookmark_map[para_index] = existing_map[para_index]
                        else:
                            needs_bookmark.append((para_index, element.getStart()))

        # Names already on a current heading. New inserts are not in the
        # document yet, so they cannot appear in this stale list.
        kept = set(bookmark_map.values())
        stale = self._demoted_mcp_names(all_names, kept) if key not in _ACTIVE_SAVE_HOOK_KEYS else []

        if needs_bookmark or stale:
            with self._untracked(doc):
                for para_idx, start_range in needs_bookmark:
                    bm_name = None
                    for _attempt in range(5):
                        candidate = "_mcp_%s" % uuid.uuid4().hex[:8]
                        try:
                            if not doc.getBookmarks().hasByName(candidate):
                                bm_name = candidate
                                break
                        except Exception as exc:
                            reraise_if_disposed(exc)
                            log.exception("Failed checking bookmark existence for %s", candidate)
                            break
                    if bm_name is None:
                        log.warning("Failed to find unique _mcp_ bookmark name after 5 attempts")
                        continue
                    if self._insert_named_bookmark(doc, text, bm_name, start_range):
                        bookmark_map[para_idx] = bm_name

                # Adding marks without removing them leaves a demoted heading's
                # _mcp_ bookmark in place, so later navigation still treats that
                # paragraph as the section. Delete _mcp_ names that are not on a
                # heading now. Skipped during the save hook, which strips and
                # restores the whole set itself. User bookmarks are not _mcp_.
                if stale:
                    self._remove_named_bookmarks(doc, stale)

        # No doc.store(): this runs inside READ tools. Locators stay in
        # memory and are stripped from the next user Save.
        self._ensure_save_listener(doc)
        return bookmark_map

    def _demoted_mcp_names(self, names: list[str] | tuple[str, ...], kept: set[str]) -> list[str]:
        """``_mcp_`` names that are not on a current heading."""
        stale: list[str] = []
        for name in names:
            if isinstance(name, str) and name.startswith("_mcp_") and name not in kept:
                stale.append(name)
        return stale

    def _remove_named_bookmarks(self, doc: Any, names: list[str]) -> int:
        """Remove these bookmarks. Caller holds ``_untracked``. Returns failure count."""
        if not names:
            return 0
        try:
            bookmarks = doc.getBookmarks()
            text = doc.getText()
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("Failed to access bookmarks for removal")
            return len(names)
        failed = 0
        for name in names:
            try:
                bm = bookmarks.getByName(name)
                text.removeTextContent(bm)
            except Exception as exc:
                reraise_if_disposed(exc)
                failed += 1
                log.exception("Failed to remove demoted heading bookmark %s", name)
        return failed

    def find_nearest_heading_bookmark(self, para_index: int, bookmark_map: Any) -> dict[str, Any] | None:
        """Find nearest heading bookmark at or before para_index."""
        best_idx = -1
        for idx in bookmark_map:
            if idx <= para_index and idx > best_idx:
                best_idx = idx
        if best_idx >= 0:
            return {"bookmark": bookmark_map[best_idx], "heading_para_index": best_idx}
        return None

    def _remove_mcp_bookmarks(self, doc: Any) -> int:
        """Remove all ``_mcp_*`` bookmarks. Returns removed count."""
        removed = 0
        try:
            bookmarks = doc.getBookmarks()
            names = bookmarks.getElementNames()
            text = doc.getText()
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("Failed to get bookmarks for removal")
            raise
        for name in names:
            if isinstance(name, str) and name.startswith("_mcp_"):
                try:
                    bm = bookmarks.getByName(name)
                    text.removeTextContent(bm)
                    removed += 1
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.exception("Failed to remove _mcp_ bookmark %s", name)
        return removed

    def cleanup_mcp_bookmarks(self, doc: Any) -> int:
        """Remove all _mcp_* bookmarks from the document."""
        with self._untracked(doc):
            return self._remove_mcp_bookmarks(doc)

    def _insert_named_bookmark(self, doc: Any, text: Any, name: str, start_range: Any) -> bool:
        """Insert a point bookmark. Skip and log on name collision."""
        try:
            if doc.getBookmarks().hasByName(name):
                log.info("Heading bookmark %s already exists; skip insert", name)
                return False
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("Failed checking bookmark existence for %s", name)
            return False
        try:
            bookmark = doc.createInstance("com.sun.star.text.Bookmark")
            bookmark.Name = name
            cursor = text.createTextCursorByRange(start_range)
            text.insertTextContent(cursor, bookmark, False)
            return True
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("Failed to insert heading bookmark %s", name)
            return False

    def _pending_key(self, doc: Any) -> str | None:
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
            was: bool | None = None
            try:
                was = bool(doc.isModified())
            except Exception:
                was = None
            try:
                bm_map = self.get_mcp_bookmark_map(doc)
            except Exception as exc:
                reraise_if_disposed(exc)
                return
            _PENDING_STRIPS[key] = _PendingStrip(bm_map, was, doc)
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
        rec = _PENDING_STRIPS.get(key)
        if rec is None:
            return
        target = doc if doc is not None else rec.doc
        if target is None:
            return
        leftovers: dict[int, str] = {}
        try:
            with self._untracked(target):
                leftovers = self._reinsert_named(target, rec.names)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("Failed during bookmark restore; leaving pending record")
            return

        if leftovers:
            rec.names = leftovers
        else:
            _PENDING_STRIPS.pop(key, None)

        try:
            if saved:
                target.setModified(False)
            elif rec.was_modified is not None:
                target.setModified(rec.was_modified)
        except Exception:
            log.debug("BookmarkService: post-restore setModified failed", exc_info=True)

    def _reinsert_named(self, doc: Any, names: dict[int, str]) -> dict[int, str]:
        """Re-insert bookmarks. Returns leftover uninserted names."""
        if not names:
            return {}
        try:
            text = doc.getText()
            paras = get_paragraph_ranges(doc)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("Failed getting text or paragraph ranges for restore")
            return dict(names)
        leftovers: dict[int, str] = {}
        for para_idx, name in names.items():
            if para_idx < 0 or para_idx >= len(paras):
                continue
            element = paras[para_idx]
            try:
                if not element.supportsService("com.sun.star.text.Paragraph"):
                    continue
                start_range = element.getStart()
            except Exception as exc:
                reraise_if_disposed(exc)
                continue
            if not self._insert_named_bookmark(doc, text, name, start_range):
                leftovers[para_idx] = name
        return leftovers

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
            best_effort(
                lambda: source.removeDocumentEventListener(listener),
                "heading bookmarks: removeDocumentEventListener failed",
            )


# ── Bookmark Tools ────────────────────────────────────────────────────


class BookmarkList(ToolWriterBookmarkBase):
    name: str | None = "bookmark_list"
    description: str = "List all bookmarks in the document with their anchor text preview. Includes both user bookmarks and _mcp_ heading bookmarks."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        bookmarks = doc.getBookmarks()
        names = bookmarks.getElementNames()
        result = []
        for name in names:
            try:
                bm = bookmarks.getByName(name)
                anchor = bm.getAnchor()
                anchor_text = anchor.getString() if anchor is not None else ""
                result.append({"name": name, "text": anchor_text[:100] if anchor_text else ""})
            except Exception as exc:
                reraise_if_disposed(exc)
                log.warning("BookmarkList: failed to read bookmark %r: %s", name, exc)
                result.append({"name": name, "error": str(exc), "stale": True})
        return {"status": "ok", "bookmarks": result, "count": len(result)}


class BookmarkCleanup(ToolWriterBookmarkBase):
    name: str | None = "bookmark_cleanup"
    description: str = "Remove all _mcp_* bookmarks from the document. Use when bookmarks become stale after major edits."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        bm_svc = getattr(ctx.services, "writer_bookmarks", None)
        if bm_svc is None:
            bm_svc = BookmarkService(ctx.services)
        try:
            removed = bm_svc.cleanup_mcp_bookmarks(ctx.doc)
            return {"status": "ok", "removed": removed}
        except Exception as exc:
            reraise_if_disposed(exc)
            return self._tool_error(str(exc))


class BookmarkCreate(ToolWriterBookmarkBase):
    name: str | None = "bookmark_create"
    description: str = "Create a new bookmark at the current cursor or selection in Writer. If text is selected, the bookmark will span the selection."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "The unique name for the new bookmark."}}, "required": ["name"]}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        name = kwargs["name"]
        if not name:
            return self._tool_error("Bookmark name is required.")
        if name.startswith("_mcp_"):
            return self._tool_error("Bookmark names starting with '_mcp_' are reserved for internal heading locators. Use bookmark_cleanup to manage them.")

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


class BookmarkDelete(ToolWriterBookmarkBase):
    name: str | None = "bookmark_delete"
    description: str = "Delete an existing bookmark by its name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the bookmark to delete."}}, "required": ["name"]}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        name = kwargs["name"]
        if not name:
            return self._tool_error("Bookmark name is required.")
        if name.startswith("_mcp_"):
            return self._tool_error(
                "Bookmark names starting with '_mcp_' are reserved for internal heading locators. Use bookmark_cleanup to manage them."
            )

        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(name):
            return self._tool_error(f"Bookmark '{name}' not found.")

        # doc.getText() removes bookmarks anchored in the body, headers, tables and frames
        # alike, and needs no working anchor (checked live on LO 25.2).
        try:
            doc.getText().removeTextContent(bookmarks.getByName(name))
        except Exception as exc:
            reraise_if_disposed(exc)
            if bookmarks.hasByName(name):
                raise
            return {"status": "ok", "message": f"Bookmark '{name}' was already removed."}

        return {"status": "ok", "message": f"Bookmark '{name}' deleted."}


class BookmarkRename(ToolWriterBookmarkBase):
    name: str | None = "bookmark_rename"
    description: str = "Rename an existing bookmark."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"old_name": {"type": "string", "description": "The current name of the bookmark."}, "new_name": {"type": "string", "description": "The new name for the bookmark."}}, "required": ["old_name", "new_name"]}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        old_name = kwargs["old_name"]
        new_name = kwargs["new_name"]

        if not old_name or not new_name:
            return self._tool_error("Both old_name and new_name are required.")
        if old_name.startswith("_mcp_") or new_name.startswith("_mcp_"):
            return self._tool_error("Bookmark names starting with '_mcp_' are reserved for internal heading locators. Use bookmark_cleanup to manage them.")

        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(old_name):
            return self._tool_error(f"Bookmark '{old_name}' not found.")

        if bookmarks.hasByName(new_name):
            return self._tool_error(f"A bookmark named '{new_name}' already exists.")

        bm = bookmarks.getByName(old_name)
        bm.setName(new_name)

        return {"status": "ok", "message": f"Bookmark renamed from '{old_name}' to '{new_name}'."}



class BookmarkGet(ToolWriterBookmarkBase):
    name: str | None = "bookmark_get"
    description: str = "Get details about a specific bookmark, including the text it spans."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the bookmark."}}, "required": ["name"]}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        name = kwargs["name"]
        if not name:
            return self._tool_error("Bookmark name is required.")

        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(name):
            return self._tool_error(f"Bookmark '{name}' not found.")

        bm = bookmarks.getByName(name)
        try:
            anchor = bm.getAnchor()
            text_content = anchor.getString()
        except Exception as exc:
            reraise_if_disposed(exc)
            return self._tool_error(f"Bookmark '{name}' anchor is not in the document.", stale=True)

        return {"status": "ok", "bookmark": {"name": name, "text": text_content}}


class BookmarkResolve(ToolWriterBookmarkBase):
    """Resolve a bookmark to its paragraph index and heading text."""

    name: str | None = "bookmark_resolve"
    intent: str | None = "navigate"
    is_mutation: bool | None = False
    description: str = "Resolve a bookmark to its current paragraph index and text. Most tools accept 'bookmark:NAME' as locator directly -- use resolve_bookmark only when you need the raw paragraph index."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Bookmark name (e.g. _mcp_a1b2c3d4)."}}, "required": ["name"]}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        bookmark_name = kwargs["name"]
        if not bookmark_name:
            return self._tool_error("name is required.")

        doc = ctx.doc
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
        try:
            anchor = bm.getAnchor()
        except Exception as exc:
            reraise_if_disposed(exc)
            return self._tool_error("Bookmark '%s' anchor is not in the document." % bookmark_name)

        # Find paragraph index. A failed placement is 0, which is also a real
        # first paragraph — confirm before reporting it.
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)
        placed = confirm_paragraph_index(text_obj, anchor, para_ranges, para_idx)
        if placed is None:
            return self._tool_error(
                "Bookmark '%s' anchor is not in the document. It may be stale after an edit, save, or bookmark_cleanup."
                % bookmark_name
            )
        para_idx = placed

        result = {"status": "ok", "bookmark": bookmark_name, "paragraph_index": para_idx}

        # Get heading text if available
        if 0 <= para_idx < len(para_ranges):
            element = para_ranges[para_idx]
            if element.supportsService("com.sun.star.text.Paragraph"):
                try:
                    result["text"] = element.getString()
                    result["outline_level"] = element.getPropertyValue("OutlineLevel")
                except Exception:
                    log.debug("bookmark locate: heading text or outline level unreadable", exc_info=True)

        return result
