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
        low = 0
        high = len(para_ranges) - 1

        while low <= high:
            mid = (low + high) // 2
            para = para_ranges[mid]
            # compareRegionStarts: -1 if first is after second, 1 if before, 0 if equal
            cmp_start = safe_call(text_obj.compareRegionStarts, "compareRegionStarts start", match_start, safe_call(para.getStart, "Get para start"))
            if cmp_start > 0:
                high = mid - 1
            else:
                cmp_end = safe_call(text_obj.compareRegionStarts, "compareRegionStarts end", match_start, safe_call(para.getEnd, "Get para end"))
                if cmp_end < 0:
                    low = mid + 1
                else:
                    return mid
    except UnoObjectError:
        logging.getLogger(__name__).exception("find_paragraph_for_range error")
    return 0
