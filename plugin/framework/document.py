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
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.analyzer import SheetAnalyzer
from plugin.framework.uno_context import get_active_document as get_active_doc
from plugin.framework.errors import UnoObjectError


def is_writer(model):
    """Return True if model is a Writer document."""
    try:
        return model.supportsService("com.sun.star.text.TextDocument")
    except Exception as e:
        logging.getLogger(__name__).debug("is_writer exception: %s", e)
        return False


def is_calc(model):
    """Return True if model is a Calc document."""
    try:
        return model.supportsService("com.sun.star.sheet.SpreadsheetDocument")
    except Exception as e:
        logging.getLogger(__name__).debug("is_calc exception: %s", e)
        return False


def is_draw(model):
    """Return True if model is a Draw/Impress document."""
    try:
        return (model.supportsService("com.sun.star.drawing.DrawingDocument") or
                model.supportsService("com.sun.star.presentation.PresentationDocument"))
    except Exception as e:
        logging.getLogger(__name__).debug("is_draw exception: %s", e)
        return False


def get_document_property(model, name, default=None):
    """Get a custom document property from the model."""
    try:
        if hasattr(model, "getDocumentProperties"):
            props = model.getDocumentProperties().UserDefinedProperties
            if props is None:
                return default

            # Some LibreOffice builds expose hasByName; others don't.
            if hasattr(props, "hasByName"):
                try:
                    if props.hasByName(name):
                        return props.getPropertyValue(name)
                    return default
                except Exception:
                    # Fall back to getPropertyValue attempt below.
                    pass

            if hasattr(props, "getPropertyValue"):
                try:
                    return props.getPropertyValue(name)
                except Exception:
                    return default
    except Exception as e:
        logging.getLogger(__name__).warning("get_document_property error: %s", e)
    return default


def set_document_property(model, name, value):
    """Set a custom document property in the model."""
    try:
        if hasattr(model, "getDocumentProperties"):
            props = model.getDocumentProperties().UserDefinedProperties
            if props is not None:
                # Some LibreOffice builds expose hasByName; others don't.
                # Prefer hasByName+addProperty when available, otherwise fall
                # back to setPropertyValue and, on UnknownPropertyException,
                # retry with addProperty.
                exists = False
                if hasattr(props, "hasByName"):
                    try:
                        exists = props.hasByName(name)
                    except Exception:
                        exists = False

                from com.sun.star.beans.PropertyAttribute import REMOVABLE
                if not exists and hasattr(props, "addProperty"):
                    # Using a fixed type (string) for session IDs
                    props.addProperty(name, REMOVABLE, str(value))
                elif hasattr(props, "setPropertyValue"):
                    try:
                        props.setPropertyValue(name, str(value))
                    except Exception:
                        # Some implementations raise UnknownPropertyException
                        # when the property does not yet exist; try addProperty.
                        try:
                            if hasattr(props, "addProperty"):
                                props.addProperty(name, REMOVABLE, str(value))
                        except Exception as inner_e:
                            raise UnoObjectError(f"Failed to set document property: {inner_e}", context={"property": name}) from inner_e
    except Exception as e:
        # Fallback to debug log if available, but avoid circular imports.
        # We log richer context here to help diagnose benign startup errors
        # like the commonly-seen "-1" when setting UserDefinedProperties.
        try:
            doc_url = ""
            readonly = ""
            try:
                if hasattr(model, "getURL"):
                    doc_url = model.getURL() or ""
            except Exception:
                pass
            try:
                if hasattr(model, "isReadonly"):
                    readonly = str(model.isReadonly())
            except Exception:
                pass
            logging.getLogger(__name__).warning(
                "set_document_property error: %r (type=%s, url=%s, readonly=%s)"
                % (e, type(e).__name__, doc_url, readonly)
            )
        except Exception:
            pass
        raise UnoObjectError(f"Error setting document property: {e}", context={"property": name}) from e


def normalize_linebreaks(text: str) -> str:
    """Normalize various linebreak sequences (\\r\\n, \\n\\r, \\r) to \\n."""
    if not text:
        return ""
    # Chain replacements to handle all cases safely
    return text.replace("\r\n", "\n").replace("\n\r", "\n").replace("\r", "\n")


class DocumentCache:
    """Cache for expensive UNO calls, tied to a document model."""
    _instances = {}  # {id(model): cache}

    def __init__(self):
        self.length = None
        self.para_ranges = None
        self.page_cache = {}  # (search_key) -> page_number
        self.last_invalidated = time.time()

    @classmethod
    def get(cls, model):
        mid = id(model)
        if mid not in cls._instances:
            cls._instances[mid] = DocumentCache()
        return cls._instances[mid]

    @classmethod
    def invalidate(cls, model):
        mid = id(model)
        if mid in cls._instances:
            del cls._instances[mid]



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
                        doc_type = "writer"
                        if is_calc(model):
                            doc_type = "calc"
                        elif is_draw(model):
                            doc_type = "draw"
                        return (model, doc_type)
            except Exception as e:
                logging.getLogger(__name__).debug("resolve_document_by_url element error: %s", e)
                continue
    except Exception as e:
        logging.getLogger(__name__).warning("resolve_document_by_url enumeration error: %s", e)
    return (None, None)


