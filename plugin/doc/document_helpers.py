# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
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
"""Writer chat-context assembler and ``DocumentService``.

Text/path/selection helpers live in ``plugin.doc.text_helpers`` (LibrePy-safe).
Document resolution lives in ``plugin.framework.uno_context``. Streamed Writer
edits live in ``plugin.writer.edit_review``. Calc chat context lives in
``plugin.calc.analyzer``. Draw/Impress chat context lives in
``plugin.draw.bridge``. Paragraph range helpers live in
``plugin.doc.paragraph_search``. Do not re-export those names here — a
re-export of ``get_calc_context_for_chat`` would pull ``SheetAnalyzer`` at
import time and break LibrePy.
"""
from __future__ import annotations

import logging
import weakref
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Generator

from plugin.doc import doc_type as _doc_type
from plugin.doc import text_helpers as _text_helpers
from plugin.doc.paragraph_search import (
    find_paragraph_for_range as _find_paragraph_for_range,
    get_paragraph_ranges as _get_paragraph_ranges,
)
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.errors import (
    UnoObjectError,
    check_disposed,
    safe_call,
)
from plugin.framework.service import ServiceBase
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import (
    get_active_document,
    get_ctx,
    get_runtime_uid,
    normalize_doc_url,
    resolve_document_by_url as _resolve_document_by_url,
)
log = logging.getLogger("writeragent.document")

# Cache key when RuntimeUID and URL are both missing. Must not collide across
# docs — callers must not store under this sentinel (see is_cacheable_doc_key).
UNKNOWN_DOC_KEY = "unknown"

# One modify+unload pair per open document, keyed by doc_key (not id(doc)).
# Module-level so extra DocumentService() objects in tests do not double-attach.
_CACHE_LISTENERS: dict[str, "_CacheListenerPair"] = {}
_IGNORE_DEPTH = 0

# Do not import uno_listeners here: that module imports unohelper at load,
# which breaks the isolated document_helpers import (LibrePy / no soffice).
_unohelper: Any = None
_XDocumentEventListener: Any = object
_XModifyListener: Any = object
_HAVE_UNO_LISTENERS = False
try:
    import unohelper as _unohelper_impl
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl
    from com.sun.star.util import XModifyListener as _XModifyListener_impl

    _unohelper = _unohelper_impl
    _XDocumentEventListener = _XDocumentEventListener_impl
    _XModifyListener = _XModifyListener_impl
    _HAVE_UNO_LISTENERS = True
except Exception:
    pass


@main_thread_only
def get_full_document_text(model: Any, max_chars: int = CHAT_DOCUMENT_CONTEXT_MAX_CHARS) -> str:
    """Dispatch full-text / summary by document type.

    Writer slices live in ``text_helpers``. Draw/Impress summaries live on
    ``plugin.draw.bridge``. Calc is lazy so this module does not load
    ``SheetAnalyzer`` at import time.
    """
    try:
        check_disposed(model, "Document Model")
        doc_type = _doc_type.get_document_type(model)

        if doc_type == _doc_type.DocumentType.CALC:
            from plugin.calc.analyzer import get_full_calc_text

            return get_full_calc_text(model, max_chars)

        if doc_type == _doc_type.DocumentType.WRITER:
            return _text_helpers.get_full_writer_text(model, max_chars)

        if doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            from plugin.draw.bridge import get_draw_context_for_chat

            return get_draw_context_for_chat(model, max_chars)

        return ""
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_full_document_text failed")
        return ""


def _writer_has_math_ole(model: Any) -> bool:
    """True when the Writer doc has at least one LibreOffice Math embedded object."""
    try:
        from plugin.writer.math.math_mml_convert import MATH_CLSID

        container = model.getEmbeddedObjects()
        names = container.getElementNames()
        # Index by length — iterating a MagicMock in unit tests would never end.
        if names is None:
            return False
        n = len(names)
        for i in range(n):
            obj = container.getByName(names[i])
            if str(getattr(obj, "CLSID", "") or "").lower() == MATH_CLSID.lower():
                return True
    except Exception:
        return False
    return False


def _with_math_ole_chat_hint(model: Any, body: str) -> str:
    """Plain-text excerpts skip Math OLE; point the model at get_document_content."""
    if not _writer_has_math_ole(model):
        return body
    return (
        body
        + "\n\nMath formulas are LibreOffice Math objects (OLE), not characters in the "
        "excerpt above, so Document length may omit them. Call get_document_content to "
        "read them as TeX."
    )


