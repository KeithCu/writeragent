# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared paragraph text search helpers for document research grep and document analysis."""

from __future__ import annotations

import logging
import re as re_mod
from typing import Any

from plugin.framework.errors import UnoObjectError, safe_call


def build_paragraph_match(text: str, para_idx: int, ctx_paras: int, para_count: int, para_texts: list[str]) -> dict[str, Any]:
    """Build a single match result with context paragraphs."""
    ctx_lo = max(0, para_idx - ctx_paras)
    ctx_hi = min(para_count, para_idx + ctx_paras + 1)
    context = [{"index": j, "text": para_texts[j]} for j in range(ctx_lo, ctx_hi)]
    return {"text": text, "paragraph_index": para_idx, "context": context}


def search_paragraph_texts(
    pattern: str,
    para_texts: list[str],
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 20,
    context_paragraphs: int = 1,
    stop_checker: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    """Search *para_texts* for *pattern*; return (matches up to max_results, total_count)."""
    if not pattern:
        return [], 0

    para_count = len(para_texts)
    compiled = None
    if regex:
        flags = 0 if case_sensitive else re_mod.IGNORECASE
        try:
            compiled = re_mod.compile(pattern, flags)
        except re_mod.error as e:
            raise ValueError(f"Invalid regex: {e}") from e

    matches: list[dict[str, Any]] = []
    total_count = 0

    for i, ptext in enumerate(para_texts):
        if stop_checker and stop_checker():
            break
        if not ptext:
            continue

        if regex and compiled is not None:
            for m in compiled.finditer(ptext):
                total_count += 1
                if len(matches) < max_results:
                    matches.append(build_paragraph_match(m.group(), i, context_paragraphs, para_count, para_texts))
        else:
            haystack = ptext if case_sensitive else ptext.lower()
            needle = pattern if case_sensitive else pattern.lower()
            step = max(1, len(needle))
            pos = 0
            while True:
                pos = haystack.find(needle, pos)
                if pos == -1:
                    break
                total_count += 1
                if len(matches) < max_results:
                    matches.append(build_paragraph_match(ptext[pos : pos + len(pattern)], i, context_paragraphs, para_count, para_texts))
                pos += step

    return matches, total_count


# Same service string as text_helpers.build_heading_tree and document_research_grep.
_PARAGRAPH_SERVICE = "com.sun.star.text.Paragraph"


def _is_text_paragraph(element: Any) -> bool:
    """True when *element* is an ``XTextRange`` paragraph.

    ``XText.createEnumeration`` also yields ``SwXTextTable``. Tables do not
    support this service and are not ``XTextRange`` (no ``getStart`` /
    ``getEnd``). Stand-ins without ``supportsService`` count when they expose
    ``getStart`` so unit tests can pass plain range objects.
    """
    supports = getattr(element, "supportsService", None)
    if callable(supports):
        try:
            return bool(supports(_PARAGRAPH_SERVICE))
        except Exception:
            return callable(getattr(element, "getStart", None))
    return callable(getattr(element, "getStart", None))


def get_paragraph_ranges(model: Any) -> list[Any]:
    """Return top-level text-enumeration elements, including tables.

    Tables stay in the list. Heading trees, bookmarks, and grep treat the
    enumeration index as the paragraph index and count every element, so
    dropping tables here would shift every later index. Range lookup skips
    non-paragraphs in :func:`find_paragraph_for_range`.
    """
    text = model.getText()
    enum = text.createEnumeration()
    ranges = []
    while enum.hasMoreElements():
        ranges.append(enum.nextElement())
    return ranges


def _anchor_contains_point(text_obj: Any, match_start: Any, element: Any) -> bool:
    """True when *element*'s anchor contains *match_start*.

    Used only for non-paragraph slots (tables). The element itself is not an
    ``XTextRange``; its anchor is.
    """
    get_anchor = getattr(element, "getAnchor", None)
    if not callable(get_anchor):
        return False
    try:
        anchor = get_anchor()
        # getattr: the anchor is an untyped UNO object. Direct getStart/getEnd
        # access is an unknown attribute to the type checker.
        get_start = getattr(anchor, "getStart", None)
        get_end = getattr(anchor, "getEnd", None)
        if not callable(get_start) or not callable(get_end):
            return False
        # compareRegionStarts: 1 if the first starts before the second, 0 if equal, -1 if after.
        cmp_start = text_obj.compareRegionStarts(match_start, get_start())
        if cmp_start > 0:
            return False
        cmp_end = text_obj.compareRegionStarts(match_start, get_end())
    except Exception:
        return False
    return cmp_end >= 0


def _index_outside_paragraphs(
    text_obj: Any,
    match_start: Any,
    para_ranges: list[Any],
    entries: list[tuple[int, Any]],
    low: int,
) -> int:
    """Map a point that is not inside any paragraph onto the table in that gap.

    A table anchor starts after the previous paragraph's end and before the
    next paragraph's start, so it never falls inside a paragraph. The slot
    between those paragraphs is the table's enumeration index. Returning 0
    would report every such anchor as the first paragraph.
    """
    if not para_ranges:
        return 0
    prev_enum = entries[low - 1][0] if entries and low > 0 else -1
    next_enum = entries[low][0] if low < len(entries) else len(para_ranges)
    slots = list(range(prev_enum + 1, next_enum))
    if len(slots) == 1:
        return slots[0]
    for slot in slots:
        if _anchor_contains_point(text_obj, match_start, para_ranges[slot]):
            return slot
    return 0


def find_paragraph_for_range(match_range: Any, para_ranges: list[Any], text_obj: Any = None) -> int:
    # text_obj is Any (not Any | None): None is a valid default, but typing it
    # optional made basedpyright treat compareRegionStarts as optional access.
    """Return the enumeration index of the element that contains *match_range*.

    Binary search calls ``getStart`` / ``getEnd`` only on
    ``com.sun.star.text.Paragraph`` elements. ``SwXTextTable`` is
    not an ``XTextRange``. The attribute lookup happens before
    ``safe_call``, so it raises ``AttributeError`` (callers that
    catch ``Exception`` then drop the index, often to 0). The
    returned index is still the full-enumeration index (tables
    keep their slots). A point in the gap between paragraphs maps
    to that slot.
    """
    try:
        if text_obj is None:
            text_obj = safe_call(match_range.getText, "Get text object")
        match_start = safe_call(match_range.getStart, "Get match start")
        entries = [(idx, para) for idx, para in enumerate(para_ranges) if _is_text_paragraph(para)]
        low = 0
        high = len(entries) - 1

        while low <= high:
            mid = (low + high) // 2
            idx, para = entries[mid]
            # compareRegionStarts: -1 if first is after second, 1 if before, 0 if equal
            para_start = safe_call(para.getStart, "Get para start")
            cmp_start = safe_call(text_obj.compareRegionStarts, "compareRegionStarts start", match_start, para_start)
            if cmp_start > 0:
                high = mid - 1
            else:
                para_end = safe_call(para.getEnd, "Get para end")
                cmp_end = safe_call(text_obj.compareRegionStarts, "compareRegionStarts end", match_start, para_end)
                if cmp_end < 0:
                    low = mid + 1
                else:
                    return idx
        return _index_outside_paragraphs(text_obj, match_start, para_ranges, entries, low)
    except UnoObjectError:
        logging.getLogger(__name__).exception("find_paragraph_for_range error")
    # Callers that only need a best-effort index (images, comments) still get 0.
    # Bookmark locators must not: 0 is also the first paragraph. They call
    # confirm_paragraph_index before navigating or mutating.
    return 0


def confirm_paragraph_index(text_obj: Any, anchor: Any, para_ranges: list[Any], para_idx: Any) -> int | None:
    """Return *para_idx* when it is a real hit, or None for the unplaced fallback.

    ``find_paragraph_for_range`` returns 0 both when the anchor
    sits in the first paragraph and when the anchor cannot be
    placed (disposed range, compare failure, or a point past the
    last paragraph). ``bookmark:`` locators then navigate and edit
    paragraph 0 and report success. A stale ``_mcp_`` mark after
    save/reopen or ``bookmark_cleanup`` still has a name, so the
    missing-name error never runs; only the finder fails. Index 0
    is kept only when the anchor start lies in that element. Any
    other non-negative index is a hit inside the search, not the
    fallback.
    """
    if not isinstance(para_idx, int) or isinstance(para_idx, bool) or para_idx < 0:
        return None
    if para_idx != 0:
        return para_idx
    if _anchor_is_at_index(text_obj, anchor, para_ranges, 0):
        return 0
    return None


def _anchor_is_at_index(text_obj: Any, anchor: Any, para_ranges: list[Any], para_idx: int) -> bool:
    """True when *anchor*'s start lies in ``para_ranges[para_idx]``.

    Same region test as :func:`find_paragraph_for_range`: ``compareRegionStarts``
    is 1 when the first position starts before the second, 0 when equal, and -1
    when after. A paragraph contains the point when the point does not start
    before the paragraph and does not start after the paragraph end.
    """
    if para_idx < 0 or para_idx >= len(para_ranges):
        return False
    get_start = getattr(anchor, "getStart", None)
    if not callable(get_start):
        return False
    try:
        match_start = get_start()
    except Exception:
        return False
    element = para_ranges[para_idx]
    try:
        if _is_text_paragraph(element):
            compare = getattr(text_obj, "compareRegionStarts", None)
            if not callable(compare):
                return False
            # compareRegionStarts is untyped UNO. Only an int is a region test:
            # 1 when the first position starts before the second, 0 when equal,
            # -1 when after.
            cmp_start = compare(match_start, element.getStart())
            cmp_end = compare(match_start, element.getEnd())
            if isinstance(cmp_start, bool) or not isinstance(cmp_start, int):
                return False
            if isinstance(cmp_end, bool) or not isinstance(cmp_end, int):
                return False
            if cmp_start > 0:
                return False
            return cmp_end >= 0
        return _anchor_contains_point(text_obj, match_start, element)
    except Exception:
        return False