def get_document_path(model):
    """Return the local filesystem path for the document, or None if not a file URL (e.g. untitled)."""
    try:
        url = model.getURL()
        if not url or not str(url).startswith("file://"):
            return None
        return str(uno.fileUrlToSystemPath(url))
    except Exception as e:
        logging.getLogger(__name__).debug("get_document_path exception: %s", e)
        return None


def get_full_document_text(model, max_chars=8000):
    """Get full document text for Writer or summary for Calc, truncated to max_chars."""
    try:
        if is_calc(model):
            # Calc document
            bridge = CalcBridge(model)
            analyzer = SheetAnalyzer(bridge)
            summary = analyzer.get_sheet_summary()
            text = f"Sheet: {summary['sheet_name']}\nUsed Range: {summary['used_range']}\n"
            text += f"Columns: {', '.join(filter(None, summary['headers']))}\n"
            # Maybe add some preview rows?
            return text
        
        text = model.getText()
        # ... rest of Writer logic
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        full = cursor.getString()
        if len(full) > max_chars:
            full = full[:max_chars] + "\n\n[... document truncated ...]"
        return full
    except Exception as e:
        logging.getLogger(__name__).warning("get_full_document_text exception: %s", e)
        return ""


def get_document_end(model, max_chars=4000):
    """Get the last max_chars of the document."""
    try:
        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        cursor.gotoStart(True)  # expand backward to select from start to end
        full = cursor.getString()
        if len(full) <= max_chars:
            return full
        return full[-max_chars:]
    except Exception as e:
        logging.getLogger(__name__).warning("get_document_end exception: %s", e)
        return ""


# goRight(nCount, bExpand) takes short; max 32767 per call
_GO_RIGHT_CHUNK = 8192


def get_document_length(model):
    """Return total character length of the document. Returns 0 on error."""
    cache = DocumentCache.get(model)
    if cache.length is not None:
        return cache.length
    try:
        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        length = len(normalize_linebreaks(cursor.getString()))
        cache.length = length
        return length
    except Exception as e:
        logging.getLogger(__name__).warning("get_document_length exception: %s", e)
        return 0


def get_text_cursor_at_range(model, start_offset, end_offset):
    """Return a text cursor that selects the character range [start_offset, end_offset).
    The cursor is positioned at start and expanded to end so caller can setString('') and insert.
    goRight is used in chunks because UNO's goRight takes short (max 32767).
    Returns None on error or invalid range."""
    try:
        doc_len = get_document_length(model)
        start_offset = max(0, min(start_offset, doc_len))
        end_offset = max(0, min(end_offset, doc_len))
        if start_offset > end_offset:
            start_offset, end_offset = end_offset, start_offset
        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        # Move to start_offset in chunks
        remaining = start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            cursor.goRight(n, False)
            remaining -= n
        # Expand selection by (end_offset - start_offset)
        remaining = end_offset - start_offset
        while remaining > 0:
            n = min(remaining, _GO_RIGHT_CHUNK)
            cursor.goRight(n, True)
            remaining -= n
        return cursor
    except Exception as e:
        logging.getLogger(__name__).warning("get_text_cursor_at_range exception: %s", e)
        return None


def get_selection_range(model):
    """Return (start_offset, end_offset) character positions into the document.
    Cursor (no selection) = same start and end. Returns (0, 0) on error or no text range."""
    try:
        sel = model.getCurrentController().getSelection()
        if not sel or sel.getCount() == 0:
            # No selection: use view cursor for insertion point
            vc = model.getCurrentController().getViewCursor()
            rng = vc
        else:
            rng = sel.getByIndex(0)
        if not rng or not hasattr(rng, "getStart") or not hasattr(rng, "getEnd"):
            return (0, 0)
        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoRange(rng.getStart(), True)
        start_offset = len(normalize_linebreaks(cursor.getString()))
        cursor.gotoStart(False)
        cursor.gotoRange(rng.getEnd(), True)
        end_offset = len(normalize_linebreaks(cursor.getString()))
        return (start_offset, end_offset)
    except Exception as e:
        logging.getLogger(__name__).warning("get_selection_range exception: %s", e)
        return (0, 0)


