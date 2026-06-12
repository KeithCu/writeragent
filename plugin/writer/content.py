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
"""Writer content tools — read, apply, find, and paragraph operations."""

import logging

from plugin.framework.tool import ToolBase, ToolBaseDummy
from plugin.framework.constants import APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT
from plugin.doc.document_helpers import normalize_linebreaks, get_string_without_tracked_deletions
from plugin.framework.errors import safe_json_loads
import re as re_mod


log = logging.getLogger("writeragent.writer")

# Cap for replace-all search (_find_all_ranges).
_MAX_SEARCH_REPLACEMENTS = 200

# Non-breaking / exotic spaces -> ASCII space. Length-preserving (each maps to a
# single BMP char) so character offsets into the document text stay valid. NBSP
# (U+00A0) in particular is a common artifact of prior edits and breaks literal
# search when old_content uses a normal space.
#
# Regenerate the inventory table: python3 -c "..."  (see git history / plan doc) or
# run the snippet in the finish-NBSP plan; paste rows here when expanding the map.
#
# | Code   | Name                         | In _SPACE_NORMALIZE_MAP | Follow-up note |
# |--------|------------------------------|-------------------------|----------------|
# | U+0020 | SPACE                        | no                      | target; not mapped |
# | U+00A0 | NO-BREAK SPACE               | yes                     | mapped today |
# | U+1680 | OGHAM SPACE MARK             | no                      | OGHAM SPACE MARK; rare in Writer |
# | U+2000 | EN QUAD                      | yes                     | mapped today |
# | U+2001 | EM QUAD                      | yes                     | mapped today |
# | U+2002 | EN SPACE                     | yes                     | mapped today |
# | U+2003 | EM SPACE                     | yes                     | mapped today |
# | U+2004 | THREE-PER-EM SPACE           | yes                     | mapped today |
# | U+2005 | FOUR-PER-EM SPACE            | yes                     | mapped today |
# | U+2006 | SIX-PER-EM SPACE             | yes                     | mapped today |
# | U+2007 | FIGURE SPACE                 | yes                     | mapped today |
# | U+2008 | PUNCTUATION SPACE            | yes                     | mapped today |
# | U+2009 | THIN SPACE                   | yes                     | mapped today |
# | U+200A | HAIR SPACE                   | yes                     | mapped today |
# | U+202F | NARROW NO-BREAK SPACE        | yes                     | mapped today |
# | U+205F | MEDIUM MATHEMATICAL SPACE    | yes                     | mapped today |
# | U+3000 | IDEOGRAPHIC SPACE            | yes                     | mapped today |
#
# DEVELOPER DISCUSSION / FUTURE WORK (Intentionally deferred to avoid complexity):
#
# - Full-document replace via target='search' + old_content=entire body:
#   DO NOT support this with a body offset fallback. Callers (LLM, tests, translation) must use
#   target='full_document' — no old_content, no search. If we ever revisit, git history has
#   _find_range_by_offset / phase-3 offset scan removed after the LO-regex unification commit.
#
# - Format.py search-replace helpers:
#   Functions like format.find_text_ranges are still LO-literal only. apply_document_content search
#   uses _find_chained_range (LO regex + paragraph chaining) with exotic-space flex matching.
#   Unifying format.find_text_ranges with that stack is deferred — callers are internal/benchmark-only.
#
# - Casefolding & Unicode length changes:
#   Chaining compares via .lower(); LO regex uses SearchCaseSensitive=False. German ß (folds to ss)
#   or Turkish I can still mis-match or mis-size cursor ranges. Fixing that needs character mapping
#   tracking beyond goRight — deferred for rare edge cases.
#
# - Headers/footers:
#   doc.findFirst searches the full document model; we rely on LO for header/footer hits rather than
#   enumerating nested XText containers ourselves.
#
# - Markup apply in nested XText:
#   When inserting HTML/markup inside a table cell, the HTML import helper (replace_single_range_with_content)
#   can sometimes jump the cursor to the end of the document body rather than the cell's end. This is a potential
#   real-world bug if the AI attempts to write rich formatting/math inside cells, but we defer it until we
#   receive actual user bug reports due to the complexity of relative cursor mapping in nested XText.
_SPACE_CODEPOINTS = (
    0x00A0,  # NO-BREAK SPACE
    0x202F,  # NARROW NO-BREAK SPACE
    0x2007,  # FIGURE SPACE
    0x2009,  # THIN SPACE
    # Typographic spaces
    0x2000,  # EN QUAD
    0x2001,  # EM QUAD
    0x2002,  # EN SPACE
    0x2003,  # EM SPACE
    0x2004,  # THREE-PER-EM SPACE
    0x2005,  # FOUR-PER-EM SPACE
    0x2006,  # SIX-PER-EM SPACE
    0x2008,  # PUNCTUATION SPACE
    0x200A,  # HAIR SPACE
    0x205F,  # MEDIUM MATHEMATICAL SPACE
    # CJK space
    0x3000,  # IDEOGRAPHIC SPACE
)
_SPACE_NORMALIZE_MAP = {cp: " " for cp in _SPACE_CODEPOINTS}
# Shared horizontal-space class — must stay aligned with _SPACE_CODEPOINTS.
_HORIZONTAL_SPACE_CLASS = r"[ \t" + "".join("\\u%04x" % cp for cp in _SPACE_CODEPOINTS) + "]"
_HORIZONTAL_SPACE_RE = _HORIZONTAL_SPACE_CLASS + "+"


