# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text pipeline for native grammar: BreakIterator, sentence splits, offset mapping."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .grammar_proofread_locale import (
    GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS,
    GRAMMAR_WHITESPACE_RUN_RE,
    count_nonspace_chars,
    fingerprint_for_text,
    is_whitespace_sentence_locale,
    looks_complete_sentence,
    parse_grammar_json,  # noqa: F401 — re-export for `grammar_proofread_text.parse_grammar_json`
    split_sentence_chunks_by_separator_regex,
    word_before_period_is_abbrev,
)

_grammar_diag = logging.getLogger("writeragent.grammar")

# ---------------------------------------------------------------------------
# LibreOffice BreakIterator + Locale
# ---------------------------------------------------------------------------


def get_break_iterator_and_locale(ctx: Any, loc_key: str | None) -> tuple[Any, Any]:
    """Initialize LO BreakIterator and Locale from a BCP-47 key."""
    import uno

    smgr = ctx.ServiceManager
    bi = smgr.createInstanceWithContext("com.sun.star.i18n.BreakIterator", ctx)
    parts = (loc_key or "").split("-")
    if len(parts) > 1:
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", Language=parts[0], Country=parts[1])
    else:
        loc = uno.createUnoStruct("com.sun.star.lang.Locale", Language=parts[0])
    return bi, loc


# ---------------------------------------------------------------------------
# Sentence splitting (BreakIterator + abbrev heuristic; Thai/Lao/Khmer whitespace)
# ---------------------------------------------------------------------------


def extend_through_trailing_whitespace(text: str, end_pos: int) -> int:
    """Return index after ``end_pos`` including any following whitespace on the same line."""
    ws_end = end_pos
    while ws_end < len(text) and text[ws_end].isspace():
        ws_end += 1
    return ws_end


def split_into_sentences(ctx: Any, locale_key: str, text: str) -> list[tuple[int, str]]:
    """Split *text* into ``(start_offset, sentence_text)`` pairs."""
    if not text or not text.strip():
        return []

    if is_whitespace_sentence_locale(locale_key):
        return split_sentence_chunks_by_separator_regex(text, GRAMMAR_WHITESPACE_RUN_RE)

    bi, locale = get_break_iterator_and_locale(ctx, locale_key)

    pos = 0
    sentences = []

    while pos < len(text):
        end_pos = bi.endOfSentence(text, pos, locale)

        if end_pos <= pos:
            end_pos = len(text)

        while end_pos < len(text):
            i = end_pos - 1
            while i >= pos and text[i].isspace():
                i -= 1
            if i >= pos and text[i] == ".":
                j = i - 1
                while j >= pos and not text[j].isspace() and text[j] not in ".!?":
                    j -= 1
                word = text[j + 1 : i]
                if word_before_period_is_abbrev(word):
                    next_end = bi.endOfSentence(text, end_pos, locale)
                    if next_end > end_pos:
                        end_pos = next_end
                        continue
            break

        ws_end = extend_through_trailing_whitespace(text, end_pos)

        sentences.append((pos, text[pos:ws_end]))
        pos = ws_end

    return sentences or [(0, text)]


# ---------------------------------------------------------------------------
# Proofreading sentence selection
# ---------------------------------------------------------------------------


def grammar_inflight_key(a_document_identifier: str, loc_key: str, sentence_start: int) -> str:
    """Queue supersede key: stable while editing inside one sentence; distinct per sentence in a paragraph."""
    return f"{a_document_identifier}|{loc_key}|{sentence_start}"


def span_overlaps_range(s_start: int, s_end: int, lo: int, hi: int) -> bool:
    """Half-open ``[s_start, s_end)`` overlaps ``[lo, hi)`` (empty range yields False)."""
    return lo < hi and s_start < hi and s_end > lo


def candidate_sentence_spans_for_proofreading(
    ctx: Any,
    loc_key: str,
    a_text: str,
    n_start_lo: int,
    n_suggested_behind_end: int,
) -> list[tuple[int, int, str]]:
    """Return ``(abs_start, abs_end, sentence_text)`` for sentences Writer should check this call.

    - ``n_start_lo == 0``: paragraph-scale pass — all sentences in ``a_text``.
    - Else: incremental — sentences overlapping LibreOffice's active range.
    """
    all_sents = split_into_sentences(ctx, loc_key, a_text)
    if not all_sents:
        return []
    nlen = len(a_text)
    spans: list[tuple[int, int, str]] = []
    for off, txt in all_sents:
        end = off + len(txt)
        spans.append((off, end, txt))
    if n_start_lo == 0:
        return spans
    lo = max(0, min(n_start_lo, nlen))
    hi = max(lo, min(n_suggested_behind_end, nlen))
    return [(s, e, t) for s, e, t in spans if span_overlaps_range(s, e, lo, hi)]


