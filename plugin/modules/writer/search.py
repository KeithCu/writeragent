"""Writer search tools: search_in_document, replace_in_document."""

import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger("writeragent.writer")


class SearchInDocument(ToolBase):
    """Search for text in a document with paragraph context."""

    name = "search_in_document"
    description = (
        "Search for text in the document using LibreOffice native search. "
        "Returns matches with surrounding paragraph text for context."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Search string or regex pattern.",
            },
            "regex": {
                "type": "boolean",
                "description": "Use regular expression (default: false).",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive search (default: false).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default: 20).",
            },
            "context_paragraphs": {
                "type": "integer",
                "description": (
                    "Number of paragraphs of context around each match "
                    "(default: 1)."
                ),
            },
            "return_offsets": {
                "type": "boolean",
                "description": (
                    "If true, returns {start, end, text} character offsets "
                    "instead of paragraph context. (Regex not supported)."
                ),
            },
        },
        "required": ["pattern"],
    }
    doc_types = ["writer"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        import re as re_mod

        pattern = kwargs.get("pattern", "")
        if not pattern:
            return {"status": "error", "message": "pattern is required."}

        use_regex = kwargs.get("regex", False)
        case_sensitive = kwargs.get("case_sensitive", False)
        max_results = kwargs.get("max_results", 20)
        context_paragraphs = kwargs.get("context_paragraphs", 1)
        return_offsets = kwargs.get("return_offsets", False)

        if return_offsets:
            from plugin.modules.writer import format_support
            ranges = format_support.find_text_ranges(
                ctx.doc, ctx.ctx, pattern,
                start=0, limit=max_results, case_sensitive=case_sensitive,
            )
            return {"status": "ok", "ranges": ranges}

        doc = ctx.doc
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        para_count = len(para_ranges)

        try:
            # Read paragraph texts once
            para_texts = []
            for para in para_ranges:
                try:
                    if para.supportsService(
                        "com.sun.star.text.Paragraph"
                    ):
                        para_texts.append(para.getString())
                    else:
                        para_texts.append("")
                except Exception:
                    para_texts.append("")

            # Compile regex if needed
            if use_regex:
                flags = 0 if case_sensitive else re_mod.IGNORECASE
                try:
                    compiled = re_mod.compile(pattern, flags)
                except re_mod.error as e:
                    return {
                        "status": "error",
                        "error": "Invalid regex: %s" % e,
                    }

            # Search within paragraphs
            matches = []
            total_count = 0

            for i, ptext in enumerate(para_texts):
                if not ptext:
                    continue

                if use_regex:
                    for m in compiled.finditer(ptext):
                        total_count += 1
                        if len(matches) < max_results:
                            matches.append(
                                _build_match(
                                    m.group(), i,
                                    context_paragraphs, para_count,
                                    para_texts,
                                )
                            )
                else:
                    haystack = ptext if case_sensitive else ptext.lower()
                    needle = (
                        pattern if case_sensitive else pattern.lower()
                    )
                    step = max(1, len(needle))
                    pos = 0
                    while True:
                        pos = haystack.find(needle, pos)
                        if pos == -1:
                            break
                        total_count += 1
                        if len(matches) < max_results:
                            matches.append(
                                _build_match(
                                    ptext[pos:pos + len(pattern)], i,
                                    context_paragraphs, para_count,
                                    para_texts,
                                )
                            )
                        pos += step

            return {
                "status": "ok",
                "matches": matches,
                "count": total_count,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


def _build_match(text, para_idx, ctx_paras, para_count, para_texts):
    """Build a single match result with context paragraphs."""
    ctx_lo = max(0, para_idx - ctx_paras)
    ctx_hi = min(para_count, para_idx + ctx_paras + 1)
    context = [
        {"index": j, "text": para_texts[j]}
        for j in range(ctx_lo, ctx_hi)
    ]
    return {
        "text": text,
        "paragraph_index": para_idx,
        "context": context,
    }


class ReplaceInDocument(ToolBase):
    """Find and replace text preserving formatting."""

    name = "replace_in_document"
    description = (
        "Find and replace text in the document with regex support. "
        "Preserves existing formatting. Returns count of replacements."
    )
    parameters = {
        "type": "object",
        "properties": {
            "search": {
                "type": "string",
                "description": "Text or regex pattern to find.",
            },
            "replace": {
                "type": "string",
                "description": "Replacement text.",
            },
            "regex": {
                "type": "boolean",
                "description": "Use regular expression (default: false).",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive matching (default: false).",
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "Replace all occurrences (default: true). "
                    "Set to false to replace only the first match."
                ),
            },
        },
        "required": ["search", "replace"],
    }
    doc_types = ["writer"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        search = kwargs.get("search", "")
        replace = kwargs.get("replace", "")
        if not search:
            return {"status": "error", "message": "search is required."}

        regex = kwargs.get("regex", False)
        case_sensitive = kwargs.get("case_sensitive", False)
        replace_all = kwargs.get("replace_all", True)

        doc = ctx.doc

        try:
            replace_desc = doc.createReplaceDescriptor()
            replace_desc.SearchString = search
            replace_desc.ReplaceString = replace
            replace_desc.SearchRegularExpression = bool(regex)
            replace_desc.SearchCaseSensitive = bool(case_sensitive)

            if replace_all:
                count = doc.replaceAll(replace_desc)
            else:
                # Replace only the first match
                found = doc.findFirst(replace_desc)
                if found is not None:
                    found.setString(replace)
                    count = 1
                else:
                    count = 0

            # Invalidate document cache after edits
            if count > 0:
                doc_svc = ctx.services.document
                doc_svc.invalidate_cache(doc)

            return {
                "status": "ok",
                "replacements": count,
                "search": search,
                "replace": replace,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


class AdvancedSearch(ToolBase):
    name = "advanced_search"
    intent = "navigate"
    description = (
        "Full-text search with Snowball stemming. Supports boolean queries: "
        "AND (default), OR, NOT, NEAR/N. "
        "Language auto-detected from document locale. "
        "Returns matching paragraphs with context and nearest heading bookmark. "
        "Use around_page to restrict results near a specific page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query. Examples: 'climate change', "
                    "'energy AND renewable', 'solar OR wind', "
                    "'climate NOT politics', 'ocean NEAR/3 warming'"
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default: 20)",
            },
            "context_paragraphs": {
                "type": "integer",
                "description": "Paragraphs of context around each match (default: 1)",
            },
            "around_page": {
                "type": "integer",
                "description": (
                    "Restrict results to paragraphs near this page "
                    "(optional). Enables page numbers in results."
                ),
            },
            "page_radius": {
                "type": "integer",
                "description": (
                    "Page radius for around_page filter "
                    "(default: 1, meaning +/-1 page)"
                ),
            },
            "include_pages": {
                "type": "boolean",
                "description": (
                    "Add page numbers to results. "
                    "Automatic when around_page is set. "
                    "(default: false)"
                ),
            },
        },
        "required": ["query"],
    }
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        idx_svc = ctx.services.writer_index
        around_page = kwargs.get("around_page")
        page_radius = kwargs.get("page_radius", 1)
        include_pages = kwargs.get("include_pages", False)

        if around_page is not None:
            include_pages = True

        try:
            result = idx_svc.search_boolean(
                ctx.doc,
                kwargs["query"],
                max_results=kwargs.get("max_results", 20),
                context_paragraphs=kwargs.get("context_paragraphs", 1),
            )
        except ValueError as e:
            return {"status": "error", "error": str(e)}

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
                result["matches"] = [
                    m for m in result["matches"]
                    if lo <= m.get("page", 0) <= hi
                ]
                result["returned"] = len(result["matches"])
                result["filtered_by_page"] = {
                    "around_page": around_page,
                    "page_radius": page_radius,
                    "before_filter": before_count,
                }

        return {"status": "ok", **result}


# Page map cache (cleared on doc change)
_page_map_cache = {}


def _build_page_map(doc):
    """Map paragraph indices to page numbers using view cursor."""
    doc_url = doc.getURL() or id(doc)
    if doc_url in _page_map_cache:
        return _page_map_cache[doc_url]

    page_map = {}
    try:
        controller = doc.getCurrentController()
        vc = controller.getViewCursor()
        saved = doc.getText().createTextCursorByRange(vc.getStart())
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
            vc.gotoRange(saved, False)
            doc.unlockControllers()
    except Exception:
        pass

    _page_map_cache[doc_url] = page_map
    return page_map


class GetIndexStats(ToolBase):
    name = "get_index_stats"
    intent = "navigate"
    description = (
        "Get search index statistics: paragraph count, unique stems, "
        "language, build time, and top 20 most frequent stems."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        idx_svc = ctx.services.writer_index
        result = idx_svc.get_index_stats(ctx.doc)
        return {"status": "ok", **result}