@main_thread_only
def get_document_context_for_chat(
    model: Any,
    max_context: int = CHAT_DOCUMENT_CONTEXT_MAX_CHARS,
    include_end: bool = True,
    include_selection: bool = True,
    ctx: Any | None = None,
) -> str:
    """Build a single context string for chat. Handles Writer, Calc and Draw.
    ctx: component context (required for Calc and Draw documents)."""
    try:
        doc_type = _doc_type.get_document_type(model)

        if doc_type == _doc_type.DocumentType.CALC:
            from plugin.calc.analyzer import get_calc_context_for_chat

            return get_calc_context_for_chat(model, max_context, ctx)

        if doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            from plugin.draw.bridge import get_draw_context_for_chat

            return get_draw_context_for_chat(model, max_context, ctx)

        # Writer: plain-text start/end slices (hides tracked deletions). Math OLE is not
        # in getString(); we only hint to call get_document_content.
        if doc_type == _doc_type.DocumentType.WRITER:
            try:
                check_disposed(model, "Document Model")
                doc_len = _text_helpers._writer_char_count(model)
            except (UnoObjectError, Exception):
                logging.getLogger(__name__).exception("get_document_context_for_chat Writer failed, trying fallback to selection-only")
                sel_text = _text_helpers.get_selection_text(model)
                if sel_text:
                    return f"[Document text reading failed. Active selection: {sel_text}]"
                return "[Document content unavailable]"

            if include_end and doc_len > (max_context // 2):
                start_chars = max_context // 2
                end_chars = max_context - start_chars
                excerpt_windows = [(0, start_chars), (doc_len - end_chars, doc_len)]
            else:
                start_chars = 0
                end_chars = 0
                take = min(doc_len, max_context)
                excerpt_windows = [(0, take)]

            start_offset, end_offset = (0, 0)
            if include_selection:
                sel_positions = _text_helpers._get_writer_selection_positions(model)
                if sel_positions is not None and _text_helpers._writer_selection_overlaps_windows(model, excerpt_windows, sel_positions[1], sel_positions[2]):
                    start_offset, end_offset = _text_helpers.get_selection_range(model)
                    start_offset = max(0, min(start_offset, doc_len))
                    end_offset = max(0, min(end_offset, doc_len))
                    if start_offset > end_offset:
                        start_offset, end_offset = end_offset, start_offset
                    max_selection_span = 2000
                    if end_offset - start_offset > max_selection_span:
                        end_offset = start_offset + max_selection_span

            if include_end and doc_len > (max_context // 2):
                start_excerpt = _text_helpers._read_writer_text_slice(model, 0, start_chars)
                end_excerpt = _text_helpers._read_writer_text_slice(model, doc_len - end_chars, end_chars)
                start_excerpt = _inject_markers_into_excerpt(start_excerpt, 0, start_chars, start_offset, end_offset, "[DOCUMENT START]\n", "\n[DOCUMENT END]")
                end_excerpt = _inject_markers_into_excerpt(end_excerpt, doc_len - end_chars, doc_len, start_offset, end_offset, "[DOCUMENT END]\n", "\n[END DOCUMENT]")
                middle_note = "\n\n[... middle of document omitted ...]\n\n" if doc_len > max_context else ""
                return _with_math_ole_chat_hint(
                    model,
                    "Document length: %d characters.\n\n%s%s%s" % (doc_len, start_excerpt, middle_note, end_excerpt),
                )

            take = min(doc_len, max_context)
            excerpt = _text_helpers._read_writer_text_slice(model, 0, take)
            if doc_len > max_context:
                excerpt += "\n\n[... document truncated ...]"
            excerpt = _inject_markers_into_excerpt(excerpt, 0, take, start_offset, end_offset, "[DOCUMENT START]\n", "\n[END DOCUMENT]")
            return _with_math_ole_chat_hint(
                model,
                "Document length: %d characters.\n\n%s" % (doc_len, excerpt),
            )

        return ""
    except Exception:
        logging.getLogger(__name__).exception("get_document_context_for_chat unexpected failure, trying selection fallback")
        try:
            sel_text = _text_helpers.get_selection_text(model)
            if sel_text:
                return f"[Document context resolution failed. Active selection: {sel_text}]"
        except Exception:
            pass
        return "[Document content unavailable]"


def _inject_markers_into_excerpt(
    excerpt_text: str,
    excerpt_start: int,
    excerpt_end: int,
    sel_start: int,
    sel_end: int,
    prefix: str,
    suffix: str,
) -> str:
    # ...
    """Inject [SELECTION_START] and [SELECTION_END] at character positions relative to excerpt.
    excerpt_start/excerpt_end are the document character range this excerpt covers.
    sel_start/sel_end are the selection/cursor range in document coordinates."""
    if sel_start >= excerpt_end or sel_end <= excerpt_start:
        # Selection does not overlap this excerpt (or both markers in same position outside)
        return prefix + excerpt_text + suffix
    # Map to excerpt-relative indices
    local_start = max(0, sel_start - excerpt_start)
    local_end = min(len(excerpt_text), sel_end - excerpt_start)
    # Build result with markers inserted (order: text before start, START, text between, END, text after)
    before = excerpt_text[:local_start]
    between = excerpt_text[local_start:local_end]
    after = excerpt_text[local_end:]
    out = prefix + before + "[SELECTION_START]" + between + "[SELECTION_END]" + after + suffix
    return out


def resolve_locator(model: Any, locator: str) -> dict[str, int]:
    """Resolve a locator string to a paragraph index or other document position.

    Broader than bookmarks: ``paragraph:``, ``heading:``, and ``bookmark:``. Left
    here because ``plugin.writer.specialized.bookmarks`` only owns bookmark tools.
    """
    loc_type, sep, loc_value = locator.partition(":")
    if not sep:
        return {"para_index": 0}

    if loc_type == "paragraph":
        return {"para_index": int(loc_value)}

    if loc_type == "heading":
        parts = []
        try:
            parts = [int(p) for p in loc_value.split(".")]
        except Exception:
            logging.getLogger(__name__).exception("resolve_locator heading parse error")
            return {"para_index": 0}

        tree = _text_helpers.build_heading_tree(model)
        node: _text_helpers.HeadingTreeNode = tree
        for part in parts:
            children = node["children"]
            if 1 <= part <= len(children):
                node = children[part - 1]
            else:
                break
        return {"para_index": node["para_index"]}

    if loc_type == "bookmark":
        if hasattr(model, "getBookmarks"):
            bms = model.getBookmarks()
            if bms.hasByName(loc_value):
                anchor = bms.getByName(loc_value).getAnchor()
                para_ranges = _get_paragraph_ranges(model)
                return {"para_index": _find_paragraph_for_range(anchor, para_ranges, model.getText())}

    return {"para_index": 0}


def is_cacheable_doc_key(key: str) -> bool:
    """False for the empty-identity sentinel — do not store or attach listeners."""
    return bool(key) and key != UNKNOWN_DOC_KEY


def _compute_doc_key(doc: Any) -> str:
    """uid:<RuntimeUID> then url:<normalized>; never id(doc)."""
    if doc is None:
        return UNKNOWN_DOC_KEY
    uid = get_runtime_uid(doc)
    if uid:
        return "uid:%s" % uid
    try:
        raw = doc.getURL()
    except Exception:
        raw = ""
    url = normalize_doc_url(raw) if isinstance(raw, str) else ""
    if url:
        return "url:%s" % url
    return UNKNOWN_DOC_KEY


def _emit_cache_invalidated(*, doc: Any | None = None, key: str | None = None) -> None:
    from plugin.framework.event_bus import get_event_bus

    payload: dict[str, Any] = {}
    if key is not None:
        payload["key"] = key
    if doc is not None:
        payload["doc"] = doc
    get_event_bus().emit("document:cache_invalidated", **payload)


class _CacheListenerPair:
    key: str
    modify: Any
    unload: Any
    _model: Any

    def __init__(self, key: str, modify: Any, unload: Any, model: Any) -> None:
        self.key = key
        self.modify = modify
        self.unload = unload
        try:
            self._model_ref: Any = weakref.ref(model)
        except TypeError:
            self._model_ref = None
            self._model = model

    def model(self) -> Any:
        if self._model_ref is not None:
            return self._model_ref()
        return getattr(self, "_model", None)


class _CacheModifyListener:
    """Drops Writer tree / proximity / FTS caches when the model changes.

    PyUNO wrappers are not stable identity (see doc_key). Store the key at
    attach so disposing() can emit without calling getURL() / RuntimeUID on a
    half-dead model.
    """

    _doc_key_val: str

    def __init__(self, key: str) -> None:
        self._doc_key_val = key

    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            if _IGNORE_DEPTH > 0:
                return
            model = getattr(aEvent, "Source", None)
            _emit_cache_invalidated(doc=model)
        except Exception:
            log.debug("cache invalidate modify handler failed", exc_info=True)

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        _teardown_cache_listener(self._doc_key_val, owner_modify=self)


class _CacheUnloadListener:
    _doc_key_val: str

    def __init__(self, key: str) -> None:
        self._doc_key_val = key

    def documentEventOccured(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO spelling
        try:
            name = getattr(Event, "EventName", "") or ""
            if name == "OnUnload":
                _teardown_cache_listener(self._doc_key_val, owner_unload=self)
        except Exception:
            log.debug("cache invalidate unload handler failed", exc_info=True)

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        _teardown_cache_listener(self._doc_key_val, owner_unload=self)


def _teardown_cache_listener(key: str, owner_modify: Any | None = None, owner_unload: Any | None = None) -> None:
    pair = _CACHE_LISTENERS.get(key)
    if pair is None:
        return
    # Recycled RuntimeUID: a late disposing() from the old listener must not
    # evict the newer pair (review_toolbar._ReviewModifyListener).
    if owner_modify is not None and pair.modify is not owner_modify:
        return
    if owner_unload is not None and pair.unload is not owner_unload:
        return
    popped = _CACHE_LISTENERS.pop(key, None)
    if popped is None:
        return
    _emit_cache_invalidated(key=key)
    model = popped.model()
    if model is None:
        return
    try:
        if hasattr(model, "removeModifyListener"):
            model.removeModifyListener(popped.modify)
    except Exception:
        log.debug("cache modify listener removal failed", exc_info=True)
    try:
        if hasattr(model, "removeDocumentEventListener"):
            model.removeDocumentEventListener(popped.unload)
    except Exception:
        log.debug("cache unload listener removal failed", exc_info=True)


def _uno_listener(logic_cls: Any, iface: Any, key: str) -> Any:
    """Attach-time UNO subclass so module import stays soffice-free."""
    if not _HAVE_UNO_LISTENERS:
        return logic_cls(key)
    base = cast("Any", _unohelper).Base
    cls = type("_UnoCacheListener", (base, iface, logic_cls), {})
    return cls(key)


def _ensure_cache_listener(doc: Any, key: str) -> None:
    if key in _CACHE_LISTENERS:
        return
    can_modify = hasattr(doc, "addModifyListener")
    can_unload = hasattr(doc, "addDocumentEventListener")
    if not can_modify and not can_unload:
        return
    modify = _uno_listener(_CacheModifyListener, _XModifyListener, key)
    unload = _uno_listener(_CacheUnloadListener, _XDocumentEventListener, key)
    try:
        if can_modify:
            doc.addModifyListener(modify)
        if can_unload:
            doc.addDocumentEventListener(unload)
    except Exception:
        log.debug("cache listener registration failed", exc_info=True)
        try:
            if can_modify:
                doc.removeModifyListener(modify)
        except Exception:
            pass
        try:
            if can_unload:
                doc.removeDocumentEventListener(unload)
        except Exception:
            pass
        return
    _CACHE_LISTENERS[key] = _CacheListenerPair(key, modify, unload, doc)


class DocumentService(ServiceBase):
    name: str | None = "document"

    def initialize(self, ctx: Any) -> None:
        pass

    def get_active_document(self) -> Any:
        return get_active_document()

    def resolve_document_by_url(self, url: str) -> Any:
        """Resolve (doc, doc_type) by document URL; (None, None) if not found. Main-thread only."""
        return _resolve_document_by_url(get_ctx(), url)

    def detect_doc_type(self, doc: Any) -> str:
        doc_type = _doc_type.get_document_type(doc)
        if doc_type == _doc_type.DocumentType.CALC:
            return "calc"
        if doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            return "draw"
        return "writer"

    def is_writer(self, doc: Any) -> bool:
        return _doc_type.is_writer(doc)

    def is_calc(self, doc: Any) -> bool:
        return _doc_type.is_calc(doc)

    def is_draw(self, doc: Any) -> bool:
        return _doc_type.is_draw(doc)

    def get_full_text(self, doc: Any, max_chars: int = 8000) -> str:
        return get_full_document_text(doc, max_chars)

    def get_document_length(self, doc: Any) -> int:
        return _text_helpers.get_document_length(doc)

    def get_document_context_for_chat(
        self,
        doc: Any,
        max_context: int = CHAT_DOCUMENT_CONTEXT_MAX_CHARS,
        include_end: bool = True,
        include_selection: bool = True,
    ) -> str:
        return get_document_context_for_chat(doc, max_context, include_end, include_selection, get_ctx())

    def get_page_for_paragraph(self, model: Any, para_index: int) -> Any:
        """Return page number for a paragraph by index.

        Uses lockControllers + cursor save/restore to prevent visible viewport jumping.
        """
        try:
            check_disposed(model, "Document Model")
            text = safe_call(model.getText, "Get document text")
            controller = safe_call(model.getCurrentController, "Get current controller")
            vc = safe_call(controller.getViewCursor, "Get view cursor")
            saved = safe_call(text.createTextCursorByRange, "Create text cursor by range", safe_call(vc.getStart, "Get view cursor start"))
            safe_call(model.lockControllers, "Lock controllers")
            try:
                cursor = safe_call(text.createTextCursor, "Create text cursor")
                safe_call(cursor.gotoStart, "Cursor gotoStart", False)
                for _unused in range(para_index):
                    if not safe_call(cursor.gotoNextParagraph, "Cursor gotoNextParagraph", False):
                        break
                safe_call(vc.gotoRange, "View cursor gotoRange", cursor, False)
                page = safe_call(vc.getPage, "Get page")
            finally:
                safe_call(vc.gotoRange, "Restore view cursor", saved, False)
                safe_call(model.unlockControllers, "Unlock controllers")
            return page
        except UnoObjectError:
            logging.getLogger(__name__).exception("get_page_for_paragraph error")
            return 1

    def get_page_count(self, model: Any) -> Any:
        """Return page count of a Writer document."""
        try:
            check_disposed(model, "Document Model")
            text = safe_call(model.getText, "Get document text")
            controller = safe_call(model.getCurrentController, "Get current controller")
            vc = safe_call(controller.getViewCursor, "Get view cursor")
            saved = safe_call(text.createTextCursorByRange, "Create text cursor by range", safe_call(vc.getStart, "Get view cursor start"))
            safe_call(model.lockControllers, "Lock controllers")
            try:
                safe_call(vc.jumpToLastPage, "Jump to last page")
                count = safe_call(vc.getPage, "Get page")
            finally:
                safe_call(vc.gotoRange, "Restore view cursor", saved, False)
                safe_call(model.unlockControllers, "Unlock controllers")
            return count
        except UnoObjectError:
            logging.getLogger(__name__).exception("get_page_count error")
            return 0

    def doc_key(self, doc: Any) -> str:
        """Stable cache key for one open document.

        PyUNO hands out a new Python wrapper on almost every lookup of the same
        UNO document. Keying caches with ``id(doc)`` therefore almost never
        hits; after GC, CPython can reuse that id and a lookup can return
        another document's tree (or a disposed one). Nelson mcp ``039ade49`` /
        ``e9d3aa36`` (#2642). RuntimeUID (then URL) matches MCP
        ``_resolve_mcp_doc_key``. Empty both → ``UNKNOWN_DOC_KEY`` (do not
        cache). First call lazily attaches one modify + OnUnload listener.
        """
        key = _compute_doc_key(doc)
        if is_cacheable_doc_key(key):
            _ensure_cache_listener(doc, key)
        return key

    @contextmanager
    def ignore_cache_invalidation(self) -> Generator[None, None, None]:
        """Suppress modify-driven cache drops (reentrant).

        Item 4 wraps ``_mcp_`` bookmark insert/strip so those mutations do not
        thrash the heading tree. Nested ``with`` is required (strip then
        restore during save). Queued modifies are dropped, not flushed.
        """
        global _IGNORE_DEPTH
        _IGNORE_DEPTH += 1
        try:
            yield
        finally:
            _IGNORE_DEPTH -= 1

    def get_paragraph_ranges(self, doc: Any) -> list[Any]:
        """Return list of top-level paragraph elements."""
        return _get_paragraph_ranges(doc)

    def find_paragraph_for_range(self, anchor: Any, para_ranges: list[Any], text_obj: Any = None) -> int:
        """Return the 0-based paragraph index that contains anchor."""
        return _find_paragraph_for_range(anchor, para_ranges, text_obj)

    def resolve_locator(self, doc: Any, locator: str) -> dict[str, int]:
        """Resolve a locator string to a paragraph index or other document position."""
        return resolve_locator(doc, locator)

    def yield_to_gui(self) -> None:
        """Yield to the UI event loop (no-op here)."""
        pass

    def annotate_pages(self, children: Any, doc: Any) -> None:
        """Annotate tree children with page numbers (no-op here)."""
        pass

    def find_paragraph_element(self, doc: Any, para_index: int) -> tuple[Any, None]:
        """Return (paragraph_element, None) for the given index, or (None, None) if out of range."""
        ranges = _get_paragraph_ranges(doc)
        if 0 <= para_index < len(ranges):
            return (ranges[para_index], None)
        return (None, None)
