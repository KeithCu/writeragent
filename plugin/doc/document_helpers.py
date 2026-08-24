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
``plugin.calc.analyzer``. Paragraph range helpers live in
``plugin.doc.paragraph_search``. Do not re-export those names here — a
re-export of ``get_calc_context_for_chat`` would pull ``SheetAnalyzer`` at
import time and break LibrePy.
"""
import logging

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
from plugin.framework.uno_context import get_active_document, get_ctx, resolve_document_by_url as _resolve_document_by_url


@main_thread_only
def get_full_document_text(model, max_chars=CHAT_DOCUMENT_CONTEXT_MAX_CHARS):
    """Get full document text for Writer or summary for Calc, truncated to max_chars.

    Stays here (not ``text_helpers``) because the Calc branch needs
    ``SheetAnalyzer`` / ``CalcBridge``. Those are imported lazily so this
    module's import does not load Calc.
    """
    try:
        check_disposed(model, "Document Model")
        doc_type = _doc_type.get_document_type(model)

        if doc_type == _doc_type.DocumentType.CALC:
            from plugin.calc.analyzer import SheetAnalyzer
            from plugin.calc.bridge import CalcBridge

            # Calc document
            bridge = CalcBridge(model)
            analyzer = SheetAnalyzer(bridge)
            summary = analyzer.get_sheet_summary()
            text = f"Sheet: {summary['sheet_name']}\nUsed Range: {summary['used_range']}\n"
            text += f"Columns: {', '.join(filter(None, summary['headers']))}\n"
            # Maybe add some preview rows?
            return text

        if doc_type == _doc_type.DocumentType.WRITER:
            doc_len = _text_helpers._writer_char_count(model)
            take = min(doc_len, max_chars)
            excerpt = _text_helpers._read_writer_text_slice(model, 0, take)
            if doc_len > max_chars:
                excerpt += "\n\n[... document truncated ...]"
            return excerpt

        if doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            return get_draw_context_for_chat(model, max_chars)

        return ""
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_full_document_text failed")
        return ""


def _writer_has_math_ole(model) -> bool:
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


def _with_math_ole_chat_hint(model, body: str) -> str:
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
def get_document_context_for_chat(model, max_context=CHAT_DOCUMENT_CONTEXT_MAX_CHARS, include_end=True, include_selection=True, ctx=None):
    """Build a single context string for chat. Handles Writer, Calc and Draw.
    ctx: component context (required for Calc and Draw documents)."""
    try:
        doc_type = _doc_type.get_document_type(model)

        if doc_type == _doc_type.DocumentType.CALC:
            from plugin.calc.analyzer import get_calc_context_for_chat

            return get_calc_context_for_chat(model, max_context, ctx)

        if doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
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


@main_thread_only
def get_draw_context_for_chat(model, max_context=8000, ctx=None):
    """Get context summary for a Draw/Impress document. ctx: component context (unused, kept for signature compat)."""
    try:
        check_disposed(model, "Document Model")
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(model)
        pages = bridge.get_pages()
        active_page = bridge.get_active_page()

        is_impress = safe_call(model.supportsService, "Check supportsService", "com.sun.star.presentation.PresentationDocument")
        doc_type = "Impress Presentation" if is_impress else "Draw Document"

        ctx_str = "%s: %s\n" % (doc_type, safe_call(model.getURL, "Get document URL") or "Untitled")
        ctx_str += "Total %s: %d\n" % ("Slides" if is_impress else "Pages", safe_call(pages.getCount, "Get page count"))

        # Get index of active page
        active_page_idx = -1
        for i in range(safe_call(pages.getCount, "Get page count")):
            if safe_call(pages.getByIndex, "Get page by index", i) == active_page:
                active_page_idx = i
                break

        ctx_str += "Active %s Index: %d\n" % ("Slide" if is_impress else "Page", active_page_idx)

        # Summarize shapes on active page
        if active_page:
            shapes = bridge.get_shapes(active_page)
            ctx_str += "\nShapes on %s %d:\n" % ("Slide" if is_impress else "Page", active_page_idx)
            for i, s in enumerate(shapes):
                type_name = safe_call(s.getShapeType, "Get shape type").split(".")[-1]
                pos = safe_call(s.getPosition, "Get position")
                size = safe_call(s.getSize, "Get size")
                ctx_str += "- [%d] %s: pos(%d, %d) size(%dx%d)" % (i, type_name, pos.X, pos.Y, size.Width, size.Height)
                if hasattr(s, "getString"):
                    text = _text_helpers.normalize_linebreaks(safe_call(s.getString, "Get string"))
                    if text:
                        ctx_str += ' text: "%s"' % text[:200]
                ctx_str += "\n"

            # Impress-specific: Speaker Notes
            if is_impress and hasattr(active_page, "getNotesPage"):
                try:
                    notes_page = safe_call(active_page.getNotesPage, "Get notes page")
                    notes_text = ""
                    for i in range(safe_call(notes_page.getCount, "Get notes page count")):
                        shape = safe_call(notes_page.getByIndex, "Get notes shape by index", i)
                        if safe_call(shape.getShapeType, "Get notes shape type") == "com.sun.star.presentation.NotesShape":
                            notes_text += safe_call(shape.getString, "Get notes shape string") + "\n"
                    if notes_text.strip():
                        ctx_str += "\nSpeaker Notes:\n%s\n" % notes_text.strip()
                except UnoObjectError:
                    pass

        return ctx_str
    except UnoObjectError:
        logging.getLogger(__name__).exception("get_draw_context_for_chat error")
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"
    except Exception:
        logging.getLogger(__name__).exception("get_draw_context_for_chat exception")
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"


def _inject_markers_into_excerpt(excerpt_text, excerpt_start, excerpt_end, sel_start, sel_end, prefix, suffix):
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


def resolve_locator(model, locator: str):
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


class DocumentService(ServiceBase):
    name = "document"

    def initialize(self, ctx):
        pass

    def get_active_document(self):
        return get_active_document()

    def resolve_document_by_url(self, url):
        """Resolve (doc, doc_type) by document URL; (None, None) if not found. Main-thread only."""
        return _resolve_document_by_url(get_ctx(), url)

    def detect_doc_type(self, doc):
        doc_type = _doc_type.get_document_type(doc)
        if doc_type == _doc_type.DocumentType.CALC:
            return "calc"
        if doc_type in (_doc_type.DocumentType.DRAW, _doc_type.DocumentType.IMPRESS):
            return "draw"
        return "writer"

    def is_writer(self, doc):
        return _doc_type.is_writer(doc)

    def is_calc(self, doc):
        return _doc_type.is_calc(doc)

    def is_draw(self, doc):
        return _doc_type.is_draw(doc)

    def get_full_text(self, doc, max_chars=8000):
        return get_full_document_text(doc, max_chars)

    def get_document_length(self, doc):
        return _text_helpers.get_document_length(doc)

    def get_document_context_for_chat(self, doc, max_context=CHAT_DOCUMENT_CONTEXT_MAX_CHARS, include_end=True, include_selection=True):
        return get_document_context_for_chat(doc, max_context, include_end, include_selection, get_ctx())

    def get_page_for_paragraph(self, model, para_index):
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

    def get_page_count(self, model):
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

    def doc_key(self, doc):
        """Return a stable key for the document for use in caches."""
        return id(doc)

    def get_paragraph_ranges(self, doc):
        """Return list of top-level paragraph elements."""
        return _get_paragraph_ranges(doc)

    def find_paragraph_for_range(self, anchor, para_ranges, text_obj=None):
        """Return the 0-based paragraph index that contains anchor."""
        return _find_paragraph_for_range(anchor, para_ranges, text_obj)

    def resolve_locator(self, doc, locator):
        """Resolve a locator string to a paragraph index or other document position."""
        return resolve_locator(doc, locator)

    def yield_to_gui(self):
        """Yield to the UI event loop (no-op here)."""
        pass

    def annotate_pages(self, children, doc):
        """Annotate tree children with page numbers (no-op here)."""
        pass

    def find_paragraph_element(self, doc, para_index):
        """Return (paragraph_element, None) for the given index, or (None, None) if out of range."""
        ranges = _get_paragraph_ranges(doc)
        if 0 <= para_index < len(ranges):
            return (ranges[para_index], None)
        return (None, None)