def get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=True, ctx=None):
    """Build a single context string for chat. Handles Writer and Calc.
    ctx: component context (required for Calc and Draw documents)."""
    if is_calc(model):
        return get_calc_context_for_chat(model, max_context, ctx)
    
    if is_draw(model):
        return get_draw_context_for_chat(model, max_context, ctx)
    
    # Original Writer logic
    try:
        text = model.getText()
        # ... (rest of the function)
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        full = normalize_linebreaks(cursor.getString())
        doc_len = len(full)
    except Exception as e:
        logging.getLogger(__name__).warning("get_document_context_for_chat Writer exception: %s", e)
        return "Document length: 0.\n\n[DOCUMENT START]\n(empty)\n[END DOCUMENT]"

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


def get_calc_context_for_chat(model, max_context=8000, ctx=None):
    """Get context summary for a Calc spreadsheet."""
    if ctx is None:
        raise ValueError("ctx is required for get_calc_context_for_chat")
    try:
        bridge = CalcBridge(model)
        analyzer = SheetAnalyzer(bridge)
        summary = analyzer.get_sheet_summary()

        ctx_str = f"Spreadsheet Document: {model.getURL() or 'Untitled'}\n"
        ctx_str += f"Active Sheet: {summary['sheet_name']}\n"
        ctx_str += f"Used Range: {summary['used_range']} ({summary['row_count']} rows x {summary['col_count']} columns)\n"
        ctx_str += f"Columns: {', '.join([str(h) for h in summary['headers'] if h])}\n"

        # Add selection context if available
        controller = model.getCurrentController()
        selection = controller.getSelection()
        if selection:
            if hasattr(selection, "getRangeAddress"):
                addr = selection.getRangeAddress()
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
    except Exception as e:
        return f"Error getting Calc context: {e}"


def get_draw_context_for_chat(model, max_context=8000, ctx=None):
    """Get context summary for a Draw/Impress document. ctx: component context (unused, kept for signature compat)."""
    try:
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(model)
        pages = bridge.get_pages()
        active_page = bridge.get_active_page()
        
        is_impress = model.supportsService("com.sun.star.presentation.PresentationDocument")
        doc_type = "Impress Presentation" if is_impress else "Draw Document"

        ctx_str = "%s: %s\n" % (doc_type, model.getURL() or "Untitled")
        ctx_str += "Total %s: %d\n" % ("Slides" if is_impress else "Pages", pages.getCount())

        # Get index of active page
        active_page_idx = -1
        for i in range(pages.getCount()):
            if pages.getByIndex(i) == active_page:
                active_page_idx = i
                break

        ctx_str += "Active %s Index: %d\n" % ("Slide" if is_impress else "Page", active_page_idx)

        # Summarize shapes on active page
        if active_page:
            shapes = bridge.get_shapes(active_page)
            ctx_str += "\nShapes on %s %d:\n" % ("Slide" if is_impress else "Page", active_page_idx)
            for i, s in enumerate(shapes):
                type_name = s.getShapeType().split(".")[-1]
                pos = s.getPosition()
                size = s.getSize()
                ctx_str += "- [%d] %s: pos(%d, %d) size(%dx%d)" % (
                    i, type_name, pos.X, pos.Y, size.Width, size.Height)
                if hasattr(s, "getString"):
                    text = normalize_linebreaks(s.getString())
                    if text:
                        ctx_str += " text: \"%s\"" % text[:200]
                ctx_str += "\n"
            
            # Impress-specific: Speaker Notes
            if is_impress and hasattr(active_page, "getNotesPage"):
                try:
                    notes_page = active_page.getNotesPage()
                    notes_text = ""
                    for i in range(notes_page.getCount()):
                        shape = notes_page.getByIndex(i)
                        if shape.getShapeType() == "com.sun.star.presentation.NotesShape":
                            notes_text += shape.getString() + "\n"
                    if notes_text.strip():
                        ctx_str += "\nSpeaker Notes:\n%s\n" % notes_text.strip()
                except Exception:
                    pass

        return ctx_str
    except Exception as e:
        return "Error getting Draw context: %s" % e


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
    """Return list of top-level paragraph elements. Uses DocumentCache."""
    cache = DocumentCache.get(model)
    if cache.para_ranges is not None:
        return cache.para_ranges
    
    text = model.getText()
    enum = text.createEnumeration()
    ranges = []
    while enum.hasMoreElements():
        ranges.append(enum.nextElement())
    cache.para_ranges = ranges
    return ranges


def find_paragraph_for_range(match_range, para_ranges, text_obj=None):
    """Return the 0-based paragraph index that contains match_range."""
    try:
        if text_obj is None:
            text_obj = match_range.getText()
        match_start = match_range.getStart()
        for i, para in enumerate(para_ranges):
            try:
                # compareRegionStarts: 1 if first is after second, -1 if before, 0 if equal
                cmp_start = text_obj.compareRegionStarts(match_start, para.getStart())
                cmp_end = text_obj.compareRegionStarts(match_start, para.getEnd())
                if cmp_start <= 0 and cmp_end >= 0:
                    return i
            except Exception as e:
                logging.getLogger(__name__).debug("find_paragraph_for_range comparison error at index %d: %s", i, e)
                continue
    except Exception as e:
        logging.getLogger(__name__).warning("find_paragraph_for_range exception: %s", e)
    return 0


