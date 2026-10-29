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

from plugin.framework.errors import reraise_if_disposed, uno_probe


def compile_search_pattern(
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
) -> re_mod.Pattern[str]:
    """Compile a search pattern into a regex Pattern.

    Literal text is escaped, so one compiled matcher serves both modes.
    """
    flags = 0 if case_sensitive else re_mod.IGNORECASE
    try:
        # Literal mode previously used str.lower() and sliced original text by lowercased
        # offsets, which drifted on characters like 'İ'. Compiling both literal (via re.escape)
        # and regex patterns into a single matcher keeps offsets matched directly to original text.
        return re_mod.compile(pattern if regex else re_mod.escape(pattern), flags)
    except re_mod.error as e:
        raise ValueError(f"Invalid regex: {e}") from e


def search_paragraph_texts(
    pattern: str,
    para_texts: list[str],
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 20,
    context_paragraphs: int = 1,
    stop_checker: Any = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Search *para_texts* for *pattern*; return (matches up to max_results, truncated).

    truncated is True when another hit exists beyond the returned matches.
    The scan stops on that extra hit. A stop_checker break is not truncation.
    """
    if not pattern:
        return [], False

    para_count = len(para_texts)
    compiled = compile_search_pattern(pattern, regex=regex, case_sensitive=case_sensitive)
    matches: list[dict[str, Any]] = []

    for i, ptext in enumerate(para_texts):
        if stop_checker and stop_checker():
            break
        if not ptext:
            continue
        for m in compiled.finditer(ptext):
            if len(matches) >= max_results:
                return matches, True
            ctx_lo = max(0, i - context_paragraphs)
            ctx_hi = min(para_count, i + context_paragraphs + 1)
            context = [{"index": j, "text": para_texts[j]} for j in range(ctx_lo, ctx_hi)]
            matches.append({"text": m.group(), "paragraph_index": i, "context": context})

    return matches, False


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
        return bool(supports(_PARAGRAPH_SERVICE))
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
    while enum.hasMoreElements() is True:  # a MagicMock return never stops a plain truth test
        ranges.append(enum.nextElement())
    return ranges


def _anchor_contains_point(text_obj: Any, match_start: Any, element: Any) -> bool:
    """True when *element*'s anchor contains *match_start*.

    Used only for non-paragraph slots (tables). The element itself is not an
    ``XTextRange``; its anchor is.
    """
    def _check() -> bool:
        anchor = element.getAnchor()
        # compareRegionStarts: 1 if the first starts before the second, 0 if equal, -1 if after.
        cmp_start = text_obj.compareRegionStarts(match_start, anchor.getStart())
        if cmp_start > 0:
            return False
        cmp_end = text_obj.compareRegionStarts(match_start, anchor.getEnd())
        return cmp_end >= 0

    return uno_probe(_check, False)


def _index_outside_paragraphs(
    text_obj: Any,
    match_start: Any,
    para_ranges: list[Any],
    entries: list[tuple[int, Any]],
    low: int,
) -> int | None:
    """Map a point that is not inside any paragraph onto the table in that gap.

    A table anchor starts after the previous paragraph's end and before the
    next paragraph's start, so it never falls inside a paragraph. The slot
    between those paragraphs is the table's enumeration index. Returns None
    when the point is in no paragraph or table slot.
    """
    prev_enum = entries[low - 1][0] if low > 0 else -1
    next_enum = entries[low][0] if low < len(entries) else len(para_ranges)
    slots = range(prev_enum + 1, next_enum)
    if len(slots) == 1:
        return slots[0]
    for slot in slots:
        if _anchor_contains_point(text_obj, match_start, para_ranges[slot]):
            return slot
    return None


def paragraph_entries(para_ranges: list[Any]) -> list[tuple[int, Any]]:
    """Filtered enumeration entries for text paragraphs."""
    return [(idx, para) for idx, para in enumerate(para_ranges) if _is_text_paragraph(para)]


def find_paragraph_for_range(
    match_range: Any,
    para_ranges: list[Any],
    text_obj: Any = None,
    entries: list[tuple[int, Any]] | None = None,
) -> int | None:
    # text_obj is Any (not Any | None): None is a valid default, but typing it
    # optional made basedpyright treat compareRegionStarts as optional access.
    """Return the enumeration index of the element that contains *match_range*, or None when it cannot be placed.

    Binary search calls ``getStart`` / ``getEnd`` only on
    ``com.sun.star.text.Paragraph`` elements. ``SwXTextTable`` is
    not an ``XTextRange``. The returned index is still the
    full-enumeration index (tables keep their slots). A point in the
    gap between paragraphs maps to that slot.
    """
    try:
        if text_obj is None:
            text_obj = match_range.getText()
        match_start = match_range.getStart()
        if entries is None:
            entries = paragraph_entries(para_ranges)
        low = 0
        high = len(entries) - 1

        while low <= high:
            mid = (low + high) // 2
            idx, para = entries[mid]
            # compareRegionStarts: -1 if first is after second, 1 if before, 0 if equal
            cmp_start = text_obj.compareRegionStarts(match_start, para.getStart())
            if cmp_start > 0:
                high = mid - 1
            else:
                cmp_end = text_obj.compareRegionStarts(match_start, para.getEnd())
                if cmp_end < 0:
                    low = mid + 1
                else:
                    return idx
        return _index_outside_paragraphs(text_obj, match_start, para_ranges, entries, low)
    except Exception as exc:
        reraise_if_disposed(exc)
        # Unplaced anchors (stale bookmarks, compare failures) are expected misses,
        # so log at DEBUG instead of logging an ERROR traceback.
        logging.getLogger(__name__).debug("paragraph placement missed", exc_info=True)
        return None


def confirm_paragraph_index(text_obj: Any, anchor: Any, para_ranges: list[Any], para_idx: Any) -> int | None:
    """Return *para_idx* when it is a real hit, or None for the unplaced fallback.

    ``find_paragraph_for_range`` returns None when unplaced; this guard
    keeps a legacy or literal 0 only when the anchor start lies in element 0.
    ``bookmark:`` locators then navigate and edit
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
    element = para_ranges[para_idx]

    def _test() -> bool:
        match_start = anchor.getStart()
        if match_start is None:
            return False
        if _is_text_paragraph(element):
            cmp_start = text_obj.compareRegionStarts(match_start, element.getStart())
            cmp_end = text_obj.compareRegionStarts(match_start, element.getEnd())
            if isinstance(cmp_start, bool) or not isinstance(cmp_start, int):
                return False
            if isinstance(cmp_end, bool) or not isinstance(cmp_end, int):
                return False
            if cmp_start > 0:
                return False
            return cmp_end >= 0
        return _anchor_contains_point(text_obj, match_start, element)

    return uno_probe(_test, False)
