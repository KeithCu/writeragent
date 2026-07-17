# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text pipeline for native grammar: BreakIterator, sentence splits, offset mapping."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

log = logging.getLogger("writeragent.grammar")


def slice_preview_debug(text: str, max_len: int = 72) -> str:
    """Compact one-line preview for DEBUG logs (avoid dumping huge paragraphs)."""
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}\u2026"


from .grammar_obs import grammar_obs

from .grammar_ignore_rules import WA_G_RULE_PREFIX
from .grammar_proofread_locale import (
    GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS,
    GRAMMAR_WHITESPACE_RUN_RE,
    count_nonspace_chars,
    is_whitespace_sentence_locale,
    looks_complete_sentence,
    split_sentence_chunks_by_separator_regex,
    word_before_period_is_abbrev,
)

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
                abbrev_len = word_before_period_is_abbrev(word)  # Returns alpha char count (1-6) or 1 for pure numbers
                grammar_obs("word_before_period_is_abbrev", word=word, abbrev_len=abbrev_len, text_preview=text[pos:pos+60])
                if abbrev_len > 0:  # >0 means it's an abbreviation or number
                    # Skip past the period and any whitespace
                    k = i + 1
                    while k < len(text) and text[k].isspace():
                        k += 1
                    # Use BreakIterator from there to find the real sentence end
                    new_end = bi.endOfSentence(text, k, locale)
                    log.debug("[grammar] split_abbrev_skip word=%r abbrev_len=%d new_end=%d", word, abbrev_len, new_end)
                    grammar_obs("split_abbrev_skip", word=word, abbrev_len=abbrev_len, i=i, k=k, new_end_pos=new_end)
                    # Forward-progress guard: BreakIterator has been observed to return
                    # a position <= the abbreviation period itself (e.g. UNO. followed
                     # by text it cannot bound), which spun this inner loop forever and
                     # bloated the debug log to hundreds of MB. Bail out when that happens.
                    if new_end <= end_pos:
                        end_pos = len(text)
                        break
                    end_pos = new_end
                    continue
            break

        ws_end = extend_through_trailing_whitespace(text, end_pos)

        sentences.append((pos, text[pos:ws_end]))
        pos = ws_end

    log.debug("[grammar] split_into_sentences result count=%d: %r", len(sentences), [s for _, s in sentences])
    return sentences or [(0, text)]


# ---------------------------------------------------------------------------
# Proofreading sentence selection
# ---------------------------------------------------------------------------


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
    # Paragraph-scale pass from LibreOffice (n_start_lo == 0): process ALL sentences in aText.
    # Incremental mode (n_start_lo != 0): only sentences overlapping [n_start_lo, n_suggested_behind_end).
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
    """Split text into word / punctuation tokens using BreakIterator."""
    if not text:
        return []

    tokens = []
    start = 0
    while start < len(text):
        res = break_iterator.getWordBoundary(text, start, locale, 0, True)
        if res.endPos <= start:
            # BI failed to progress, take the rest as a single token
            tokens.append(text[start:])
            break
        if res.startPos > start:
            # BI skipped some text (e.g. control chars), include it as a token
            tokens.append(text[start : res.startPos])
        tokens.append(text[res.startPos : res.endPos])
        start = res.endPos
    return tokens


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
        grammar_obs("normalize_skip_duplicate", wrong=wrong, wrong_idx=wrong_idx, search_pos=search_pos)
        return None
    return rel


def _provider_error_span(window: str, item: dict[str, Any], wrong: str) -> tuple[int, int] | None:
    """Return a validated provider-native span relative to *window*, when present."""
    start = item.get("n_error_start")
    length = item.get("n_error_length")
    if isinstance(start, bool) or isinstance(length, bool) or not isinstance(start, int) or not isinstance(length, int):
        return None
    if start < 0 or length <= 0 or start + length > len(window):
        return None
    if wrong and window[start : start + length] != wrong:
        return None
    return start, length


def _proofreading_suggestions(item: dict[str, Any], correct: Any) -> tuple[str, ...]:
    raw = item.get("suggestions")
    if isinstance(raw, (list, tuple)):
        return tuple(str(value) for value in raw)
    return (str(correct),) if correct else ()


def _suggestion_hint(suggestions: tuple[str, ...]) -> str:
    if not suggestions:
        return "No automatic replacement is available."
    if "" in suggestions:
        return "Suggested fix: delete the highlighted text (the blank replacement below)."
    if any(value.isspace() for value in suggestions):
        return "Suggested fix: replace with one space (the blank replacement below)."
    return "Choose a replacement below."


def normalize_errors_for_text(full_text: str, n_slice_start: int, n_slice_end: int, items: Iterable[dict[str, Any]], ctx: Any = None, loc_key: str | None = None) -> list[NormalizedProofError]:
    """Map ``wrong`` substrings to absolute positions in ``full_text`` (Writer buffer)."""
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
        # Harper returns diagnostics grouped by rule, not text position. Re-searching those
        # substrings in order moved a final single-space error into an earlier space run and
        # then dropped earlier-word diagnostics. Trust validated LSP offsets so each issue
        # remains attached to the span Harper actually reported.
        provider_span = _provider_error_span(window, it, wrong)
        if provider_span is not None:
            rel, length = provider_span
            pos = slice_start + rel
        else:
            anchored_rel = anchor_wrong_in_window(window, wrong, search_pos, wrong_idx=idx)
            if anchored_rel is None:
                continue
            pos = slice_start + anchored_rel
            length = len(wrong)
            if length <= 0:
                continue
            search_pos = anchored_rel + 1

        if correct and provider_span is None:
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

        reason = it.get("reason", "")
        existing = str(it.get("rule_identifier") or "").strip()
        rule_id = existing if existing else f"{WA_G_RULE_PREFIX}{reason}"

        sugg = _proofreading_suggestions(it, correct)
        typ = it.get("type", "grammar")
        provider_short = str(it.get("short_comment") or "").strip()
        provider_full = str(it.get("full_comment") or "").strip()
        comment = provider_short or reason
        short = f"({typ}) {comment}".strip() if comment else str(typ)
        full = provider_full or reason or short
        if provider_short or provider_full:
            short = f"{short} {_suggestion_hint(sugg)}"
        try:
            results.append(NormalizedProofError(n_error_start=pos, n_error_length=length, suggestions=sugg, short_comment=short[:500], full_comment=full[:2000], rule_identifier=rule_id))
        except Exception as e:
            grammar_obs("normalize_error", idx=idx, error=str(e))
    return results
