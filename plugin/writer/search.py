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
"""Writer search tools: search_in_document, advanced_search."""

import logging

from plugin.framework.tool import ToolBase, ToolBaseDummy
from plugin.doc.document_helpers import get_string_without_tracked_deletions
from . import format as format_support


log = logging.getLogger("writeragent.writer")


def _safe_name(obj):
    """Best-effort name of a UNO object (getName() then .Name); '' if unavailable."""
    if obj is None:
        return ""
    try:
        return obj.getName()
    except Exception:
        try:
            return getattr(obj, "Name", "") or ""
        except Exception:
            return ""


def _prop(obj, name):
    """obj.getPropertyValue(name) or attribute, '' / None-safe."""
    if obj is None:
        return None
    try:
        return obj.getPropertyValue(name)
    except Exception:
        return getattr(obj, name, None)


def _cell_name(cur, text):
    """Cell address ('A1', 'B2') for a match inside a table cell. A Writer cell exposes it via the
    CellProperties 'CellName' property (not XNamed.getName), so try that on the cell text object and
    on the cursor's Cell."""
    for obj in (text, cur, _prop(cur, "Cell")):
        v = _prop(obj, "CellName")
        if v:
            return v
    return _safe_name(_prop(cur, "Cell"))


def _header_footer_label(text_obj, doc=None):
    """"header (page style 'X')" / "footer (...)" when *text_obj* is a page-style header/footer
    text, else None.

    Header/footer text objects are SwXHeadFootText -- findFirst/findNext DOES reach them, but the
    cursor's TextTable/TextFrame properties don't apply there, so their matches were mislabeled
    'body' (verified live 2026-07-02). The page-style walk pins down the region and style name; if
    it fails we still say 'header or footer' rather than misreport 'body'."""
    try:
        if getattr(text_obj, "ImplementationName", "") != "SwXHeadFootText":
            return None
    except Exception:
        return None
    if doc is not None:
        try:
            styles = doc.getStyleFamilies().getByName("PageStyles")
            for i in range(int(styles.getCount())):
                st = styles.getByIndex(i)
                try:
                    if not st.isInUse():
                        continue
                except Exception:
                    pass
                for attr, region in (
                    ("HeaderText", "header"), ("HeaderTextLeft", "header"), ("HeaderTextRight", "header"),
                    ("FooterText", "footer"), ("FooterTextLeft", "footer"), ("FooterTextRight", "footer"),
                ):
                    try:
                        if getattr(st, attr, None) == text_obj:
                            name = _safe_name(st)
                            return "%s (page style '%s')" % (region, name) if name else region
                    except Exception:
                        continue
        except Exception:
            pass
    return "header or footer"


def _describe_match_location(found, doc=None):
    """Where a found range lives: 'body', "table 'X' cell B2", "text box 'Y'", or a header/footer.

    Uses the cursor's TextTable / Cell / TextFrame properties (the same ones the cell-aware edit
    path relies on), then the header/footer check. Fails open to 'body' so a match is never
    dropped over a location quirk."""
    try:
        text = found.getText()
        cur = text.createTextCursorByRange(found.getStart())
    except Exception:
        return "body"
    try:
        tt = cur.getPropertyValue("TextTable")
    except Exception:
        tt = None
    if tt is not None:
        tname = _safe_name(tt)
        cname = _cell_name(cur, text)
        if tname and cname:
            return "table '%s' cell %s" % (tname, cname)
        return "table '%s'" % tname if tname else "a table"
    try:
        tf = cur.getPropertyValue("TextFrame")
    except Exception:
        tf = None
    if tf is not None:
        name = _safe_name(tf)
        return "text box '%s'" % name if name else "a text box"
    hf = _header_footer_label(text, doc)
    if hf:
        return hf
    return "body"


def _enclosing_paragraph_text(found):
    """Text of the paragraph containing the match (context); '' on failure."""
    try:
        text = found.getText()
        cur = text.createTextCursorByRange(found.getStart())
        cur.gotoStartOfParagraph(False)
        cur.gotoEndOfParagraph(True)
        return get_string_without_tracked_deletions(cur)
    except Exception:
        try:
            return found.getString()
        except Exception:
            return ""


