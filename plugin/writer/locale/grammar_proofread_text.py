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

        # Abbreviation extension loop: BreakIterator treats every period as a potential sentence
        # boundary. When the word preceding '.' is an abbreviation or number (e.g. 'Dr.', 'U.S.A.',
        # '1.5'), we skip past it and ask BreakIterator for the next boundary from position k.
        while end_pos < len(text):
            # Cursor i: scan backward from candidate end to locate the sentence-terminating period,
            # skipping any intervening trailing whitespace or closing quotes.
            i = end_pos - 1
            while i >= pos and text[i].isspace():
                i -= 1
            if i >= pos and text[i] == ".":
                # Cursor j: scan backward from period to find the start of the preceding word token.
                j = i - 1
                while j >= pos and not text[j].isspace() and text[j] not in ".!?":
                    j -= 1
                word = text[j + 1 : i]
                abbrev_len = word_before_period_is_abbrev(word)  # Returns alpha char count (1-6) or 1 for pure numbers
                grammar_obs("word_before_period_is_abbrev", word=word, abbrev_len=abbrev_len, text_preview=text[pos:pos+60])
                if abbrev_len > 0:  # >0 means it's an abbreviation or number
                    # Cursor k: advance past the period and any following whitespace to set the
                    # clean restart position for BreakIterator.
                    k = i + 1
                    while k < len(text) and text[k].isspace():
                        k += 1
                    # Use BreakIterator from position k to find the real sentence end.
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

    log.debug("[grammar] split_into_sentences result count=%d: %r", len(sentences), [s for _unused_idx, s in sentences])
    return sentences or [(0, text)]


# ---------------------------------------------------------------------------
# Proofreading sentence selection
# ---------------------------------------------------------------------------


# Double-quote characters tracked for dialogue merge. Single quotes are intentionally
# excluded to avoid false merges on apostrophes (don't, it's, etc.).
_DIALOGUE_QUOTES: frozenset[str] = frozenset((
    '"',
    "\u201c",  # Left double quotation mark “
    "\u201d",  # Right double quotation mark ”
    "\u201e",  # Double low-9 quotation mark „
    "\u00ab",  # Left-pointing double angle quotation mark «
    "\u00bb",  # Right-pointing double angle quotation mark »
    "\u300c",  # Left corner bracket 「
    "\u300d",  # Right corner bracket 」
    "\u300e",  # White left corner bracket 『
    "\u300f",  # White right corner bracket 』
))


def _count_dialogue_quotes(text: str) -> int:
    """Count double-quote-class characters in *text* for dialogue merge parity."""
    return sum(1 for ch in text if ch in _DIALOGUE_QUOTES)


def merge_dialogue_sentences(
    sentences: list[tuple[int, str]],
    max_merge_chars: int = 1000,
    max_consecutive_merges: int = 4,
) -> list[tuple[int, str]]:
    """Merge falsely split dialogue sentences (e.g. at '!' or '?') by checking quote parity.

    If a sentence chunk has an odd count of double quotes, it is likely split
    mid-dialogue. We merge it with the subsequent sentence up to safety thresholds.
    """
    if not sentences:
        return []

    merged: list[tuple[int, str]] = []
    current_start, current_text = sentences[0]
    consecutive_merges = 0

    for i in range(1, len(sentences)):
        next_start, next_text = sentences[i]
        quote_count = _count_dialogue_quotes(current_text)

        if (
            quote_count % 2 != 0
            and len(current_text) + len(next_text) <= max_merge_chars
            and consecutive_merges < max_consecutive_merges
        ):
            current_text += next_text
            consecutive_merges += 1
        else:
            merged.append((current_start, current_text))
            current_start, current_text = next_start, next_text
            consecutive_merges = 0

    merged.append((current_start, current_text))
    return merged


def span_overlaps_range(s_start: int, s_end: int, lo: int, hi: int) -> bool:
    """Half-open ``[s_start, s_end)`` overlaps ``[lo, hi)`` (empty range yields False)."""
    return lo < hi and s_start < s_end and s_start < hi and s_end > lo


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
    all_sents = merge_dialogue_sentences(all_sents)
    nlen = len(a_text)
    spans: list[tuple[int, int, str]] = []
    for off, txt in all_sents:
        end = off + len(txt)
        spans.append((off, end, txt))
    # Paragraph-scale pass from LibreOffice (n_start_lo == 0): process ALL sentences in aText.
    # Incremental mode (n_start_lo != 0): only sentences overlapping [n_start_lo, n_suggested_behind_end).
    if n_start_lo == 0:
        return spans
    return filter_sentence_spans_for_overlap(spans, n_start_lo, n_suggested_behind_end, nlen)


def filter_sentence_spans_for_overlap(
    spans: Sequence[tuple[int, int, str]],
    n_start: int,
    n_suggested_end: int,
    nlen: int,
) -> list[tuple[int, int, str]]:
    """Keep spans whose ``[start, end)`` overlaps the clamped active window."""
    lo = max(0, min(n_start, nlen))
    hi = max(lo, min(n_suggested_end, nlen))
    return [(s, e, t) for s, e, t in spans if span_overlaps_range(s, e, lo, hi)]


