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
import logging
import uno
import time
from enum import Enum, auto
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.analyzer import SheetAnalyzer
from plugin.framework.uno_context import get_active_document as get_active_doc
from plugin.framework.errors import UnoObjectError, DocumentDisposedError, check_disposed, safe_call, safe_uno_call


class DocumentType(Enum):
    UNKNOWN = auto()
    WRITER = auto()
    CALC = auto()
    DRAW = auto()
    IMPRESS = auto()


_DOCUMENT_SERVICE_MAP = {
    DocumentType.WRITER: "com.sun.star.text.TextDocument",
    DocumentType.CALC: "com.sun.star.sheet.SpreadsheetDocument",
    DocumentType.DRAW: "com.sun.star.drawing.DrawingDocument",
    DocumentType.IMPRESS: "com.sun.star.presentation.PresentationDocument"
}


@safe_uno_call(default=DocumentType.UNKNOWN)
def get_document_type(model):
    """Return the DocumentType for the given model."""
    if model is None:
        return DocumentType.UNKNOWN

    # Check services in priority order
    for doc_type, service_name in _DOCUMENT_SERVICE_MAP.items():
        if safe_call(model.supportsService, f"Check {service_name}", service_name):
            return doc_type

    return DocumentType.UNKNOWN


def is_writer(model):
    """Return True if model is a Writer document."""
    return get_document_type(model) == DocumentType.WRITER


def is_calc(model):
    """Return True if model is a Calc document."""
    return get_document_type(model) == DocumentType.CALC


def is_draw(model):
    """Return True if model is a Draw/Impress document."""
    doc_type = get_document_type(model)
    return doc_type in (DocumentType.DRAW, DocumentType.IMPRESS)


def get_document_property(model, name, default=None):
    """Get a custom document property from the model."""
    try:
        check_disposed(model, "Document Model")
        if hasattr(model, "getDocumentProperties"):
            doc_props = safe_call(model.getDocumentProperties, "Get document properties")
            props = doc_props.UserDefinedProperties
            if props is None:
                return default

            check_disposed(props, "UserDefinedProperties")

            # Use safe_call and specific logic for properties
            if hasattr(props, "hasByName"):
                if safe_call(props.hasByName, "Check property name", name):
                    return safe_call(props.getPropertyValue, "Get property value", name)
                return default

            if hasattr(props, "getPropertyValue"):
                # Fallback if hasByName is missing
                try:
                    return safe_call(props.getPropertyValue, "Get property value fallback", name)
                except UnoObjectError:
                    return default
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_document_property error: %s", e)
    return default


def set_document_property(model, name, value):
    """Set a custom document property in the model."""
    try:
        check_disposed(model, "Document Model")
        if hasattr(model, "getDocumentProperties"):
            doc_props = safe_call(model.getDocumentProperties, "Get document properties")
            props = doc_props.UserDefinedProperties
            if props is not None:
                check_disposed(props, "UserDefinedProperties")
                exists = False
                if hasattr(props, "hasByName"):
                    # Catch the UnoObjectError specifically to treat missing as exists=False
                    try:
                        exists = safe_call(props.hasByName, "Check property name", name)
                    except UnoObjectError:
                        exists = False

                from com.sun.star.beans.PropertyAttribute import REMOVABLE
                if not exists and hasattr(props, "addProperty"):
                    safe_call(props.addProperty, "Add property", name, REMOVABLE, str(value))
                elif hasattr(props, "setPropertyValue"):
                    try:
                        safe_call(props.setPropertyValue, "Set property value", name, str(value))
                    except UnoObjectError:
                        if hasattr(props, "addProperty"):
                            safe_call(props.addProperty, "Add property fallback", name, REMOVABLE, str(value))
                        else:
                            raise
    except UnoObjectError as e:
        # Fallback context enrichment
        doc_url = ""
        readonly = ""
        try:
            if hasattr(model, "getURL"):
                doc_url = model.getURL() or ""
            if hasattr(model, "isReadonly"):
                readonly = str(model.isReadonly())
        except Exception:
            pass

        logging.getLogger(__name__).warning(
            "set_document_property error: %s (url=%s, readonly=%s)", e, doc_url, readonly
        )
        raise