def _search_try_strings(search_string):
    """Literal search string, then newline-collapsed variant (HTML wrap artifact)."""
    s = search_string or ""
    collapsed = re_mod.sub(r" +", " ", s.replace("\n", " ")).strip()
    for candidate in (s, collapsed):
        if candidate:
            yield candidate


def _escape_for_lo_regex(s):
    """Escape regular expression characters and match any horizontal space sequence."""
    s = (s or "").translate(_SPACE_NORMALIZE_MAP)
    escaped = re_mod.sub(r'([\\^$.|?*+()\[\]{}])', r'\\\1', s)
    return re_mod.sub(r' +', lambda m: _HORIZONTAL_SPACE_CLASS + '+', escaped)


def _compare_normalize(s):
    return normalize_linebreaks(s).translate(_SPACE_NORMALIZE_MAP).strip().lower()


def _paragraph_matches_part(para_text, part, *, head=False, tail=False):
    """Compare a paragraph to a search part; return (ok, offset_len).

    *head*: part must match the start of the paragraph; offset_len is goRight length.
    *tail*: part must match the end; offset_len is goRight start offset from para start.
    Neither flag: full paragraph must equal part (after normalize).
    """
    expected_norm = _compare_normalize(part)
    actual_norm = _compare_normalize(para_text)
    if head:
        if not actual_norm.startswith(expected_norm):
            return False, None
        skipped_leading = len(para_text) - len(para_text.lstrip())
        return True, skipped_leading + len(part.strip())
    if tail:
        if not actual_norm.endswith(expected_norm):
            return False, None
        trimmed_trailing = para_text.rstrip()
        return True, max(0, len(trimmed_trailing) - len(part.strip()))
    return actual_norm == expected_norm, None


def _find_lo_regex_ranges(doc, candidate, all_matches=False):
    """LO regex findFirst/findNext for one candidate string."""
    sd = doc.createSearchDescriptor()
    sd.SearchRegularExpression = True
    sd.SearchString = _escape_for_lo_regex(candidate)

    if not all_matches:
        for case_sens in (True, False):
            sd.SearchCaseSensitive = case_sens
            found = doc.findFirst(sd)
            if found is not None:
                return found
        return None

    ranges = []
    for case_sens in (True, False):
        sd.SearchCaseSensitive = case_sens
        found = doc.findFirst(sd)
        while found is not None:
            if len(ranges) >= _MAX_SEARCH_REPLACEMENTS:
                return ranges
            ranges.append(found)
            found = doc.findNext(found, sd)
        if ranges:
            return ranges
    return ranges