def _iter_draw_shapes(container):
    """Yield every shape on a draw-page container, recursing into group shapes.

    ``doc.findAll`` searches the text model (body, cells, frames, and AS_CHARACTER-anchored shape
    text that flows inline) but NOT floating drawing-layer shapes. This walks getDrawPage() so the
    caller can reach text that lives only inside a floating text box / custom shape (note 7)."""
    try:
        n = int(container.getCount())
    except Exception:
        return
    for i in range(n):
        try:
            shape = container.getByIndex(i)
        except Exception:
            continue
        try:
            is_group = shape.supportsService("com.sun.star.drawing.GroupShape")
        except Exception:
            is_group = False
        if is_group:
            yield from _iter_draw_shapes(shape)
        else:
            yield shape


def _shape_is_as_character(shape):
    """True if the shape is anchored AS_CHARACTER — its text already flows inline in the body, so
    the text search covers it. We skip those in the draw-page sweep to avoid reporting them twice."""
    try:
        from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
        return shape.getPropertyValue("AnchorType") == AS_CHARACTER
    except Exception:
        return False


def _shape_is_text_box(shape):
    """True if the shape is a Writer 'text box' (Insert > Text Box) whose text lives in a linked
    text frame. The findFirst/findNext text search already reaches that frame, so we skip the shape
    in the draw sweep to avoid reporting the same text twice (once as frame, once as shape)."""
    try:
        return bool(shape.getPropertyValue("TextBox"))
    except Exception:
        return False


def _shape_text_hits(shape_text, pattern, use_regex, case_sensitive):
    """Matched substrings of *pattern* inside *shape_text* (actual-case), [] if none."""
    if not shape_text or not pattern:
        return []
    if use_regex:
        import re as _re
        flags = 0 if case_sensitive else _re.IGNORECASE
        try:
            return [m.group(0) for m in _re.finditer(pattern, shape_text, flags) if m.group(0)]
        except Exception:
            return []
    hay = shape_text if case_sensitive else shape_text.lower()
    needle = pattern if case_sensitive else pattern.lower()
    hits = []
    step = len(needle) or 1
    idx = hay.find(needle)
    while idx != -1:
        hits.append(shape_text[idx:idx + len(pattern)])
        idx = hay.find(needle, idx + step)  # non-overlapping, matching LO's native text search
    return hits


def _comment_matches(doc, pattern, use_regex, case_sensitive):
    """Yield (hit_text, author, content) for pattern matches inside comment (annotation) fields.

    Comments live in text FIELDS, not in the searchable text model -- findFirst/findNext never
    reaches them (verified live 2026-07-02: a marker inside a comment returned count 0), so text
    that exists only in a comment was invisible to search. Matching reuses _shape_text_hits (plain
    substring / regex over the comment's Content). Fully defensive on any UNO failure."""
    try:
        fields = doc.getTextFields().createEnumeration()
    except Exception:
        return
    while True:
        try:
            if not fields.hasMoreElements():
                return
            field = fields.nextElement()
        except Exception:
            return
        try:
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            content = getattr(field, "Content", "") or ""
            author = (getattr(field, "Author", "") or "").strip()
        except Exception:
            continue
        for hit in _shape_text_hits(content, pattern, use_regex, case_sensitive):
            yield hit, author, content


