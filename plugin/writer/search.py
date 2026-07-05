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
"""Writer search: search_in_document tool and shared text-find helpers for edit/dry_run paths."""

import logging
import re as re_mod
from typing import Any, Literal, overload

from plugin.doc.document_helpers import get_string_without_tracked_deletions
from plugin.framework.tool import ToolBase, ToolBaseDummy
from . import format as format_support


log = logging.getLogger("writeragent.writer")

_MAX_SEARCH_REPLACEMENTS = 200

# Horizontal-space class aligned with content.py exotic-space map.
_SPACE_CODEPOINTS = (
    0x00A0, 0x202F, 0x2007, 0x2009, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004,
    0x2005, 0x2006, 0x2008, 0x200A, 0x205F, 0x3000,
)
_HORIZONTAL_SPACE_CLASS = r"[ \t" + "".join("\\u%04x" % cp for cp in _SPACE_CODEPOINTS) + "]"
_HORIZONTAL_SPACE_RE = _HORIZONTAL_SPACE_CLASS + "+"


def find_next_after_match(doc, found, sd):
    """Resume LO search after *found* — use getEnd() so the next hit starts after the match."""
    return doc.findNext(found.getEnd(), sd)


def validate_regex_pattern(pattern):
    """Return an error message when *pattern* is not valid Python regex, else None."""
    try:
        re_mod.compile(pattern)
    except re_mod.error as rex:
        return str(rex)
    return None


def invalid_regex_tool_message(rex_msg):
    """Standard INVALID_REGEX message for tools."""
    return (
        "0 matches, and the pattern does not parse as a regular expression "
        "(%s). Fix the pattern, or set regex=false for a literal search." % rex_msg
    )


def normalize_search_string_for_find(s):
    """Collapse horizontal whitespace (incl. NBSP); preserve newlines for literal find."""
    return re_mod.sub(_HORIZONTAL_SPACE_RE, " ", s).strip()


def _safe_name(obj):
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
    if obj is None:
        return None
    try:
        return obj.getPropertyValue(name)
    except Exception:
        return getattr(obj, name, None)


def _cell_name(cur, text):
    for obj in (text, cur, _prop(cur, "Cell")):
        v = _prop(obj, "CellName")
        if v:
            return v
    return _safe_name(_prop(cur, "Cell"))


def _header_footer_label(text_obj, doc=None, label_cache=None):
    try:
        if getattr(text_obj, "ImplementationName", "") != "SwXHeadFootText":
            return None
    except Exception:
        return None
    if label_cache is not None:
        key = id(text_obj)
        if key in label_cache:
            return label_cache[key]
    result = None
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
                            result = "%s (page style '%s')" % (region, name) if name else region
                            break
                    except Exception:
                        continue
                if result:
                    break
        except Exception:
            pass
    if result is None:
        result = "header or footer"
    if label_cache is not None:
        label_cache[id(text_obj)] = result
    return result


def describe_match_location(found, doc=None, label_cache=None):
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
    hf = _header_footer_label(text, doc, label_cache=label_cache)
    if hf:
        return hf
    return "body"


def enclosing_paragraph_text(found):
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


def iter_draw_shapes(container):
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
            yield from iter_draw_shapes(shape)
        else:
            yield shape


def shape_is_as_character(shape):
    try:
        from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
        return shape.getPropertyValue("AnchorType") == AS_CHARACTER
    except Exception:
        return False


def shape_is_text_box(shape):
    try:
        return bool(shape.getPropertyValue("TextBox"))
    except Exception:
        return False


def shape_text_hits(shape_text, pattern, use_regex, case_sensitive):
    if not shape_text or not pattern:
        return []
    if use_regex:
        flags = 0 if case_sensitive else re_mod.IGNORECASE
        try:
            return [m.group(0) for m in re_mod.finditer(pattern, shape_text, flags) if m.group(0)]
        except Exception:
            return []
    hay = shape_text if case_sensitive else shape_text.lower()
    needle = pattern if case_sensitive else pattern.lower()
    hits = []
    step = len(needle) or 1
    idx = hay.find(needle)
    while idx != -1:
        hits.append(shape_text[idx:idx + len(pattern)])
        idx = hay.find(needle, idx + step)
    return hits