def normalize_linebreaks(text: str) -> str:
    """Normalize various linebreak sequences (\\r\\n, \\n\\r, \\r) to \\n."""
    if not text:
        return ""
    # Chain replacements to handle all cases safely
    return text.replace("\r\n", "\n").replace("\n\r", "\n").replace("\r", "\n")


# class DocumentCache:
#     """Cache for expensive UNO calls, tied to a document model."""
#     _instances = {}  # {id(model): cache}
#
#     def __init__(self):
#         self.length = None
#         self.para_ranges = None
#         self.page_cache = {}  # (search_key) -> page_number
#         self.last_invalidated = time.time()
#
#     @classmethod
#     def get(cls, model):
#         mid = id(model)
#         if mid not in cls._instances:
#             cls._instances[mid] = DocumentCache()
#         return cls._instances[mid]
#
#     @classmethod
#     def invalidate(cls, model):
#         mid = id(model)
#         if mid in cls._instances:
#             del cls._instances[mid]



def _normalize_doc_url(url):
    """Normalize document URL for comparison (strip, optional trailing slash)."""
    if not url:
        return ""
    s = str(url).strip()
    if s.endswith("/") and len(s) > 1:
        s = s[:-1]
    return s


def resolve_document_by_url(ctx, url):
    """Resolve an open document by URL. Must be called on the UNO main thread.

    Returns (doc, doc_type) or (None, None) if not found.
    doc_type is one of 'writer', 'calc', 'draw'.
    """
    if not url or not str(url).strip():
        return (None, None)
    from plugin.framework.uno_context import get_desktop
    target = _normalize_doc_url(url)
    try:
        desktop = get_desktop(ctx)
        comps = desktop.getComponents()
        if not comps:
            return (None, None)
        enum = comps.createEnumeration()
        if not enum:
            return (None, None)
        while enum and enum.hasMoreElements():
            elem = enum.nextElement()
            try:
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and elem.getController():
                    model = elem.getController().getModel()
                if model and hasattr(model, "getURL"):
                    doc_url = _normalize_doc_url(model.getURL())
                    if doc_url and doc_url == target:
                        doc_type_enum = get_document_type(model)
                        doc_type = "writer"
                        if doc_type_enum == DocumentType.CALC:
                            doc_type = "calc"
                        elif doc_type_enum in (DocumentType.DRAW, DocumentType.IMPRESS):
                            doc_type = "draw"
                        return (model, doc_type)
            except Exception as e:
                logging.getLogger(__name__).debug("resolve_document_by_url element error: %s", type(e).__name__)
                continue
    except Exception as e:
        logging.getLogger(__name__).warning("resolve_document_by_url enumeration error: %s", type(e).__name__)
    return (None, None)


def get_document_path(model):
    """Return the local filesystem path for the document, or None if not a file URL (e.g. untitled)."""
    try:
        url = model.getURL()
        if not url or not str(url).startswith("file://"):
            return None
        return str(uno.fileUrlToSystemPath(url))
    except Exception as e:
        logging.getLogger(__name__).debug("get_document_path exception: %s", type(e).__name__)
        return None


def get_full_document_text(model, max_chars=8000):
    """Get full document text for Writer or summary for Calc, truncated to max_chars."""
    try:
        check_disposed(model, "Document Model")
        doc_type = get_document_type(model)
        
        if doc_type == DocumentType.CALC:
            # Calc document
            bridge = CalcBridge(model)
            analyzer = SheetAnalyzer(bridge)
            summary = analyzer.get_sheet_summary()
            text = f"Sheet: {summary['sheet_name']}\nUsed Range: {summary['used_range']}\n"
            text += f"Columns: {', '.join(filter(None, summary['headers']))}\n"
            # Maybe add some preview rows?
            return text
        
        if doc_type == DocumentType.WRITER:
            text = safe_call(model.getText, "Get document text")
            cursor = safe_call(text.createTextCursor, "Create text cursor")
            safe_call(cursor.gotoStart, "Cursor gotoStart", False)
            safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
            full = safe_call(cursor.getString, "Cursor getString")
            if len(full) > max_chars:
                full = full[:max_chars] + "\n\n[... document truncated ...]"
            return full
        
        if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
            return get_draw_context_for_chat(model, max_chars)
            
        return ""
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_full_document_text error: %s", e)
        return ""