def active_spans_from_paragraph(
    paragraph_spans: Sequence[tuple[int, int, str]],
    a_text: str,
    n_start: int,
    n_suggested_end: int,
) -> list[tuple[int, int, str]]:
    """Active-window sentences from an already-split paragraph.

    ``n_start == 0`` is a paragraph-scale pass: return every span and ignore
    the suggested end (LibreOffice's end hint is not a clip). Incremental
    calls (``n_start != 0``) keep only spans overlapping the active range.
    """
    if n_start == 0:
        return list(paragraph_spans)
    return filter_sentence_spans_for_overlap(paragraph_spans, n_start, n_suggested_end, len(a_text))


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
    """Return a validated provider-native span relative to *window*, when present.

    ``length <= 0`` must not reject Harper inserts
    (``start == end``). ``normalize_errors_for_text`` then fails
    ``anchor_wrong_in_window`` on the empty ``wrong`` and drops the
    issue, so the sentence looks clean. A point with
    ``0 <= start <= len(window)`` is kept. Negative starts,
    non-ints, bools, and spans past the end still return None.
    """
    start = item.get("n_error_start")
    length = item.get("n_error_length")
    if isinstance(start, bool) or isinstance(length, bool) or not isinstance(start, int) or not isinstance(length, int):
        return None
    # ``start == len(window)`` is the insert position after the last character.
    if start < 0 or length < 0 or start > len(window) or start + length > len(window):
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
    from plugin.framework.i18n import _

    if not suggestions:
        return _("No automatic replacement is available.")
    if "" in suggestions:
        return _("Suggested fix: delete the highlighted text (the blank replacement below).")
    if any(value.isspace() for value in suggestions):
        return _("Suggested fix: replace with one space (the blank replacement below).")
    return _("Choose a replacement below.")


def _expand_span_for_overlap(pos: int, length: int, correct: str, full_text: str, bi: Any, locale: Any) -> tuple[int, int]:
    """Expand the error span if the correction overlaps with adjacent text tokens."""
    t_c = _tokenize(correct, bi, locale)
    max_overlap_chars = max(len(correct) * 2, 128)

    # Check suffix overlap
    suffix_window = full_text[pos + length : pos + length + max_overlap_chars]
    t_s = _tokenize(suffix_window, bi, locale)
    for k in range(min(len(t_c), len(t_s)), 0, -1):
        if t_c[-k:] == t_s[:k]:
            length += sum(len(t) for t in t_c[-k:])
            break

    # Check prefix overlap
    prefix_window = full_text[max(0, pos - max_overlap_chars) : pos]
    t_p = _tokenize(prefix_window, bi, locale)
    for k in range(min(len(t_p), len(t_c)), 0, -1):
        if t_p[-k:] == t_c[:k]:
            overlap_len = sum(len(t) for t in t_p[-k:])
            pos -= overlap_len
            length += overlap_len
            break

    return pos, length


def _is_span_overlapping(span: tuple[int, int], used_spans: list[tuple[int, int]]) -> bool:
    """Return True if the proposed span overlaps with any previously used spans."""
    return any(not (span[1] <= o[0] or span[0] >= o[1]) for o in used_spans)


def _build_normalized_error(pos: int, length: int, it: dict[str, Any], correct: str, idx: int) -> NormalizedProofError | None:
    """Construct a NormalizedProofError from the raw JSON payload properties."""
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
        return NormalizedProofError(
            n_error_start=pos,
            n_error_length=length,
            suggestions=sugg,
            short_comment=short[:500],
            full_comment=full[:2000],
            rule_identifier=rule_id,
        )
    except Exception as e:
        grammar_obs("normalize_error", idx=idx, error=str(e))
        return None


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

        # 1. Resolve position (provider span or anchor)
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

        # 2. Expand span for overlap
        if correct and provider_span is None:
            pos, length = _expand_span_for_overlap(pos, length, correct, full_text, bi, locale)
            expanded_wrong = full_text[pos : pos + length]
            if expanded_wrong == correct:
                continue

        # 3. Check for span collisions.
        # A zero-width insert is a point, not a character range. Recording it
        # in ``used_spans`` would make a later positive span that covers that
        # point look overlapping and drop a diagnostic that used to be kept.
        if length > 0:
            span = (pos, pos + length)
            if _is_span_overlapping(span, used_spans):
                continue
            used_spans.append(span)

        # 4. Build and append the NormalizedProofError
        err = _build_normalized_error(pos, length, it, correct, idx)
        if err is not None:
            results.append(err)

    return results


def calculate_covered_span_end(active_spans: Sequence[tuple[int, int, str]]) -> int:
    """Return the maximum end offset among active sentence spans, or 0 if empty."""
    if not active_spans:
        return 0
    return max(end for _start, end, _text in active_spans)


def reconcile_active_and_paragraph_spans(
    active_spans: Sequence[tuple[int, int, str]],
    uncached_paragraph_spans: Sequence[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Return the active sentence spans that need background enqueuing.

    An active span needs checking if its start offset matches an uncached sentence
    in the paragraph. Start-only matching assumes ``split_into_sentences`` yields
    non-overlapping spans (unique starts). A splitter that reused a start with a
    different end would coalesce those sentences; match ``(start, end)`` then.
    """
    uncached_starts = {start for start, _end, _text in uncached_paragraph_spans}
    return [
        (start, end, text)
        for start, end, text in active_spans
        if start in uncached_starts
    ]