def comment_matches(doc, pattern, use_regex, case_sensitive):
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
        for hit in shape_text_hits(content, pattern, use_regex, case_sensitive):
            yield hit, author, content


@overload
def find_ranges_regex_case(doc, pattern: str, use_regex: bool, case_sensitive: bool, all_matches: Literal[False]) -> Any: ...


@overload
def find_ranges_regex_case(doc, pattern: str, use_regex: bool, case_sensitive: bool, all_matches: Literal[True]) -> list[Any]: ...


def find_ranges_regex_case(doc, pattern, use_regex, case_sensitive, all_matches):
    if use_regex:
        err = validate_regex_pattern(pattern)
        if err:
            raise ValueError(invalid_regex_tool_message(err))
    sd = doc.createSearchDescriptor()
    sd.SearchString = pattern
    sd.SearchRegularExpression = bool(use_regex)
    sd.SearchCaseSensitive = bool(case_sensitive)
    if not all_matches:
        return doc.findFirst(sd)
    out = []
    found = doc.findFirst(sd)
    while found is not None and len(out) < _MAX_SEARCH_REPLACEMENTS:
        out.append(found)
        found = find_next_after_match(doc, found, sd)
    return out


def drawing_shape_object_containing(doc, search_string, *, use_regex=False, case_sensitive=False):
    pattern = (search_string or "").strip()
    if not pattern:
        return None
    try:
        if not hasattr(doc, "getDrawPage"):
            return None
        draw_page = doc.getDrawPage()
    except Exception:
        return None
    for shape in iter_draw_shapes(draw_page):
        try:
            if shape_is_as_character(shape) or shape_is_text_box(shape):
                continue
            text = shape.getString() if hasattr(shape, "getString") else ""
        except Exception:
            continue
        if shape_text_hits(text, pattern, use_regex, case_sensitive):
            return shape
    return None


def drawing_shape_containing(doc, search_string, *, use_regex=False, case_sensitive=False):
    shape = drawing_shape_object_containing(
        doc, search_string, use_regex=use_regex, case_sensitive=case_sensitive)
    if shape is None:
        return None
    return (getattr(shape, "Name", "") or "").strip() or "(unnamed shape)"


def all_start_indices(haystack, needle):
    """Non-overlapping start indices of *needle* in *haystack*."""
    out = []
    if not needle:
        return out
    i = haystack.find(needle)
    while i >= 0:
        out.append(i)
        i = haystack.find(needle, i + len(needle))
    return out


def sweep_draw_shape_preview_matches(doc, pattern, use_regex, case_sensitive, limit=20):
    """Edit-reachable draw-layer hits for dry_run (same skip rules as search sweep)."""
    matches = []
    if not hasattr(doc, "getDrawPage"):
        return matches
    try:
        draw_page = doc.getDrawPage()
    except Exception:
        return matches
    for shape in iter_draw_shapes(draw_page):
        try:
            if shape_is_as_character(shape) or shape_is_text_box(shape):
                continue
            shape_text = shape.getString() if hasattr(shape, "getString") else ""
        except Exception:
            continue
        hits = shape_text_hits(shape_text, pattern, use_regex, case_sensitive)
        if not hits:
            continue
        sname = (getattr(shape, "Name", "") or "").strip() or "(unnamed shape)"
        for hit in hits:
            matches.append({
                "text": hit,
                "location": "shape '%s'" % sname,
                "context": shape_text.strip(),
            })
            if len(matches) >= limit:
                return matches
    return matches


def sweep_comment_preview_matches(doc, pattern, use_regex, case_sensitive, limit=20):
    matches = []
    for hit, author, content in comment_matches(doc, pattern, use_regex, case_sensitive):
        matches.append({
            "text": hit,
            "location": "comment by '%s'" % (author or "unknown"),
            "context": content.strip(),
        })
        if len(matches) >= limit:
            break
    return matches