def _find_chained_range(doc, search_string, all_matches=False):
    """Find search_string via LO regex (literal + newline-collapsed retry) then paragraph chaining.

    doc.findFirst covers body, table cells, and text frames. Chaining handles real paragraph
    breaks that LO regex cannot cross.

    Not for whole-document replace: use apply_document_content(target='full_document') instead
    of passing the entire body as old_content (see ApplyDocumentContent docstring).
    """
    if not search_string:
        return [] if all_matches else None

    # Phase 1: whole-string LO regex — literal first, then newline-collapsed (HTML wrap artifact).
    for candidate in _search_try_strings(search_string):
        result = _find_lo_regex_ranges(doc, candidate, all_matches=all_matches)
        if all_matches:
            if result:
                return result
        elif result is not None:
            return result

    # Phase 2: paragraph chaining on the original string (real cross-paragraph intent).
    parts = search_string.split('\n')
    if len(parts) <= 1:
        return [] if all_matches else None

    anchor_idx = -1
    for idx, part in enumerate(parts):
        if part.strip():
            anchor_idx = idx
            break
    if anchor_idx == -1:
        return [] if all_matches else None

    sd = doc.createSearchDescriptor()
    sd.SearchRegularExpression = True
    sd.SearchString = _escape_for_lo_regex(parts[anchor_idx])

    matched_ranges = []

    for case_sens in (True, False):
        sd.SearchCaseSensitive = case_sens
        found = doc.findFirst(sd)
        while found is not None:
            text = found.getText()
            chain_ok = True

            forward_cursor = text.createTextCursorByRange(found)
            forward_cursor.gotoRange(found.getEnd(), False)
            last_end_cursor = None

            for i in range(anchor_idx + 1, len(parts)):
                if not forward_cursor.gotoNextParagraph(False):
                    chain_ok = False
                    break

                check_cursor = text.createTextCursorByRange(forward_cursor)
                check_cursor.gotoEndOfParagraph(True)
                para_text = get_string_without_tracked_deletions(check_cursor)

                is_last = i == len(parts) - 1
                ok, offset_len = _paragraph_matches_part(para_text, parts[i], head=is_last)
                if not ok:
                    chain_ok = False
                    break
                if is_last:
                    last_end_cursor = text.createTextCursorByRange(forward_cursor)
                    last_end_cursor.goRight(offset_len, False)

            if not chain_ok:
                found = doc.findNext(found, sd)
                continue

            backward_cursor = text.createTextCursorByRange(found)
            backward_cursor.gotoRange(found.getStart(), False)
            first_start_cursor = None

            for i in range(anchor_idx - 1, -1, -1):
                if not backward_cursor.gotoPreviousParagraph(False):
                    chain_ok = False
                    break

                check_cursor = text.createTextCursorByRange(backward_cursor)
                check_cursor.gotoEndOfParagraph(True)
                para_text = get_string_without_tracked_deletions(check_cursor)

                is_first = i == 0
                ok, offset_len = _paragraph_matches_part(para_text, parts[i], tail=is_first)
                if not ok:
                    chain_ok = False
                    break
                if is_first:
                    first_start_cursor = text.createTextCursorByRange(backward_cursor)
                    first_start_cursor.goRight(offset_len, False)

            if chain_ok:
                start_range = first_start_cursor.getStart() if first_start_cursor else found.getStart()
                end_range = last_end_cursor.getStart() if last_end_cursor else found.getEnd()

                try:
                    result_range = text.createTextCursorByRange(start_range)
                    result_range.gotoRange(end_range, True)
                    if not all_matches:
                        return result_range
                    matched_ranges.append(result_range)
                    if len(matched_ranges) >= _MAX_SEARCH_REPLACEMENTS:
                        return matched_ranges
                except Exception:
                    log.debug("Failed creating combined XTextRange", exc_info=True)

            found = doc.findNext(found, sd)

        if matched_ranges:
            return matched_ranges

    return matched_ranges if all_matches else None


def _find_first_range(doc, search_string):
    """First match: LO native search with chaining fallback."""
    return _find_chained_range(doc, search_string, all_matches=False)


def _normalize_search_string_for_find(s):
    """Collapse horizontal whitespace (incl. NBSP & friends) to a single ASCII
    space; preserve newlines for literal find.
    """
    return re_mod.sub(_HORIZONTAL_SPACE_RE, " ", s).strip()


def _all_start_indices(haystack, needle):
    """Non-overlapping start indices of *needle* in *haystack*."""
    out = []
    if not needle:
        return out
    i = haystack.find(needle)
    while i >= 0:
        out.append(i)
        i = haystack.find(needle, i + len(needle))
    return out


def _find_all_ranges(doc, search_string):
    """All occurrences as TextRanges in document order (NBSP-aware native search with chaining)."""
    return _find_chained_range(doc, search_string, all_matches=True)


# ------------------------------------------------------------------
# GetDocumentContent
# ------------------------------------------------------------------