def build_heading_tree(model):
    """Build a hierarchical heading tree. Single pass enumeration."""
    text = model.getText()
    enum = text.createEnumeration()
    root = {"level": 0, "text": "root", "para_index": -1, "children": [], "body_paragraphs": 0}
    stack = [root]
    para_index = 0

    while enum.hasMoreElements():
        element = enum.nextElement()
        if element.supportsService("com.sun.star.text.Paragraph"):
            outline_level = 0
            try:
                outline_level = element.getPropertyValue("OutlineLevel")
            except Exception as e:
                logging.getLogger(__name__).debug("build_heading_tree could not get OutlineLevel: %s", e)
            
            if outline_level > 0:
                while len(stack) > 1 and stack[-1]["level"] >= outline_level:
                    stack.pop()
                node = {
                    "level": outline_level,
                    "text": element.getString(),
                    "para_index": para_index,
                    "children": [],
                    "body_paragraphs": 0
                }
                stack[-1]["children"].append(node)
                stack.append(node)
            else:
                stack[-1]["body_paragraphs"] += 1
        elif element.supportsService("com.sun.star.text.TextTable"):
            stack[-1]["body_paragraphs"] += 1
        para_index += 1
    return root


def ensure_heading_bookmarks(model):
    """Ensure every heading has an _mcp_ bookmark. Returns {para_index: bookmark_name}."""
    cache = DocumentCache.get(model)
    
    text = model.getText()
    para_ranges = get_paragraph_ranges(model)
    
    # 1. Map existing _mcp_ bookmarks
    existing_map = {}
    if hasattr(model, "getBookmarks"):
        bookmarks = model.getBookmarks()
        for name in bookmarks.getElementNames():
            if name.startswith("_mcp_"):
                bm = bookmarks.getByName(name)
                idx = find_paragraph_for_range(bm.getAnchor(), para_ranges, text)
                existing_map[idx] = name
    
    # 2. Scanthe document for headings
    enum = text.createEnumeration()
    para_index = 0
    bookmark_map = {}
    needs_bookmark = []
    
    while enum.hasMoreElements():
        element = enum.nextElement()
        if element.supportsService("com.sun.star.text.Paragraph"):
            try:
                if element.getPropertyValue("OutlineLevel") > 0:
                    if para_index in existing_map:
                        bookmark_map[para_index] = existing_map[para_index]
                    else:
                        needs_bookmark.append((para_index, element.getStart()))
            except Exception as e:
                logging.getLogger(__name__).debug("ensure_heading_bookmarks could not get OutlineLevel: %s", e)
        para_index += 1
        
    # 3. Add missing bookmarks
    for idx, start_range in needs_bookmark:
        name = f"_mcp_{uuid.uuid4().hex[:8]}"
        bookmark = model.createInstance("com.sun.star.text.Bookmark")
        bookmark.Name = name
        cursor = text.createTextCursorByRange(start_range)
        text.insertTextContent(cursor, bookmark, False)
        bookmark_map[idx] = name
        
    return bookmark_map


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
            logging.getLogger(__name__).warning("resolve_locator heading parse error: %s", e)
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
        if is_calc(doc): return "calc"
        if is_draw(doc): return "draw"
        return "writer"

    def invalidate_cache(self, doc):
        DocumentCache.invalidate(doc)

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
            text = model.getText()
            controller = model.getCurrentController()
            vc = controller.getViewCursor()
            saved = text.createTextCursorByRange(vc.getStart())
            model.lockControllers()
            try:
                cursor = text.createTextCursor()
                cursor.gotoStart(False)
                for _ in range(para_index):
                    if not cursor.gotoNextParagraph(False):
                        break
                vc.gotoRange(cursor, False)
                page = vc.getPage()
            finally:
                vc.gotoRange(saved, False)
                model.unlockControllers()
            return page
        except Exception as e:
            logging.getLogger(__name__).warning("get_page_for_paragraph exception: %s", e)
            return 1

    def get_page_count(self, model):
        """Return page count of a Writer document."""
        try:
            text = model.getText()
            controller = model.getCurrentController()
            vc = controller.getViewCursor()
            saved = text.createTextCursorByRange(vc.getStart())
            model.lockControllers()
            try:
                vc.jumpToLastPage()
                count = vc.getPage()
            finally:
                vc.gotoRange(saved, False)
                model.unlockControllers()
            return count
        except Exception as e:
            logging.getLogger(__name__).warning("get_page_count exception: %s", e)
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