# Underscore aliases for tracking.py, comments.py, and older imports.
_describe_match_location = describe_match_location
_enclosing_paragraph_text = enclosing_paragraph_text
_comment_matches = comment_matches
_iter_draw_shapes = iter_draw_shapes
_shape_text_hits = shape_text_hits


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
        "of its text and its location rather than any internal index. "
        "With regex=true, body/table/frame hits use LibreOffice/ICU regex; shape and comment sweeps "
        "use Python re (same INVALID_REGEX check). return_offsets is body-only literal search."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: false)."},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 20)."},
            "return_offsets": {"type": "boolean", "description": (
                "If true, returns {start, end, text} character offsets for body text only "
                "(no regex, no shapes/comments). Default: false.")},
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

        if return_offsets and use_regex:
            return self._tool_error(
                "return_offsets does not support regex=true; use regex=false or omit return_offsets.",
                code="INVALID_PARAM")
        if return_offsets:
            ranges = format_support.find_text_ranges(
                ctx.doc, ctx.ctx, pattern, start=0, limit=max_results, case_sensitive=case_sensitive)
            return {"status": "ok", "ranges": ranges}

        if use_regex:
            rex_err = validate_regex_pattern(pattern)
            if rex_err:
                return self._tool_error(invalid_regex_tool_message(rex_err), code="INVALID_REGEX", count=0)

        doc = ctx.doc
        label_cache = {}
        truncated = False
        try:
            sd = doc.createSearchDescriptor()
            sd.SearchString = pattern
            sd.SearchRegularExpression = bool(use_regex)
            sd.SearchCaseSensitive = bool(case_sensitive)
            found = doc.findFirst(sd)
        except Exception:
            log.exception("search_in_document: createSearchDescriptor / findFirst failed")
            return self._tool_error("search failed.", code="SEARCH_FAILED")

        matches = []
        total_count = 0
        guard = 0
        find_next_failed = False
        while found is not None and guard < 10000:
            guard += 1
            try:
                hit_text = found.getString()
            except Exception:
                hit_text = ""
            if hit_text == "":
                break
            total_count += 1
            if len(matches) < max_results:
                matches.append({
                    "text": hit_text,
                    "location": describe_match_location(found, doc, label_cache=label_cache),
                    "context": enclosing_paragraph_text(found),
                })
            try:
                found = find_next_after_match(doc, found, sd)
            except Exception:
                log.exception("search_in_document: findNext failed")
                find_next_failed = True
                break
        if guard >= 10000:
            truncated = True

        if hasattr(doc, "getDrawPage"):
            try:
                for shape in iter_draw_shapes(doc.getDrawPage()):
                    try:
                        if shape_is_as_character(shape) or shape_is_text_box(shape):
                            continue
                        shape_text = shape.getString() if hasattr(shape, "getString") else ""
                    except Exception:
                        continue
                    hits = shape_text_hits(shape_text, pattern, use_regex, case_sensitive)
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
            except Exception:
                log.debug("search_in_document: draw-page sweep failed", exc_info=True)

        for hit, author, content in comment_matches(doc, pattern, use_regex, case_sensitive):
            total_count += 1
            if len(matches) < max_results:
                matches.append({
                    "text": hit,
                    "location": "comment by '%s'" % (author or "unknown"),
                    "context": content.strip(),
                })

        if total_count == 0 and use_regex:
            rex_err = validate_regex_pattern(pattern)
            if rex_err:
                return self._tool_error(invalid_regex_tool_message(rex_err), code="INVALID_REGEX", count=0)

        result = {"status": "ok", "matches": matches, "count": total_count, "returned": len(matches)}
        if truncated or find_next_failed:
            result["truncated"] = True
            if truncated:
                result["warning"] = "Search stopped after 10000 text-model matches; count may be incomplete."
            elif find_next_failed:
                result["warning"] = "Text-model search ended early after a findNext failure; count may be incomplete."
        return result


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


_page_map_cache: dict[str | int, dict[int, int]] = {}


def _build_page_map(doc):
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
    name = "get_index_stats"
    intent = "navigate"
    description = "Get search index statistics: paragraph count, unique stems, language, build time, and top 20 most frequent stems."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        idx_svc = ctx.services.writer_index
        result = idx_svc.get_index_stats(ctx.doc)
        return {"status": "ok", **result}