class GetDocumentContent(ToolBase):
    """Export the document (or a portion) as formatted content."""

    name = "get_document_content"
    description = "Get document (or selection/range) content. Result includes document_length. scope: full, selection, or range (requires start, end)."
    parameters = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["full", "selection", "range"], "description": ("Return full document (default), current selection/cursor region, or a character range (requires start and end).")},
            "max_chars": {"type": "integer", "description": "Maximum characters to return."},
            "start": {"type": "integer", "description": "Start character offset (0-based). Required for scope 'range'."},
            "end": {"type": "integer", "description": "End character offset (exclusive). Required for scope 'range'."},
            "include_images": {"type": "boolean", "description": "Include embedded image data (base64) in export. Default false."},
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        from . import format as format_support
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        range_start = kwargs.get("start") if scope == "range" else None
        range_end = kwargs.get("end") if scope == "range" else None

        if scope == "range" and (range_start is None or range_end is None):
            return self._tool_error("scope 'range' requires start and end.")

        include_images = bool(kwargs.get("include_images", False))
        content = format_support.document_to_content(
            ctx.doc,
            ctx.ctx,
            ctx.services,
            max_chars=max_chars,
            scope=scope,
            range_start=range_start,
            range_end=range_end,
            include_images=include_images,
        )
        doc_len = ctx.services.document.get_document_length(ctx.doc)
        result = {"status": "ok", "content": content, "length": len(content), "document_length": doc_len}
        if scope == "range" and range_start is not None and range_end is not None:
            result["start"] = int(range_start)
            result["end"] = int(range_end)
        return result


# ------------------------------------------------------------------
# ApplyDocumentContent
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# ApplyDocumentContent
# ------------------------------------------------------------------