def get_document_end(model, max_chars=4000):
    """Get the last max_chars of the document."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", False)
        safe_call(cursor.gotoStart, "Cursor gotoStart", True)  # expand backward to select from start to end
        full = safe_call(cursor.getString, "Cursor getString")
        if len(full) <= max_chars:
            return full
        return full[-max_chars:]
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_document_end error: %s", e)
        return ""


# goRight(nCount, bExpand) takes short; max 32767 per call
_GO_RIGHT_CHUNK = 8192


def get_document_length(model):
    """Return total character length of the document. Returns 0 on error."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
        length = len(normalize_linebreaks(safe_call(cursor.getString, "Cursor getString")))
        return length
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_document_length error: %s", e)
        return 0


def get_text_cursor_at_range(model, start_offset, end_offset):
    """Return a text cursor that selects the character range [start_offset, end_offset).
    The cursor is positioned at start and expanded to end so caller can setString('') and insert.
    goRight is used in chunks because UNO's goRight takes short (max 32767).
    Returns None on error or invalid range."""
    try:
        check_disposed(model, "Document Model")
        doc_len = get_document_length(model)
        start_offset = max(0, min(start_offset, doc_len))
        end_offset = max(0, min(end_offset, doc_len))
        if start_offset > end_offset:
            start_offset, end_offset = end_offset, start_offset
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        # Move to start_offset in chunks
        remaining = start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            safe_call(cursor.goRight, "Cursor goRight", n, False)
            remaining -= n
        # Expand selection by (end_offset - start_offset)
        remaining = end_offset - start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            safe_call(cursor.goRight, "Cursor goRight", n, True)
            remaining -= n
        return cursor
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_text_cursor_at_range error: %s", e)
        return None


def get_selection_range(model):
    """Return (start_offset, end_offset) character positions into the document.
    Cursor (no selection) = same start and end. Returns (0, 0) on error or no text range."""
    try:
        check_disposed(model, "Document Model")
        controller = safe_call(model.getCurrentController, "Get current controller")
        sel = safe_call(controller.getSelection, "Get selection")
        if not sel or safe_call(sel.getCount, "Get selection count") == 0:
            # No selection: use view cursor for insertion point
            vc = safe_call(controller.getViewCursor, "Get view cursor")
            rng = vc
        else:
            rng = safe_call(sel.getByIndex, "Get selection by index", 0)
        if not rng or not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            return (0, 0)
        text = safe_call(model.getText, "Get document text")
        cursor = safe_call(text.createTextCursor, "Create text cursor")
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        safe_call(cursor.gotoRange, "Cursor gotoRange start", safe_call(rng.getStart, "Get range start"), True)
        start_offset = len(normalize_linebreaks(safe_call(cursor.getString, "Cursor getString start")))
        safe_call(cursor.gotoStart, "Cursor gotoStart", False)
        safe_call(cursor.gotoRange, "Cursor gotoRange end", safe_call(rng.getEnd, "Get range end"), True)
        end_offset = len(normalize_linebreaks(safe_call(cursor.getString, "Cursor getString end")))
        return (start_offset, end_offset)
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_selection_range error: %s", e)
        return (0, 0)