class SearchInDocument(ToolBase):
    """Search for text anywhere in a document (body, tables, text boxes) and report where."""

    name = "search_in_document"
    description = (
        "Search for text ANYWHERE in the document using LibreOffice native search — body paragraphs "
        "and headings, table cells, text boxes / frames, floating drawing shapes, page headers/footers, "
        "AND comments (annotations) are all covered. Each match reports WHERE it was found (location, "
        "e.g. \"body\", \"table 'Table1' cell B2\", \"text box 'Frame1'\", \"shape 'Shape 1'\", "
        "\"header (page style 'Standard')\", \"comment by 'Ana'\") plus the surrounding paragraph (or "
        "shape/comment text) for context. When pointing the user to a match, quote the first few words "
        "of its text and its location rather than any internal index."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: false)."},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 20)."},
            "return_offsets": {"type": "boolean", "description": ("If true, returns {start, end, text} character offsets instead of located matches. (Regex not supported).")},
        },
        "required": ["pattern"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    tier = "core"

    def execute(self, ctx, **kwargs):

        pattern = kwargs.get("pattern", "")
        if not pattern:
            return self._tool_error("pattern is required.")

        use_regex = kwargs.get("regex", False)
        case_sensitive = kwargs.get("case_sensitive", False)
        max_results = kwargs.get("max_results", 20)
        return_offsets = kwargs.get("return_offsets", False)

        if return_offsets:
            ranges = format_support.find_text_ranges(ctx.doc, ctx.ctx, pattern, start=0, limit=max_results, case_sensitive=case_sensitive)
            return {"status": "ok", "ranges": ranges}

        # Iterate findFirst/findNext (one range at a time) rather than findAll: findAll's bulk
        # SwXTextRanges::Create can SIGABRT-crash LibreOffice on documents that contain floating
        # drawing shapes (observed 2026-07-01 on a real petition). findFirst/findNext covers the
        # same scope — body, table cells, and text boxes / frames — one match at a time, and is
        # exactly what the edit path uses without crashing. For each hit we report WHERE it lives.
        doc = ctx.doc
        try:
            sd = doc.createSearchDescriptor()
            sd.SearchString = pattern
            sd.SearchRegularExpression = bool(use_regex)
            sd.SearchCaseSensitive = bool(case_sensitive)
            found = doc.findFirst(sd)
        except Exception as e:
            return {"status": "error", "error": "search failed: %s" % e}

        matches = []
        total_count = 0
        guard = 0
        while found is not None and guard < 10000:
            guard += 1
            try:
                hit_text = found.getString()
            except Exception:
                hit_text = pattern
            if hit_text == "":
                # Zero-width match (e.g. a zero-width regex): getEnd()==getStart() would make
                # findNext restart in place and spin. Stop rather than emit empty hits / loop.
                break
            total_count += 1
            if len(matches) < max_results:
                matches.append({
                    "text": hit_text,
                    "location": _describe_match_location(found, doc),
                    "context": _enclosing_paragraph_text(found),
                })
            try:
                found = doc.findNext(found.getEnd(), sd)
            except Exception:
                break

        # Draw-layer sweep: floating shapes (text boxes / custom shapes anchored to page or paragraph)
        # are NOT reached by findAll, so text living only inside one is otherwise reported "not found"
        # (note 7). Walk the draw page (recursing into groups) and report those hits with a 'shape'
        # location. AS_CHARACTER shapes are skipped — findAll already covers their inline text.
        # Every hit is COUNTED (count = total matches in the document, like the text search above);
        # only the first max_results are returned in matches.
        if hasattr(doc, "getDrawPage"):
            try:
                draw_shapes = list(_iter_draw_shapes(doc.getDrawPage()))
            except Exception:
                draw_shapes = []
            for shape in draw_shapes:
                try:
                    if _shape_is_as_character(shape) or _shape_is_text_box(shape):
                        continue
                    shape_text = shape.getString() if hasattr(shape, "getString") else ""
                except Exception:
                    continue
                hits = _shape_text_hits(shape_text, pattern, use_regex, case_sensitive)
                if not hits:
                    continue
                sname = (getattr(shape, "Name", "") or "").strip() or "(unnamed shape)"
                for hit in hits:
                    total_count += 1
                    if len(matches) < max_results:
                        matches.append({
                            "text": hit,
                            "location": "shape '%s'" % sname,
                            "context": shape_text.strip(),
                        })

        # Comment (annotation) sweep: comments are text fields outside the searchable text model,
        # so the text search above never sees them. Report matches with the comment's author and
        # the full comment text as context. Same counting contract as above.
        for hit, author, content in _comment_matches(doc, pattern, use_regex, case_sensitive):
            total_count += 1
            if len(matches) < max_results:
                matches.append({
                    "text": hit,
                    "location": "comment by '%s'" % (author or "unknown"),
                    "context": content.strip(),
                })

        # Zero hits with regex=true: a malformed pattern finds nothing SILENTLY (LibreOffice's
        # SearchDescriptor does not raise, and the Python re sweeps swallow compile errors), which
        # is indistinguishable from "text not in document". Diagnose it: if the pattern does not
        # even compile, say so instead of reporting a clean miss. (A pattern that found something
        # never reaches this check, so ICU-only syntax that Python cannot parse is not rejected.)
        if total_count == 0 and use_regex:
            import re as _re

            try:
                _re.compile(pattern)
            except _re.error as rex:
                return {
                    "status": "error",
                    "code": "INVALID_REGEX",
                    "message": ("0 matches, and the pattern does not parse as a regular expression "
                                "(%s). Fix the pattern, or set regex=false for a literal search." % rex),
                    "count": 0,
                }
        return {"status": "ok", "matches": matches, "count": total_count, "returned": len(matches)}


class AdvancedSearch(ToolBaseDummy):
    name = "advanced_search"
    intent = "navigate"
    description = "Full-text search with Snowball stemming. Supports boolean queries: AND (default), OR, NOT, NEAR/N. Language auto-detected from document locale. Returns matching paragraphs with context and nearest heading bookmark. Use around_page to restrict results near a specific page."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": ("Search query. Examples: 'climate change', 'energy AND renewable', 'solar OR wind', 'climate NOT politics', 'ocean NEAR/3 warming'")},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 20)"},
            "context_paragraphs": {"type": "integer", "description": "Paragraphs of context around each match (default: 1)"},
            "around_page": {"type": "integer", "description": ("Restrict results to paragraphs near this page (optional). Enables page numbers in results.")},
            "page_radius": {"type": "integer", "description": ("Page radius for around_page filter (default: 1, meaning +/-1 page)")},
            "include_pages": {"type": "boolean", "description": ("Add page numbers to results. Automatic when around_page is set. (default: false)")},
        },
        "required": ["query"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        idx_svc = ctx.services.writer_index
        around_page = kwargs.get("around_page")
        page_radius = kwargs.get("page_radius", 1)
        include_pages = kwargs.get("include_pages", False)

        if around_page is not None:
            include_pages = True

        try:
            result = idx_svc.search_boolean(ctx.doc, kwargs["query"], max_results=kwargs.get("max_results", 20), context_paragraphs=kwargs.get("context_paragraphs", 1))
        except ValueError as e:
            return self._tool_error(str(e))

        # Post-process: add page numbers and filter by page proximity
        if include_pages and result.get("matches"):
            page_map = _build_page_map(ctx.doc)
            for m in result["matches"]:
                pi = m.get("paragraph_index")
                if pi is not None and pi in page_map:
                    m["page"] = page_map[pi]

            if around_page is not None:
                lo = around_page - page_radius
                hi = around_page + page_radius
                before_count = len(result["matches"])
                result["matches"] = [m for m in result["matches"] if lo <= m.get("page", 0) <= hi]
                result["returned"] = len(result["matches"])
                result["filtered_by_page"] = {"around_page": around_page, "page_radius": page_radius, "before_filter": before_count}

        return {"status": "ok", **result}


# Page map cache (cleared on doc change)
_page_map_cache: dict[str | int, dict[int, int]] = {}


def _build_page_map(doc):
    """Map paragraph indices to page numbers using view cursor."""
    doc_url = doc.getURL() or id(doc)
    if doc_url in _page_map_cache:
        return _page_map_cache[doc_url]

    page_map = {}
    try:
        controller = doc.getCurrentController()
        vc = controller.getViewCursor()
        saved = None
        try:
            saved = doc.getText().createTextCursorByRange(vc.getStart())
        except Exception:
            pass

        doc.lockControllers()
        try:
            text = doc.getText()
            enum = text.createEnumeration()
            idx = 0
            while enum.hasMoreElements():
                para = enum.nextElement()
                try:
                    vc.gotoRange(para.getStart(), False)
                    page_map[idx] = vc.getPage()
                except Exception:
                    pass
                idx += 1
        finally:
            if saved is not None:
                vc.gotoRange(saved, False)
            doc.unlockControllers()
    except Exception:
        pass

    _page_map_cache[doc_url] = page_map
    return page_map


class GetIndexStats(ToolBaseDummy):
    # Niche stem-index diagnostic; not exposed to LLM/MCP. Re-enable with ToolBase when needed.
    name = "get_index_stats"
    intent = "navigate"
    description = "Get search index statistics: paragraph count, unique stems, language, build time, and top 20 most frequent stems."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        idx_svc = ctx.services.writer_index
        result = idx_svc.get_index_stats(ctx.doc)
        return {"status": "ok", **result}