class ApplyDocumentContent(ToolBase):
    """Insert or replace content in the document.

    Design notes (important for callers and future maintainers):

    - **Two edit paths**:
      - *Import path* (HTML/markup): for structural rewrites (tables, headings,
        page changes) we prepare HTML in `format_support` and import it via
        ``insertDocumentFromURL``. This is what all of the `insert_*` helpers
        use.
      - *Format‑preserving path* (plain text): for small textual corrections
        we avoid HTML entirely and call `format_support.replace_preserving_format`,
        which mutates characters in place so existing character‑level styling
        (bold, colors, background fills, etc.) is preserved even when the
        replacement text length differs.

    - **Decision rule**: we treat content as *plain text* (and thus eligible
      for format‑preserving replacement) only when `content_has_markup` is
      false. Any obvious HTML/Markdown markers force the import path. This
      keeps the heuristic simple and robust: small literal edits naturally
      stay plain text; rich formatting naturally uses HTML.

    - **Raw vs wrapped content**: `raw_content` is captured *before* any HTML
      wrapping or newline normalization and is passed to the preserving path;
      the (possibly HTML‑wrapped) `content` value is passed to the import path.
      Mixing these up will overwrite document text with serialized HTML rather
      than the intended human‑readable string.

    - **Search** (``target='search'`` only): ``old_content`` must be a **substring** to find —
      a phrase, sentence, or multi-paragraph **block**, not the entire document. To replace
      **all** document content, you **must** use ``target='full_document'`` with ``content`` only;
      **never** pass the full body as ``old_content``. Search uses ``_find_chained_range`` (LO
      regex + paragraph chaining). See ``tests/writer/test_content_search_uno.py``.
    """

    name = "apply_document_content"
    description = (
        "Insert or replace content. "
        f"IMPORTANT: {APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT} "
        "To replace the ENTIRE document use target='full_document' with content only — "
        "do NOT pass the whole document as old_content. "
        "Use target='beginning', 'end', or 'selection' to insert. "
        "Use target='search' with old_content for find-and-replace of a specific substring only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": {"type": "string"}, "description": ("List of HTML fragments or plain-text fragments (one per block); shape and math per system prompt (APPLY_DOCUMENT_CONTENT AND HTML). No Markdown.")},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to apply the content."},
            "old_content": {"type": "string", "description": ("Substring to find when target='search'. Not for whole-document replace — use target='full_document' instead.")},
            "all_matches": {"type": "boolean", "description": "Replace all occurrences (true) or first only. Default false. Only for target='search'."},
        },
        "required": ["content"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from . import format as format_support
        content = kwargs.get("content", "")
        old_content = kwargs.get("old_content")
        target = kwargs.get("target")

        if not target and old_content is not None:
            target = "search"
        if not target:
            return self._tool_error("Provide a target ('beginning', 'end', 'selection', 'full_document', 'search') or old_content for find-and-replace.")

        if target == "search" and old_content is None:
            return self._tool_error("target='search' requires old_content.")

        # Normalize content:
        # - If the model (or caller) serialized a list as a JSON string,
        #   parse it back to a real list first so commas/brackets do not
        #   become literal document text.
        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith("[") and "<" in stripped:
                parsed = safe_json_loads(stripped)
                if isinstance(parsed, list):
                    content = parsed

        # Normalize list input to a single string for HTML import paths.
        if isinstance(content, list):
            _parts = [str(x) for x in content]
            _per_part_nl = [p.count("\n") for p in _parts]
            log.debug(
                "apply_document_content: list join n_parts=%d per_part_newline_counts=%s total_chars_before_join=%d",
                len(_parts),
                _per_part_nl[:20],  # cap log size
                sum(len(p) for p in _parts),
            )
            content = "\n".join(_parts)
            log.debug("apply_document_content: after join newline_count=%d has_math_tag=%s join_preview=%r", content.count("\n"), ("<math" in content.lower()), content[:500])
        # Detect markup BEFORE any HTML wrapping.
        use_preserve = isinstance(content, str) and not format_support.content_has_markup(content)

        if use_preserve and isinstance(content, str):
            _nl_before_esc = content.count("\n")
            content = content.replace("\\n", "\n").replace("\\t", "\t")
            _nl_after_esc = content.count("\n")
            if _nl_after_esc != _nl_before_esc:
                log.debug("apply_document_content: literal \\\\n/\\\\t escape expand (plain text) newline_count %d -> %d", _nl_before_esc, _nl_after_esc)

        raw_content = content

        config_svc = ctx.services.get("config")

        if target == "full_document":
            format_support.replace_full_document(ctx.doc, ctx.ctx, content, config_svc=config_svc)
            return {"status": "ok", "message": "Replaced entire document."}
        if target == "end":
            format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "end", config_svc=config_svc)
            return {"status": "ok", "message": "Inserted content at end."}
        if target == "selection":
            format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "selection", config_svc=config_svc)
            return {"status": "ok", "message": "Inserted content at selection."}
        if target == "beginning":
            format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "beginning", config_svc=config_svc)
            return {"status": "ok", "message": "Inserted content at beginning."}

        # target == "search" from here on — old_content must be a findable substring, not the full body.
        # Whole-document replace: target='full_document' (no search, no old_content).
        old_stripped = str(old_content).strip()

        search_string = old_stripped
        if format_support.content_has_markup(search_string):
            search_string = format_support.html_to_plain_text(search_string, ctx.ctx, config_svc)
        # Collapse exotic horizontal whitespace; preserve newlines for paragraph-aware search.
        search_string = _normalize_search_string_for_find(search_string)
        if not search_string:
            # Parameter error (like old_content=None), not a search no-op: the search never ran,
            # so there's no replaced_count to report — use the standard tool error shape.
            return self._tool_error("old_content is empty after normalization.")
        doc = ctx.doc
        # replaced_count is the machine-readable success signal: 0 -> status "error" (a silent
        # no-op surfaced as a failure), N>0 -> "ok". No matched_count/warning/partial-replace:
        # if a replace raises mid-all_matches the existing abort behavior stands.
        # TODO(follow-up): share search-path return dicts with string_eval_tools.py to avoid drift.
        all_matches = kwargs.get("all_matches", False)
        if all_matches:
            ranges = _find_all_ranges(doc, search_string)
            count = 0
            # Replace from last to first so earlier character offsets stay valid after edits.
            for found in reversed(ranges):
                if use_preserve:
                    format_support.replace_preserving_format(doc, found, raw_content, ctx.ctx)
                else:
                    format_support.replace_single_range_with_content(doc, found, content, ctx.ctx, config_svc)
                count += 1
            if count == 0:
                return {"status": "error",
                        "message": "Replaced 0 occurrence(s). No matches found. Try a shorter substring.",
                        "replaced_count": 0}
            msg = "Replaced %d occurrence(s)." % count
            if use_preserve:
                msg += " (formatting preserved)"
            return {"status": "ok", "message": msg, "replaced_count": count}
        found = _find_first_range(doc, search_string)
        if found is None:
            return {"status": "error", "message": "old_content not found in document. Try a shorter, unique substring.",
                    "replaced_count": 0}
        if use_preserve:
            format_support.replace_preserving_format(doc, found, raw_content, ctx.ctx)
        else:
            format_support.replace_single_range_with_content(doc, found, content, ctx.ctx, config_svc)
        msg = "Replaced 1 occurrence (by old_content)."
        if use_preserve:
            msg += " (formatting preserved)"
        return {"status": "ok", "message": msg, "replaced_count": 1}