def get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=True, ctx=None):
    """Build a single context string for chat. Handles Writer and Calc.
    ctx: component context (required for Calc and Draw documents)."""
    doc_type = get_document_type(model)

    if doc_type == DocumentType.CALC:
        return get_calc_context_for_chat(model, max_context, ctx)
    
    if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
        return get_draw_context_for_chat(model, max_context, ctx)
    
    # Original Writer logic
    if doc_type == DocumentType.WRITER:
        try:
            check_disposed(model, "Document Model")
            text = safe_call(model.getText, "Get document text")
            # ... (rest of the function)
            cursor = safe_call(text.createTextCursor, "Create text cursor")
            safe_call(cursor.gotoStart, "Cursor gotoStart", False)
            safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
            full = normalize_linebreaks(safe_call(cursor.getString, "Cursor getString"))
            doc_len = len(full)
        except UnoObjectError as e:
            logging.getLogger(__name__).warning("get_document_context_for_chat Writer error: %s", e)
            return "[Unable to read Writer document context. The document may be locked or initializing.]"

        # Context Extensions (Memory, User Profile, Skills)
        # UNCOMMENT TO ENABLE:
        # try:
        #     if ctx:
        #         from plugin.modules.chatbot.memory import MemoryStore
        #         from plugin.modules.chatbot.skills import SkillsStore
        #         m_store = MemoryStore(ctx)
        #         memory_text = m_store.read("memory")
        #         user_text = m_store.read("user")
        #         s_store = SkillsStore(ctx)
        #         skills = s_store.find_all_skills()
        #         ext_ctx = ""
        #         if user_text:
        #             ext_ctx += f"\n[USER PROFILE]\n{user_text}\n"
        #         if memory_text:
        #             ext_ctx += f"\n[AGENT MEMORY]\n{memory_text}\n"
        #         if skills:
        #             ext_ctx += f"\n[AVAILABLE SKILLS]\n" + ", ".join(s['name'] for s in skills) + "\n"
        #         if ext_ctx:
        #             full = ext_ctx + "\n" + full
        #             doc_len = len(full)
        # except Exception as e:
        #     logging.getLogger(__name__).warning("Failed to inject memory context: %s", e)

        # Selection/cursor range; cap selection span for very long selections (e.g. 100k chars)
        start_offset, end_offset = (0, 0)
        if include_selection:
            start_offset, end_offset = get_selection_range(model)
            # Clamp to document bounds
            start_offset = max(0, min(start_offset, doc_len))
            end_offset = max(0, min(end_offset, doc_len))
            if start_offset > end_offset:
                start_offset, end_offset = end_offset, start_offset
            # Optional: cap selection span so we don't force huge context (e.g. 2000 chars max span for "selection" in excerpts)
            max_selection_span = 2000
            if end_offset - start_offset > max_selection_span:
                end_offset = start_offset + max_selection_span

        # Budget split: half for start, half for end when include_end
        if include_end and doc_len > (max_context // 2):
            start_chars = max_context // 2
            end_chars = max_context - start_chars
            start_excerpt = full[:start_chars]
            end_excerpt = full[-end_chars:]
            # Inject markers into start excerpt
            start_excerpt = _inject_markers_into_excerpt(
                start_excerpt, 0, start_chars, start_offset, end_offset, "[DOCUMENT START]\n", "\n[DOCUMENT END]"
            )
            # Inject markers into end excerpt (offsets relative to document; excerpt starts at doc_len - end_chars)
            end_excerpt = _inject_markers_into_excerpt(
                end_excerpt, doc_len - end_chars, doc_len, start_offset, end_offset, "[DOCUMENT END]\n", "\n[END DOCUMENT]"
            )
            middle_note = "\n\n[... middle of document omitted ...]\n\n" if doc_len > max_context else ""
            return (
                "Document length: %d characters.\n\n%s%s%s"
                % (doc_len, start_excerpt, middle_note, end_excerpt)
            )
        else:
            # Short doc or start-only: one block
            take = min(doc_len, max_context)
            excerpt = full[:take]
            if doc_len > max_context:
                excerpt += "\n\n[... document truncated ...]"
            content_len = take  # character range we're showing (before truncation message)
            excerpt = _inject_markers_into_excerpt(
                excerpt, 0, content_len, start_offset, end_offset, "[DOCUMENT START]\n", "\n[END DOCUMENT]"
            )
            return "Document length: %d characters.\n\n%s" % (doc_len, excerpt)
    
    return ""


def get_calc_context_for_chat(model, max_context=8000, ctx=None):
    """Get context summary for a Calc spreadsheet."""
    if ctx is None:
        raise ValueError("ctx is required for get_calc_context_for_chat")
    try:
        check_disposed(model, "Document Model")
        bridge = CalcBridge(model)
        analyzer = SheetAnalyzer(bridge)
        summary = analyzer.get_sheet_summary()

        ctx_str = f"Spreadsheet Document: {model.getURL() or 'Untitled'}\n"
        ctx_str += f"Active Sheet: {summary['sheet_name']}\n"
        ctx_str += f"Used Range: {summary['used_range']} ({summary['row_count']} rows x {summary['col_count']} columns)\n"
        ctx_str += f"Columns: {', '.join([str(h) for h in summary['headers'] if h])}\n"

        # Add selection context if available
        controller = safe_call(model.getCurrentController, "Get current controller")
        selection = safe_call(controller.getSelection, "Get selection")
        if selection:
            if hasattr(selection, "getRangeAddress"):
                addr = safe_call(selection.getRangeAddress, "Get range address")
                from plugin.modules.calc.address_utils import index_to_column
                sel_range = f"{index_to_column(addr.StartColumn)}{addr.StartRow + 1}:{index_to_column(addr.EndColumn)}{addr.EndRow + 1}"
                ctx_str += f"Current Selection: {sel_range}\n"

                # Check for selected values if small
                if (addr.EndRow - addr.StartRow + 1) * (addr.EndColumn - addr.StartColumn + 1) < 100:
                    from plugin.modules.calc.inspector import CellInspector
                    inspector = CellInspector(bridge)
                    cells = inspector.read_range(sel_range)
                    ctx_str += "Selection Content (CSV-like):\n"
                    for row in cells:
                        ctx_str += ", ".join([str(c['value']) if c['value'] is not None else "" for c in row]) + "\n"

        return ctx_str
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_calc_context_for_chat error: %s", e)
        return "[Unable to read Calc spreadsheet context. The document may be locked or initializing.]"
    except Exception as e:
        logging.getLogger(__name__).warning("get_calc_context_for_chat exception: %s", type(e).__name__)
        return "[Unable to read Calc spreadsheet context. The document may be locked or initializing.]"


def get_draw_context_for_chat(model, max_context=8000, ctx=None):
    """Get context summary for a Draw/Impress document. ctx: component context (unused, kept for signature compat)."""
    try:
        check_disposed(model, "Document Model")
        from plugin.modules.draw.bridge import DrawBridge
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
                ctx_str += "- [%d] %s: pos(%d, %d) size(%dx%d)" % (
                    i, type_name, pos.X, pos.Y, size.Width, size.Height)
                if hasattr(s, "getString"):
                    text = normalize_linebreaks(safe_call(s.getString, "Get string"))
                    if text:
                        ctx_str += " text: \"%s\"" % text[:200]
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
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("get_draw_context_for_chat error: %s", e)
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"
    except Exception as e:
        logging.getLogger(__name__).warning("get_draw_context_for_chat exception: %s", type(e).__name__)
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


# ---------------------------------------------------------------------------
# Navigation & Outline (Ported from extension)
# ---------------------------------------------------------------------------

import uuid

def get_paragraph_ranges(model):
    """Return list of top-level paragraph elements."""
    text = model.getText()
    enum = text.createEnumeration()
    ranges = []
    while enum.hasMoreElements():
        ranges.append(enum.nextElement())
    return ranges


def find_paragraph_for_range(match_range, para_ranges, text_obj=None):
    """Return the 0-based paragraph index that contains match_range."""
    try:
        if text_obj is None:
            text_obj = safe_call(match_range.getText, "Get text object")
        match_start = safe_call(match_range.getStart, "Get match start")
        for i, para in enumerate(para_ranges):
            try:
                # compareRegionStarts: 1 if first is after second, -1 if before, 0 if equal
                cmp_start = safe_call(text_obj.compareRegionStarts, "compareRegionStarts start", match_start, safe_call(para.getStart, "Get para start"))
                cmp_end = safe_call(text_obj.compareRegionStarts, "compareRegionStarts end", match_start, safe_call(para.getEnd, "Get para end"))
                if cmp_start <= 0 and cmp_end >= 0:
                    return i
            except UnoObjectError as e:
                logging.getLogger(__name__).debug("find_paragraph_for_range comparison error at index %d: %s", i, e)
                continue
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("find_paragraph_for_range error: %s", e)
    return 0


def build_heading_tree(model):
    """Build a hierarchical heading tree. Single pass enumeration."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        enum = safe_call(text.createEnumeration, "Create enumeration")
        root = {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}
        stack = [root]
        para_index = 0

        while safe_call(enum.hasMoreElements, "Check more elements"):
            element = safe_call(enum.nextElement, "Get next element")
            if safe_call(element.supportsService, "Check supportsService Paragraph", "com.sun.star.text.Paragraph"):
                outline_level = 0
                try:
                    outline_level = safe_call(element.getPropertyValue, "Get OutlineLevel", "OutlineLevel")
                except UnoObjectError as e:
                    logging.getLogger(__name__).debug("build_heading_tree could not get OutlineLevel: %s", e)

                if outline_level > 0:
                    while len(stack) > 1 and stack[-1]["level"] >= outline_level:
                        stack.pop()
                    node = {
                        "level": outline_level,
                        "text": safe_call(element.getString, "Get paragraph string"),
                        "para_index": para_index,
                        "children": [],
                        "body_paragraphs": 0
                    }
                    stack[-1]["children"].append(node)
                    stack.append(node)
                else:
                    stack[-1]["body_paragraphs"] += 1
            elif safe_call(element.supportsService, "Check supportsService TextTable", "com.sun.star.text.TextTable"):
                stack[-1]["body_paragraphs"] += 1
            para_index += 1
        return root
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("build_heading_tree error: %s", e)
        return {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}


def ensure_heading_bookmarks(model):
    """Ensure every heading has an _mcp_ bookmark. Returns {para_index: bookmark_name}."""
    try:
        check_disposed(model, "Document Model")
        text = safe_call(model.getText, "Get document text")
        para_ranges = get_paragraph_ranges(model)

        # 1. Map existing _mcp_ bookmarks
        existing_map = {}
        if hasattr(model, "getBookmarks"):
            bookmarks = safe_call(model.getBookmarks, "Get bookmarks")
            for name in safe_call(bookmarks.getElementNames, "Get element names"):
                if name.startswith("_mcp_"):
                    bm = safe_call(bookmarks.getByName, "Get bookmark by name", name)
                    idx = find_paragraph_for_range(safe_call(bm.getAnchor, "Get bookmark anchor"), para_ranges, text)
                    existing_map[idx] = name
        
        # 2. Scanthe document for headings
        enum = safe_call(text.createEnumeration, "Create enumeration")
        para_index = 0
        bookmark_map = {}
        needs_bookmark = []
        
        while safe_call(enum.hasMoreElements, "Check more elements"):
            element = safe_call(enum.nextElement, "Get next element")
            if safe_call(element.supportsService, "Check supportsService Paragraph", "com.sun.star.text.Paragraph"):
                try:
                    if safe_call(element.getPropertyValue, "Get OutlineLevel", "OutlineLevel") > 0:
                        if para_index in existing_map:
                            bookmark_map[para_index] = existing_map[para_index]
                        else:
                            needs_bookmark.append((para_index, safe_call(element.getStart, "Get element start")))
                except UnoObjectError as e:
                    logging.getLogger(__name__).debug("ensure_heading_bookmarks could not get OutlineLevel: %s", e)
            para_index += 1

        # 3. Add missing bookmarks
        for idx, start_range in needs_bookmark:
            name = f"_mcp_{uuid.uuid4().hex[:8]}"
            bookmark = safe_call(model.createInstance, "Create bookmark instance", "com.sun.star.text.Bookmark")
            bookmark.Name = name
            cursor = safe_call(text.createTextCursorByRange, "Create cursor by range", start_range)
            safe_call(text.insertTextContent, "Insert text content", cursor, bookmark, False)
            bookmark_map[idx] = name

        return bookmark_map
    except UnoObjectError as e:
        logging.getLogger(__name__).warning("ensure_heading_bookmarks error: %s", e)
        return {}


def resolve_locator(model, locator: str):
    """Resolve a locator string to a paragraph index or other document position."""
    loc_type, sep, loc_value = locator.partition(":")
    if not sep:
        return {"para_index": 0}
        
    if loc_type == "paragraph":
        return {"para_index": int(loc_value)}
        
    if loc_type == "heading":
        parts = []
        try:
            parts = [int(p) for p in loc_value.split(".")]
        except Exception as e:
            logging.getLogger(__name__).warning("resolve_locator heading parse error: %s", type(e).__name__)
            return {"para_index": 0}
        
        tree = build_heading_tree(model)
        node = tree
        for part in parts:
            children = node.get("children", [])
            if 1 <= part <= len(children):
                node = children[part-1]
            else:
                break
        return {"para_index": node["para_index"]}
        
    if loc_type == "bookmark":
        if hasattr(model, "getBookmarks"):
            bms = model.getBookmarks()
            if bms.hasByName(loc_value):
                anchor = bms.getByName(loc_value).getAnchor()
                para_ranges = get_paragraph_ranges(model)
                return {"para_index": find_paragraph_for_range(anchor, para_ranges, model.getText())}
    
    return {"para_index": 0}


from plugin.framework.service_base import ServiceBase
from plugin.framework.uno_context import get_ctx

class DocumentService(ServiceBase):
    name = "document"

    def initialize(self, ctx):
        pass

    def get_active_document(self):
        return get_active_doc()

    def resolve_document_by_url(self, url):
        """Resolve (doc, doc_type) by document URL; (None, None) if not found. Main-thread only."""
        return resolve_document_by_url(get_ctx(), url)

    def detect_doc_type(self, doc):
        doc_type = get_document_type(doc)
        if doc_type == DocumentType.CALC: return "calc"
        if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS): return "draw"
        return "writer"

    def is_writer(self, doc): return is_writer(doc)
    def is_calc(self, doc): return is_calc(doc)
    def is_draw(self, doc): return is_draw(doc)
    def get_full_text(self, doc, max_chars=8000): return get_full_document_text(doc, max_chars)
    def get_document_length(self, doc): return get_document_length(doc)
    def get_document_context_for_chat(self, doc, max_context=8000, include_end=True, include_selection=True):
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
                for _ in range(para_index):
                    if not safe_call(cursor.gotoNextParagraph, "Cursor gotoNextParagraph", False):
                        break
                safe_call(vc.gotoRange, "View cursor gotoRange", cursor, False)
                page = safe_call(vc.getPage, "Get page")
            finally:
                safe_call(vc.gotoRange, "Restore view cursor", saved, False)
                safe_call(model.unlockControllers, "Unlock controllers")
            return page
        except UnoObjectError as e:
            logging.getLogger(__name__).warning("get_page_for_paragraph error: %s", e)
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
        except UnoObjectError as e:
            logging.getLogger(__name__).warning("get_page_count error: %s", e)
            return 0

    def doc_key(self, doc):
        """Return a stable key for the document for use in caches."""
        return id(doc)

    def get_paragraph_ranges(self, doc):
        """Return list of top-level paragraph elements. Uses DocumentCache."""
        return get_paragraph_ranges(doc)

    def find_paragraph_for_range(self, anchor, para_ranges, text_obj=None):
        """Return the 0-based paragraph index that contains anchor."""
        return find_paragraph_for_range(anchor, para_ranges, text_obj)

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
        ranges = get_paragraph_ranges(doc)
        if 0 <= para_index < len(ranges):
            return (ranges[para_index], None)
        return (None, None)
