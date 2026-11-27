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
from itertools import islice
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

from plugin.framework import doc_type as _doc_type
from plugin.doc import text_helpers as _text_helpers
from plugin.doc.paragraph_search import (
    paragraph_index_for_anchor as _paragraph_index_for_anchor,
    get_paragraph_ranges as _get_paragraph_ranges,
)
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.errors import (
    ToolExecutionError,
    DocumentDisposedError, UnoObjectError,
    best_effort, uno_call, uno_read,
)
from plugin.framework.event_bus import get_event_bus
from plugin.framework.scratch_writer import is_scratch_document, run_after_scratch_loads
from plugin.framework.service import ServiceBase
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import (
    enum_has_more,
    get_active_document,
    get_ctx,
    get_desktop,
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
_OPEN_DOC_TYPES_LISTENER: Any = None
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
except ImportError:
    pass


def _writer_has_math_ole(model: Any) -> bool:
    """True when the Writer doc has at least one LibreOffice Math embedded object."""
    try:
        # Lazy: importing plugin.writer loads tree, bookmarks and review_view_attachment,
        # which import from this module (import cycle).
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
        log.debug("Failed checking Math OLE objects in Writer document", exc_info=True)
    return False


# Cap the Writer object list because the snapshot is rebuilt on every send; a
# document with hundreds of images should not add hundreds of lines.
# page_get_objects reaches any objects past this cap.
_WRITER_SNAPSHOT_MAX_OBJECTS = 20


def _writer_anchor_note(shape: Any) -> str:
    """Short anchor description for a Writer draw-page object.

    Uses a paragraph excerpt rather than a paragraph index because computing
    a paragraph index requires walking the entire document body on every send.
    """
    kind = uno_read(lambda: str(shape.getPropertyValue("AnchorType").value), "")
    if not kind:
        return ""
    if kind == "AT_PAGE":
        page_no = uno_read(lambda: int(shape.getPropertyValue("AnchorPageNo")), 0)
        # 0 = unset/not laid out; LibreOffice pages are 1-based.
        return " anchor: AT_PAGE page %d" % (page_no - 1) if page_no > 0 else " anchor: AT_PAGE"

    def read_excerpt() -> str:
        anchor = shape.getAnchor()
        cur = anchor.getText().createTextCursorByRange(anchor.getStart())
        cur.gotoStartOfParagraph(False)
        cur.gotoEndOfParagraph(True)
        return _text_helpers.normalize_linebreaks(cur.getString()).strip()

    excerpt = uno_read(read_excerpt, "")
    if excerpt:
        return ' anchor: %s in "%s"' % (kind, excerpt[:60])
    return " anchor: %s" % kind


def _writer_objects_section(model: Any) -> str:
    """List Writer draw-page objects (index, kind, name, anchor) for the chat snapshot."""
    def build() -> str:
        from plugin.draw.tree import iter_shape_addresses, shape_summary_line

        rows = list(islice(iter_shape_addresses(model.getDrawPage()), _WRITER_SNAPSHOT_MAX_OBJECTS + 1))
        if not rows:
            return ""
        lines = [
            shape_summary_line(a, s) + _writer_anchor_note(s)
            for a, s, _p in rows[:_WRITER_SNAPSHOT_MAX_OBJECTS]
        ]
        if len(rows) > _WRITER_SNAPSHOT_MAX_OBJECTS:
            lines.append("[... more objects not listed; page_get_objects lists the objects on one page]")
        return "\n\nObjects (index, kind, name, anchor):\n" + "\n".join(lines)

    # One uno_read around the whole build: a non-disposal failure (e.g.
    # getDrawPage raising) drops only this section instead of falling into the
    # outer fallback and losing the text, while disposal still escapes as
    # DocumentDisposedError.
    return uno_read(build, "")


def _writer_snapshot(model: Any, body: str) -> str:
    """Append the draw-page object list and Math OLE hint after a Writer excerpt."""
    return _with_math_ole_chat_hint(model, body + _writer_objects_section(model))


def _with_math_ole_chat_hint(model: Any, body: str) -> str:
    """Plain-text excerpts skip Math OLE; point the model at get_content."""
    if not _writer_has_math_ole(model):
        return body
    return (
        body
        + "\n\nMath formulas are LibreOffice Math objects (OLE), not characters in the "
        "excerpt above, so Document length may omit them. Call get_content to "
        "read them as TeX."
    )


@main_thread_only
def get_document_context_for_chat(
    model: Any,
    max_context: int = CHAT_DOCUMENT_CONTEXT_MAX_CHARS,
    include_end: bool = True,
    include_selection: bool = True,
    ctx: Any | None = None,
    *,
    selection: Any | None = None,
) -> str:
    """Build a single context string for chat. Handles Writer, Calc and Draw.
    ctx: component context (required for Calc and Draw documents).
    selection: optional Writer text range to mark instead of reading the live view."""
    try:
        doc_type = _doc_type.get_document_type(model)

        # Cross-app imports stay inside their branch so Calc does not pull Draw
        # and Draw does not pull SheetAnalyzer. The Writer branch lazily loads
        # plugin.draw.tree (and plugin.calc.charts when an embedded object needs
        # the chart CLSID check); both are already loaded by tool registration.
        if doc_type == _doc_type.DocumentType.CALC:
            from plugin.calc.analyzer import get_calc_context_for_chat

            return get_calc_context_for_chat(model, max_context, ctx)
        if doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            from plugin.draw.bridge import get_draw_context_for_chat

            return get_draw_context_for_chat(model, max_context, ctx)

        # Writer: plain-text start/end slices (hides tracked deletions). Math OLE is not
        # in getString(); we only hint to call get_content.
        if doc_type == _doc_type.DocumentType.WRITER:
            try:
                positions: tuple[Any, ...] = ()
                if include_selection:
                    sel_positions = _text_helpers.get_writer_selection_positions(model, selection=selection)
                    if sel_positions is not None:
                        positions = sel_positions[1:]
                text, offsets, struck = _text_helpers.read_visible_body(model, positions)
            except UnoObjectError:
                logging.getLogger(__name__).exception("get_document_context_for_chat Writer failed, trying fallback to selection-only")
                sel_text = _text_helpers.get_selection_text(model)
                if sel_text:
                    return f"[Document text reading failed. Active selection: {sel_text}]"
                return "[Document content unavailable]"

            total = len(text)
            # None: no selection in the body, so neither end is marked. (0, 0) is a caret at the start.
            sel_start: int | None = None
            sel_end: int | None = None
            if len(offsets) == 2 and offsets[0] is not None and offsets[1] is not None:
                low, high = sorted((offsets[0], offsets[1]))
                max_selection_span = 2000
                sel_start, sel_end = low, min(high, low + max_selection_span)

            # Split into non-overlapping head and tail windows only when the visible body exceeds max_context.
            if include_end and total > max_context:
                start_chars = max_context // 2
                end_chars = max_context - start_chars
                tail_start = total - end_chars
                # A selection across the omitted middle opens in the head and closes in the tail.
                spans_gap = (
                    sel_start is not None and sel_end is not None and sel_start < start_chars and sel_end > tail_start
                )
                start_excerpt = _inject_markers(
                    text[:start_chars], 0, sel_start, None if spans_gap else sel_end, "[DOCUMENT START]\n", "\n[DOCUMENT END]", struck
                )
                # snapshot_text adds the one [END DOCUMENT] closer, so this excerpt has no trailing closer.
                end_excerpt = _inject_markers(
                    text[tail_start:], tail_start, None if spans_gap else sel_start, sel_end, "[DOCUMENT CONTINUE]\n", "", struck
                )
                middle_note = "\n\n[... middle of document omitted ...]\n\n"
                return _writer_snapshot(
                    model,
                    "Document length: %d characters.\n\n%s%s%s" % (total, start_excerpt, middle_note, end_excerpt),
                )

            note = "\n\n[... document truncated ...]" if total > max_context else ""
            # snapshot_text adds the one [END DOCUMENT] closer, so this excerpt has no trailing closer.
            excerpt = _inject_markers(text[:max_context], 0, sel_start, sel_end, "[DOCUMENT START]\n", note, struck)
            return _writer_snapshot(
                model,
                "Document length: %d characters.\n\n%s" % (total, excerpt),
            )

        return ""
    except DocumentDisposedError:
        raise
    except Exception:
        logging.getLogger(__name__).exception("get_document_context_for_chat unexpected failure, trying selection fallback")
        try:
            sel_text = _text_helpers.get_selection_text(model)
            if sel_text:
                return f"[Document context resolution failed. Active selection: {sel_text}]"
        except DocumentDisposedError:
            pass
        return "[Document content unavailable]"


def _inject_markers(
    excerpt_text: str,
    excerpt_start: int,
    sel_start: int | None,
    sel_end: int | None,
    prefix: str,
    suffix: str,
    struck: Sequence[int] = (),
) -> str:
    """Insert selection and tracked-deletion markers at visible offsets in *excerpt_text*.

    A None end is not marked here: it is in another window, or there is no selection.
    """
    visible_len = len(excerpt_text)
    markers: list[tuple[int, str]] = []
    s = 0 if sel_start is None else sel_start - excerpt_start
    e = visible_len if sel_end is None else sel_end - excerpt_start
    # A caret counts at either edge of the window; a range has to overlap it.
    if (0 <= s <= visible_len) if s == e else (s < visible_len and e > 0):
        if sel_start is not None:
            markers.append((max(0, s), "[SELECTION_START]"))
        if sel_end is not None:
            markers.append((min(e, visible_len), "[SELECTION_END]"))

    in_window = [p - excerpt_start for p in struck if excerpt_start <= p <= excerpt_start + visible_len]
    if in_window:
        # Collapse consecutive struck slots so a long struck passage does not flood the context.
        runs: list[list[int]] = []
        for p in in_window:
            if runs and p == runs[-1][0] + runs[-1][1]:
                runs[-1][1] += 1
            else:
                runs.append([p, 1])
        for run_start, run_len in runs:
            label = "[tracked deletion]" if run_len == 1 else "[tracked deletion, %d paragraphs]" % run_len
            markers.append((run_start, label))

    if not markers:
        return prefix + excerpt_text + suffix
    markers.sort(key=lambda item: item[0])
    out = excerpt_text
    for pos, marker in reversed(markers):
        out = out[:pos] + marker + out[pos:]
    return prefix + out + suffix


# Resolver-only TreeService used when WriterAgent has not registered writer_tree
# (unit tests, and any call before WriterModule.initialize). One instance so
# repeated misses do not subscribe a new cache listener each time.
_FALLBACK_WRITER_TREE: Any = None


def _unresolved_locator(locator: str) -> ToolExecutionError:
    """Locator string that is not ``type:value``."""
    return ToolExecutionError(
        "Cannot resolve locator '%s'. Use type:value such as paragraph:N, "
        "heading:1.2, chapter_number:3.1, bookmark:NAME, heading_text:Title, "
        "section:NAME, or page:N (0-based: the user's page 2 is page:1)." % locator
    )


def _writer_tree_service() -> Any:
    """Return the process TreeService, or one resolver-only fallback.

    The registered service shares the heading-tree cache and bookmark map
    with navigation. The fallback exists so a locator still resolves when
    this function runs outside bootstrap (pytest, or before the writer
    module loads). Its document service is a plain DocumentService.
    """
    # Lazy like the codebase's other in-function plugin.app imports: plugin.app loads
    # plugin.bootstrap and the config schema.
    import plugin.app as app

    global _FALLBACK_WRITER_TREE

    services = app.services_or_none()
    if services is not None:
        tree = services.get("writer_tree")
        if tree is not None:
            return tree

    if _FALLBACK_WRITER_TREE is not None:
        return _FALLBACK_WRITER_TREE

    # Lazy: plugin.writer.tree imports is_cacheable_doc_key from this module (import cycle).
    from plugin.writer.tree import TreeService

    class _Events:
        def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _Bookmarks:
        def get_mcp_bookmark_map(self, _doc: Any) -> dict[Any, Any]:
            return {}

    _FALLBACK_WRITER_TREE = TreeService(
        SimpleNamespace(
            document=DocumentService(),
            writer_bookmarks=_Bookmarks(),
            events=_Events(),
        )
    )
    return _FALLBACK_WRITER_TREE


def resolve_locator(model: Any, locator: str) -> dict[str, Any]:
    """Resolve a locator string to a paragraph index.

    ``paragraph:`` is resolved here. Every other locator (``heading:``,
    ``chapter_number:``, ``bookmark:``, ``heading_text:``, ``section:``,
    ``page:``, ...) goes to ``TreeService.resolve_writer_locator``. A missing
    bookmark, or a bookmark whose anchor does not land on a paragraph, raises
    ``ToolExecutionError``.
    """
    loc_type, sep, loc_value = locator.partition(":")
    if not sep or not loc_type:
        raise _unresolved_locator(locator)

    if loc_type == "paragraph":
        try:
            return {"para_index": int(loc_value)}
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("Cannot resolve paragraph:%s" % loc_value) from exc

    # bookmark: is not resolved here. TreeService.resolve_writer_locator
    # rejects an anchor it cannot place, so a stale bookmark name fails
    # instead of navigating to paragraph 0.
    tree = _writer_tree_service()
    try:
        resolved = tree.resolve_writer_locator(model, loc_type, loc_value)
    # page:abc fails int() inside the resolver; the tool registry re-raises
    # ValueError as a programmer error, so this arm converts it.
    except ValueError as exc:
        raise ToolExecutionError("Cannot resolve %s:%s — %s" % (loc_type, loc_value, exc)) from exc
    if not isinstance(resolved, dict) or not isinstance(resolved.get("para_index"), int):
        raise ToolExecutionError("Cannot resolve %s:%s" % (loc_type, loc_value))
    return resolved


def is_cacheable_doc_key(key: str) -> bool:
    """False for the empty-identity sentinel — do not store or attach listeners."""
    return bool(key) and key != UNKNOWN_DOC_KEY


def compute_doc_key(doc: Any) -> str:
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
    _model_ref: Any
    _model: Any

    def __init__(self, key: str, modify: Any, unload: Any, model: Any) -> None:
        self.key = key
        self.modify = modify
        self.unload = unload
        self._model = None
        try:
            self._model_ref = weakref.ref(model)
        except TypeError:
            self._model_ref = None
            self._model = model

    def model(self) -> Any:
        if self._model_ref is not None:
            return self._model_ref()
        return self._model


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
            model = aEvent.Source
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
            name = Event.EventName
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
    # evict the newer pair (review_view_attachment.ReviewModifyListener).
    if owner_modify is not None and pair.modify is not owner_modify:
        return
    if owner_unload is not None and pair.unload is not owner_unload:
        return
    del _CACHE_LISTENERS[key]
    _emit_cache_invalidated(key=key)
    model = pair.model()
    if model is None:
        return
    try:
        if hasattr(model, "removeModifyListener"):
            model.removeModifyListener(pair.modify)
    except Exception:
        log.debug("cache modify listener removal failed", exc_info=True)
    try:
        if hasattr(model, "removeDocumentEventListener"):
            model.removeDocumentEventListener(pair.unload)
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
        except Exception as exc:
            log.debug("cache modify listener rollback failed (%s)", type(exc).__name__)
        try:
            if can_unload:
                doc.removeDocumentEventListener(unload)
        except Exception as exc:
            log.debug("cache unload listener rollback failed (%s)", type(exc).__name__)
        return
    _CACHE_LISTENERS[key] = _CacheListenerPair(key, modify, unload, doc)


class DocumentService(ServiceBase):
    name: str | None = "document"

    def get_active_document(self) -> Any:
        return get_active_document()

    def resolve_document_by_url(self, url: str) -> Any:
        """Resolve (doc, doc_type) by document URL; (None, None) if not found. Main-thread only."""
        return _resolve_document_by_url(get_ctx(), url)

    def detect_doc_type(self, doc: Any) -> str:
        family = _doc_type.doc_type_family_label(_doc_type.get_document_type(doc))
        return "writer" if family == "unknown" else family

    def open_documents(self) -> list[Any]:
        """Every open real document, in desktop order. Main-thread only.

        Start Center and WriterAgent's scratch documents (including files opened
        hidden for research) are excluded (see ``plugin.framework.scratch_writer``);
        user-hidden documents are included.
        """
        docs = []
        try:
            desktop = get_desktop()
            comps = desktop.getComponents() if desktop is not None else None
            enum = comps.createEnumeration() if comps else None
            while enum is not None and enum_has_more(enum):
                doc = enum.nextElement()
                try:
                    if doc.supportsService("com.sun.star.document.OfficeDocument") and not is_scratch_document(doc):
                        docs.append(doc)
                except Exception as exc:
                    log.debug("open_documents: skip component (%s)", type(exc).__name__)
        except Exception:
            log.debug("open_documents failed", exc_info=True)
        return docs

    def open_documents_by_type(self) -> dict[str, Any]:
        """``{doc_type: first open document of that type}``. Main-thread only.

        ONE document per type -- right for the tool catalog, which is keyed by type,
        and wrong for anything that needs to know how many documents are open (use
        open_documents for that).

        Catalog tools by open documents, not the active window—Calc focus must not hide Writer tools.
        Every tool accepts ``document_url``, so the open set is the honest catalog basis.
        """
        found: dict[str, Any] = {}
        for doc in self.open_documents():
            try:
                found.setdefault(self.detect_doc_type(doc), doc)
            except Exception as exc:
                log.debug("open_documents_by_type: skip document (%s)", type(exc).__name__)
        return found

    def open_doc_types(self) -> frozenset[str]:
        """Set of open document family labels (``writer``, ``calc``, ``draw``). Main-thread only."""
        return frozenset(self.open_documents_by_type())

    def watch_open_doc_types(self, on_change: Callable[[frozenset[str]], object]) -> None:
        """Call ``on_change(open_doc_types())`` now and after every document open or close.

        Listens for ``OnNew``, ``OnLoad``, ``OnLoadFinished``, ``OnViewCreated``,
        and ``OnUnload``. At ``OnUnload`` LibreOffice has already detached the
        closing document's controller so it is absent from ``open_documents()``
        and no exclusion is needed. Installs one process-wide
        GlobalEventBroadcaster listener; later calls are no-ops. Creates a UNO
        service, so call on the main thread.
        """
        global _OPEN_DOC_TYPES_LISTENER
        if _OPEN_DOC_TYPES_LISTENER is not None:
            return
        # in-function import: uno_listeners imports unohelper at load, which breaks
        # the isolated document_helpers import (see module header).
        from plugin.framework.uno_listeners import install_view_doc_listener

        # One callable so several view events in one load queue one report; it reads current state.
        def _report_types() -> object:
            return on_change(self.open_doc_types())

        def _report(_source: Any) -> None:
            run_after_scratch_loads(_report_types)

        _OPEN_DOC_TYPES_LISTENER = install_view_doc_listener(
            get_ctx(),
            _report,
            "open_doc_types",
            on_unload=_report,
        )
        if _OPEN_DOC_TYPES_LISTENER is not None:
            on_change(self.open_doc_types())

    def get_document_length(self, doc: Any) -> int:
        return _text_helpers.get_document_length(doc)

    def get_page_for_paragraph(self, model: Any, para_index: int) -> int:
        """Return LibreOffice's 1-based physical page of the paragraph (callers convert for the model).

        Uses with_view_cursor_left_body_locked to leave nested XText and prevent visible viewport jumping.
        """
        # See text_helpers.with_view_cursor_left_body_locked.
        if model is None:
            raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
        element = self.find_paragraph_element(model, para_index)
        if element is None:
            raise ToolExecutionError("Paragraph index %d not found in document" % para_index)

        anchor = element.getAnchor()

        controller = uno_call(model.getCurrentController, "Get current controller")
        # Print Preview controllers lack getViewCursor; read it inside uno_call so missing attributes become UnoObjectError.
        vc = uno_call(lambda: controller.getViewCursor(), "Get view cursor")

        def _resolve_page() -> int:
            uno_call(lambda: vc.gotoRange(anchor, False), "View cursor gotoRange")
            page = uno_call(vc.getPage, "Get page")
            if page == 0:
                # Layout stall for tables/frames under lockControllers.
                was_locked = model.hasControllersLocked()
                if was_locked:
                    uno_call(model.unlockControllers, "Unlock controllers")
                try:
                    uno_call(lambda: vc.gotoRange(anchor, False), "View cursor gotoRange")
                    page = uno_call(vc.getPage, "Get page")
                finally:
                    if was_locked:
                        uno_call(model.lockControllers, "Lock controllers")
            if page == 0:
                raise ToolExecutionError(
                    "Cannot resolve page for paragraph %d: getPage returned 0" % para_index
                )
            return page

        return _text_helpers.with_view_cursor_left_body_locked(model, vc, _resolve_page)

    def get_page_count(self, model: Any) -> int:
        """Return page count of a Writer document."""
        # See text_helpers.with_view_cursor_left_body_locked.
        try:
            if model is None:
                raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
            controller = uno_call(model.getCurrentController, "Get current controller")
            get_vc: Any = getattr(controller, "getViewCursor", None)
            if callable(get_vc):
                vc: Any = uno_call(get_vc, "Get view cursor")

                def _get_count() -> int:
                    uno_call(vc.jumpToLastPage, "Jump to last page")
                    return uno_call(vc.getPage, "Get page")

                return _text_helpers.with_view_cursor_left_body_locked(model, vc, _get_count)

            # Preview views (SwPagePreview, ViewId=7) have no getViewCursor (SfxBaseController).
            # Reading WordCount runs GetUpdatedDocStat (unotxdoc.cxx WID_DOC_WORD_COUNT), which
            # recalculates DocumentStatistics.PageCount from the layout (DocumentStatisticsManager::IncrementalDocStatCalculate,
            # DocumentStatisticsManager.cxx:145-160) only when modified or uninitialized (GetUpdatedDocStat, lines 70-77).
            # ODF import initializes stats from meta.xml (SwXMLImport::SetStatistics, xmlmeta.cxx:100-130), so mark
            # modified briefly to force a layout count.
            was_modified = uno_call(lambda: model.isModified(), "Check modified flag")
            try:
                uno_call(lambda: model.setModified(True), "Mark modified for statistics")
                uno_call(lambda: model.WordCount, "Update document statistics")
            finally:
                best_effort(lambda: model.setModified(was_modified), "restore modified flag after page count")
            doc_props = uno_call(lambda: model.getDocumentProperties(), "Get document properties")
            stats = uno_call(lambda: doc_props.DocumentStatistics, "Get document statistics")
            for prop in stats:
                if prop.Name == "PageCount":
                    return int(prop.Value)
            return 0
        except (UnoObjectError, ToolExecutionError):
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
        key = compute_doc_key(doc)
        if is_cacheable_doc_key(key):
            _ensure_cache_listener(doc, key)
        return key

    @contextmanager
    def ignore_cache_invalidation(self) -> Generator[None, None, None]:
        """Suppress modify-driven cache drops (reentrant).

        The ``_mcp_`` bookmark insert and strip run inside this so those mutations do not
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

    def find_paragraph_for_range(
        self,
        anchor: Any,
        para_ranges: list[Any],
        text_obj: Any = None,
        entries: list[tuple[int, Any]] | None = None,
    ) -> int | None:
        """Return the 0-based paragraph index that contains anchor, or None when it cannot be placed."""
        return _paragraph_index_for_anchor(anchor, para_ranges, text_obj, entries=entries)

    def resolve_locator(self, doc: Any, locator: str) -> dict[str, Any]:
        """Resolve a locator string to a paragraph index.

        Unresolvable locators raise ``ToolExecutionError``. See module
        ``resolve_locator``.
        """
        return resolve_locator(doc, locator)

    def find_paragraph_element(self, doc: Any, para_index: int) -> Any | None:
        """Return the paragraph element for the given index, or None if out of range."""
        ranges = _get_paragraph_ranges(doc)
        if 0 <= para_index < len(ranges):
            return ranges[para_index]
        return None