def filter_sentence_spans_for_thresholds(spans: Sequence[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Drop incomplete sentences shorter than ``GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS`` (conservative churn avoidance)."""
    out: list[tuple[int, int, str]] = []
    for s, e, txt in spans:
        nonspace_len = count_nonspace_chars(txt)
        complete_sentence = looks_complete_sentence(txt)
        partial_allowed = nonspace_len >= GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
        if not complete_sentence and not partial_allowed:
            continue
        out.append((s, e, txt))
    return out


# ---------------------------------------------------------------------------
# Map wrong/correct pairs to absolute offsets in the proofread buffer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedProofError:
    """One grammar issue with absolute offsets in the proofread buffer ``rText``."""

    n_error_start: int
    n_error_length: int
    suggestions: tuple[str, ...]
    short_comment: str
    full_comment: str
    rule_identifier: str


def _tokenize(text: str, break_iterator: Any, locale: Any) -> list[str]:
    """Split text into word / punctuation tokens (BreakIterator when available)."""
    if not text:
        return []

    tokens = []
    start = 0
    while start < len(text):
        res = break_iterator.getWordBoundary(text, start, locale, 0, True)
        if res.endPos <= start:
            break
        tokens.append(text[res.startPos : res.endPos])
        start = res.endPos

    if sum(len(t) for t in tokens) == len(text):
        return tokens

    return re.findall(r"\w+|\W+", text)


def anchor_wrong_in_window(window: str, wrong: str, search_pos: int, *, wrong_idx: int | None = None) -> int | None:
    """Find ``wrong`` in ``window`` starting at ``search_pos``, with ordered-scan fallback."""
    if not wrong:
        return None
    rel = window.find(wrong, search_pos)
    if rel >= 0:
        return rel
    rel = window.find(wrong)
    if rel < 0:
        return None
    if rel < search_pos:
        _grammar_diag.debug("[grammar] normalize_errors_for_text: skipped out-of-order duplicate wrong=%r idx=%s search_pos=%s", wrong, wrong_idx, search_pos)
        return None
    return rel


def normalize_errors_for_text(full_text: str, n_slice_start: int, n_slice_end: int, items: Iterable[dict[str, Any]], ignored: set[str] | None = None, ctx: Any = None, loc_key: str | None = None) -> list[NormalizedProofError]:
    """Map ``wrong`` substrings to absolute positions in ``full_text`` (Writer buffer)."""
    ignored = ignored or set()
    slice_end = min(n_slice_end, len(full_text))
    slice_start = max(0, min(n_slice_start, slice_end))
    window = full_text[slice_start:slice_end]
    results: list[NormalizedProofError] = []
    used_spans: list[tuple[int, int]] = []

    bi, locale = get_break_iterator_and_locale(ctx, loc_key)

    search_pos = 0

    for idx, it in enumerate(items):
        wrong = it.get("wrong", "")
        correct = it.get("correct", "")
        rel = anchor_wrong_in_window(window, wrong, search_pos, wrong_idx=idx)
        if rel is None:
            continue

        pos = slice_start + rel
        length = len(wrong)
        if length <= 0:
            continue

        search_pos = rel + 1

        if correct:
            suffix = full_text[pos + length :]
            t_c = _tokenize(correct, bi, locale)
            t_s = _tokenize(suffix, bi, locale)
            for k in range(min(len(t_c), len(t_s)), 0, -1):
                if t_c[-k:] == t_s[:k]:
                    overlap_len = sum(len(t) for t in t_c[-k:])
                    length += overlap_len
                    break

            prefix = full_text[:pos]
            t_p = _tokenize(prefix, bi, locale)
            for k in range(min(len(t_p), len(t_c)), 0, -1):
                if t_p[-k:] == t_c[:k]:
                    overlap_len = sum(len(t) for t in t_p[-k:])
                    pos -= overlap_len
                    length += overlap_len
                    break

            expanded_wrong = full_text[pos : pos + length]
            if expanded_wrong == correct:
                continue

        span = (pos, pos + length)
        if any(not (span[1] <= o[0] or span[0] >= o[1]) for o in used_spans):
            continue
        used_spans.append(span)
        rule_id = f"wa_grammar_{idx}_{fingerprint_for_text(wrong)[:8]}"
        if rule_id in ignored:
            continue
        sugg = (correct,) if correct else ()
        reason = it.get("reason", "")
        typ = it.get("type", "grammar")
        short = f"({typ}) {reason}".strip() if reason else str(typ)
        full = reason or short
        try:
            results.append(NormalizedProofError(n_error_start=pos, n_error_length=length, suggestions=sugg, short_comment=short[:500], full_comment=full[:2000], rule_identifier=rule_id))
        except Exception as e:
            _grammar_diag.warning("[grammar] normalize_errors_for_text: skipped item idx=%s: %s", idx, e, exc_info=True)
    return results