# ------------------------------------------------------------------
# CloneHeadingBlock
# ------------------------------------------------------------------


class CloneHeadingBlock(ToolBaseDummy):
    """Clone an entire heading block (heading + all sub-headings + body)."""

    name = "clone_heading_block"
    intent = "edit"
    description = "Clone an entire heading block (heading + all sub-headings + body). The clone is inserted right after the original block."
    parameters = {"type": "object", "properties": {"locator": {"type": "string", "description": ("Locator of the heading to clone (e.g. 'bookmark:_mcp_abc123', 'heading_text:Introduction').")}, "paragraph_index": {"type": "integer", "description": "Paragraph index of the heading (0-based)."}}}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK  # type: ignore

        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return self._tool_error("Provide locator or paragraph_index.")

        # Use writer_tree service to find the heading node and block size
        tree_svc = ctx.services.get("writer_tree")
        if tree_svc is None:
            return self._tool_error("writer_nav module not loaded; cannot resolve heading block.")

        tree = tree_svc.build_heading_tree(ctx.doc)
        node = tree_svc._find_node_by_para_index(tree, para_index)
        if node is None:
            return self._tool_error("No heading found at paragraph %d." % para_index)

        # Total paragraphs in the block: heading + body + all children
        total = 1 + tree_svc._count_all_children(node)

        # Collect elements for the block
        doc_text = ctx.doc.getText()
        enum = doc_text.createEnumeration()
        elements = []
        idx = 0
        while enum.hasMoreElements():
            el = enum.nextElement()
            if para_index <= idx < para_index + total:
                elements.append(el)
            if idx >= para_index + total - 1:
                break
            idx += 1

        if not elements:
            return self._tool_error("Could not collect heading block paragraphs.")

        # Insert duplicates after the last element of the block
        last = elements[-1]
        cursor = doc_text.createTextCursorByRange(last)
        cursor.gotoEndOfParagraph(False)

        for el in elements:
            txt = el.getString()
            sty = el.getPropertyValue("ParaStyleName")
            doc_text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            doc_text.insertString(cursor, txt, False)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            cursor.setPropertyValue("ParaStyleName", sty)
            cursor.gotoEndOfParagraph(False)

        return {"status": "ok", "message": "Cloned heading block '%s' (%d paragraphs)." % (node.get("text", ""), total), "heading_text": node.get("text", ""), "block_size": total}


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _resolve_para_index(ctx, kwargs):
    """Resolve locator or paragraph_index from tool kwargs.

    Returns an integer paragraph index, or None if neither is provided.
    """
    locator = kwargs.get("locator")
    para_index = kwargs.get("paragraph_index")

    if locator is not None and para_index is None:
        doc_svc = ctx.services.document
        resolved = doc_svc.resolve_locator(ctx.doc, locator)
        para_index = resolved.get("para_index")

    return para_index


def _resolve_style_name(doc, style_name):
    """Resolve a style name case-insensitively against the document styles."""
    try:
        families = doc.getStyleFamilies()
        para_styles = families.getByName("ParagraphStyles")
        if para_styles.hasByName(style_name):
            return style_name
        lower = style_name.lower()
        for name in para_styles.getElementNames():
            if name.lower() == lower:
                return name
    except Exception:
        pass
    return style_name


def _count_headings(nodes):
    """Recursively count heading nodes in a nested list."""
    count = 0
    for node in nodes:
        count += 1
        count += _count_headings(node.get("children", []))
    return count


def collect_document_stats(doc, doc_svc):
    """Character/word/paragraph/page/heading counts for a Writer document."""
    from plugin.doc.document_helpers import build_heading_tree

    try:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        full_text = get_string_without_tracked_deletions(cursor)
        char_count = len(full_text)
        word_count = len(full_text.split())
    except Exception:
        char_count = doc_svc.get_document_length(doc)
        word_count = 0

    try:
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        para_count = len(para_ranges)
    except Exception:
        para_count = 0

    try:
        tree = build_heading_tree(doc)
        heading_count = _count_headings(tree.get("children", []))
    except Exception:
        heading_count = 0

    page_count = 0
    try:
        page_count = doc_svc.get_page_count(doc)
    except Exception:
        try:
            vc = doc.getCurrentController().getViewCursor()
            vc.jumpToLastPage()
            page_count = vc.getPage()
        except Exception:
            pass

    return {"character_count": char_count, "word_count": word_count, "paragraph_count": para_count, "page_count": page_count, "heading_count": heading_count}
