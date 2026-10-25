# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text pipeline for native grammar: BreakIterator, sentence splits, offset mapping.

Internal offsets are Python code points. Convert only at the UNO and BreakIterator
boundary. Cache keys and stored error dicts stay Python offsets.
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

log = logging.getLogger("writeragent.grammar")


def slice_preview_debug(text: str, max_len: int = 72) -> str:
    """Compact one-line preview for DEBUG logs (avoid dumping huge paragraphs)."""
    if not text:
        return ""
    chars: list[str] = []
    in_whitespace = False
    started = False
    limit = max_len + 1
    for ch in text:
        if ch.isspace():
            if started and not in_whitespace:
                chars.append(" ")
                in_whitespace = True
                if len(chars) == limit:
                    break
        else:
            started = True
            in_whitespace = False
            chars.append(ch)
            if len(chars) == limit:
                break
    if not chars:
        return ""
    if len(chars) > max_len:
        return f"{''.join(chars[:max_len])}\u2026"
    if chars[-1] == " ":
        chars.pop()
    return "".join(chars)


from .grammar_ignore_rules import WA_G_RULE_PREFIX
from .grammar_obs import grammar_obs
from .grammar_proofread_locale import (
    GRAMMAR_TRAILING_CLOSERS,
    GRAMMAR_WHITESPACE_RUN_RE,
    is_whitespace_sentence_locale,
    split_sentence_chunks_by_separator_regex,
)
from .locale_abbrev import CLDR_ABBREVS

SentenceSpan = tuple[int, int, str]

# ---------------------------------------------------------------------------
# UTF-16 / Python code point offset conversion
# ---------------------------------------------------------------------------


class Utf16OffsetMap:
    """Map between Python code point offsets and UTF-16 code unit offsets.

    Internal offsets in the proofreader are Python code points. UNO boundaries
    (such as ``doProofreading`` and ``BreakIterator`` positions) use UTF-16
    code units. Non-BMP characters (ord > 0xFFFF) occupy two UTF-16 code units.
    """

    __slots__: tuple[str, ...] = ("_py_len", "_u16_len", "_non_bmp", "_high_surrogates", "_is_bmp")
    _py_len: int
    _u16_len: int
    _is_bmp: bool
    _non_bmp: list[int]
    _high_surrogates: list[int]

    def __init__(self, text: str) -> None:
        self._py_len = len(text)
        non_bmp = [] if text.isascii() else [i for i, ch in enumerate(text) if ord(ch) > 0xFFFF]
        self._non_bmp = non_bmp
        self._high_surrogates = [idx + k for k, idx in enumerate(non_bmp)]
        self._is_bmp = not non_bmp
        self._u16_len = self._py_len + len(non_bmp)

    def py_to_utf16(self, index: int) -> int:
        """Convert a Python code point index to a UTF-16 code unit index."""
        if self._is_bmp:
            return max(0, min(index, self._py_len))
        p = max(0, min(index, self._py_len))
        k = bisect_left(self._non_bmp, p)
        return p + k

    def utf16_to_py(self, index: int) -> int:
        """Convert a UTF-16 code unit index to a Python code point index.

        A UTF-16 index that falls inside a surrogate pair rounds to that code point.
        """
        if self._is_bmp:
            return max(0, min(index, self._py_len))
        if index <= 0:
            return 0
        if index >= self._u16_len:
            return self._py_len
        b = bisect_right(self._high_surrogates, index)
        if b == 0:
            return index
        idx = b - 1
        h = self._high_surrogates[idx]
        if index == h or index == h + 1:
            return self._non_bmp[idx]
        return index - (idx + 1)

    def utf16_span(self, start: int, length: int) -> tuple[int, int]:
        """Convert a Python [start, start + length) span to (u16_start, u16_length)."""
        if self._is_bmp:
            s = max(0, min(start, self._py_len))
            e = max(s, min(start + length, self._py_len))
            return s, e - s
        u_start = self.py_to_utf16(start)
        u_end = self.py_to_utf16(start + length)
        return u_start, max(0, u_end - u_start)


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
    """Return index after ``end_pos`` including following whitespace (line breaks included)."""
    ws_end = end_pos
    while ws_end < len(text) and text[ws_end].isspace():
        ws_end += 1
    return ws_end


# ---------------------------------------------------------------------------
# Abbreviations before "." (BreakIterator sentence extension)
# ---------------------------------------------------------------------------
#
# Current design (enough for shipped grammar locales — do not invent parallel tables):
#   • One static whitelist = filtered Unicode CLDR SentenceBreak suppressions
#     (en/de/es/fr/it/pt/ru only; generated into locale_abbrev.CLDR_ABBREVS).
#   • Heuristics for everything else: pure numbers, single-letter initials,
#     internal dots, consonant-only (Latin/Cyrillic/Greek vowels).
# LO UNO BreakIterator does not apply ICU @ss=standard; embeddings already do.
#
# Future work: see docs/writer/grammar-checker-plan.md (Abbreviation detection / Future work).
#
_ABBREV_VOWELS: frozenset[str] = frozenset("aeiouyаеёиоуыэюяαεηιουω")
# Openers a token can carry when an abbreviation starts a quote/bracket
_ABBREV_LEADING_OPENERS = "\"'“‘«‹([{"


def word_before_period_is_abbrev(word: str) -> int:
    """Returns >0 if word is an abbreviation or number (not a sentence terminator), else 0.

    Checks pure numbers, single-letter initials, internal periods, the CLDR whitelist,
    and consonant-only words across Latin, Cyrillic, and Greek alphabets.
    """
    if not word:
        return 0

    # 1. Pure numbers (e.g. "123", "1.23")
    if all(ch.isdigit() or ch in ".,-" for ch in word) and any(ch.isdigit() for ch in word):
        return 1

    # Normalize: strip trailing periods and lowercase
    w_norm = word.lower().rstrip(".")

    # 2. Single-letter initials (e.g. "A.", "B.", "г.")
    if len(w_norm) == 1 and w_norm.isalpha():
        return len(word)

    # 3. CLDR SentenceBreak suppressions whitelist
    if w_norm in CLDR_ABBREVS:
        return len(word)

    # 4. Words with internal periods (e.g. "U.S.A.", "d.h.", "т.е.")
    if "." in w_norm:
        alpha_count = sum(1 for ch in w_norm if ch.isalpha())
        if alpha_count >= 2:
            return len(word)

    # 5. Consonant-only check (no vowels in Latin, Cyrillic, or Greek)
    # Must be a purely alphabetic word (no internal apostrophes/contractions like "dn't")
    if w_norm.isalpha() and not any(ch in _ABBREV_VOWELS for ch in w_norm):
        return len(word)

    return 0


def split_into_sentences(ctx: Any, locale_key: str, text: str) -> list[tuple[int, str]]:
    """Split *text* into ``(start_offset, sentence_text)`` pairs."""
    if not text or text.isspace():
        return []

    if is_whitespace_sentence_locale(locale_key):
        return split_sentence_chunks_by_separator_regex(text, GRAMMAR_WHITESPACE_RUN_RE)

    bi, locale = get_break_iterator_and_locale(ctx, locale_key)

    u16_map = Utf16OffsetMap(text)
    pos = 0
    sentences = []

    while pos < len(text):
        u16_pos = u16_map.py_to_utf16(pos)
        end_u16 = bi.endOfSentence(text, u16_pos, locale)
        end_pos = u16_map.utf16_to_py(end_u16)

        if end_pos <= pos:
            end_pos = len(text)

        # Abbreviation extension loop: BreakIterator treats every period as a potential sentence
        # boundary. When the word preceding '.' is an abbreviation or number (e.g. 'Dr.', 'U.S.A.',
        # '1.5'), we skip past it and ask BreakIterator for the next boundary from position k.
        while end_pos < len(text):
            # Cursor i: scan backward from candidate end to locate the sentence-terminating period,
            # skipping any intervening trailing whitespace or trailing closers (GRAMMAR_TRAILING_CLOSERS).
            i = end_pos - 1
            while i >= pos and (text[i].isspace() or text[i] in GRAMMAR_TRAILING_CLOSERS):
                i -= 1
            if i >= pos and text[i] == ".":
                # Cursor j: scan backward from period to find the start of the preceding word token,
                # stripping any leading openers carried when the abbreviation starts a quote/bracket.
                j = i - 1
                while j >= pos and not text[j].isspace() and text[j] not in ".!?":
                    j -= 1
                word = text[j + 1 : i].lstrip(_ABBREV_LEADING_OPENERS)
                abbrev_len = word_before_period_is_abbrev(word)  # Returns alpha char count (1-6) or 1 for pure numbers
                grammar_obs("word_before_period_is_abbrev", word=word, abbrev_len=abbrev_len)
                if abbrev_len > 0:  # >0 means it's an abbreviation or number
                    # Cursor k: advance past the period, trailing closers, and following whitespace to set the
                    # clean restart position for BreakIterator.
                    k = i + 1
                    while k < len(text) and (text[k].isspace() or text[k] in GRAMMAR_TRAILING_CLOSERS):
                        k += 1
                    # Use BreakIterator from position k to find the real sentence end.
                    u16_k = u16_map.py_to_utf16(k)
                    new_u16 = bi.endOfSentence(text, u16_k, locale)
                    new_end = u16_map.utf16_to_py(new_u16)
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

    if log.isEnabledFor(logging.DEBUG):
        log.debug("[grammar] split_into_sentences count=%d preview=%r locale=%s", len(sentences), slice_preview_debug(text), locale_key)
    return sentences


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
) -> list[SentenceSpan]:
    """Return ``(abs_start, abs_end, sentence_text)`` for sentences Writer should check this call.

    - ``n_start_lo == 0``: paragraph-scale pass — all sentences in ``a_text``.
    - Else: incremental — sentences overlapping LibreOffice's active range.
    """
    all_sents = split_into_sentences(ctx, loc_key, a_text)
    if not all_sents:
        return []
    all_sents = merge_dialogue_sentences(all_sents)
    nlen = len(a_text)
    spans: list[SentenceSpan] = []
    for off, txt in all_sents:
        end = off + len(txt)
        spans.append((off, end, txt))
    # Paragraph-scale pass from LibreOffice (n_start_lo == 0): process ALL sentences in aText.
    # Incremental mode (n_start_lo != 0): only sentences overlapping [n_start_lo, n_suggested_behind_end).
    if n_start_lo == 0:
        return spans
    return filter_sentence_spans_for_overlap(spans, n_start_lo, n_suggested_behind_end, nlen)


def filter_sentence_spans_for_overlap(
    spans: Sequence[SentenceSpan],
    n_start: int,
    n_suggested_end: int,
    nlen: int,
) -> list[SentenceSpan]:
    """Keep spans whose ``[start, end)`` overlaps the clamped active window."""
    lo = max(0, min(n_start, nlen))
    hi = max(lo, min(n_suggested_end, nlen))
    return [(s, e, t) for s, e, t in spans if span_overlaps_range(s, e, lo, hi)]


def active_spans_from_paragraph(
    paragraph_spans: Sequence[SentenceSpan],
    a_text: str,
    n_start: int,
    n_suggested_end: int,
) -> list[SentenceSpan]:
    """Active-window sentences from an already-split paragraph.

    ``n_start == 0`` is a paragraph-scale pass: return every span and ignore
    the suggested end (LibreOffice's end hint is not a clip). Incremental
    calls (``n_start != 0``) keep only spans overlapping the active range.
    """
    if n_start == 0:
        return list(paragraph_spans)
    return filter_sentence_spans_for_overlap(paragraph_spans, n_start, n_suggested_end, len(a_text))


def resolve_work_spans(
    a_doc_id: str,
    loc_key: str,
    a_text: str,
    n_start: int,
    n_suggested_end: int,
    paragraph_spans: list[SentenceSpan],
) -> list[SentenceSpan]:
    """Active-window spans from the already-split, threshold-filtered paragraph.

    One BreakIterator pass happens in ``doProofreading``; this only overlap-filters
    (or returns all spans when ``n_start == 0``). ``raw_candidates`` is the
    post-threshold paragraph count.
    """
    work_spans = active_spans_from_paragraph(paragraph_spans, a_text, n_start, n_suggested_end)
    if not work_spans:
        grammar_obs(
            "do_proofreading_skip",
            reason="no_active_sentences",
            doc_id=a_doc_id,
            n_start_lo=n_start,
            raw_candidates=len(paragraph_spans),
            grammar_bcp47=loc_key,
        )
        return []
    return work_spans


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
    u16_map = Utf16OffsetMap(text)
    while start < len(text):
        u16_start = u16_map.py_to_utf16(start)
        res = break_iterator.getWordBoundary(text, u16_start, locale, 0, True)
        py_start_pos = u16_map.utf16_to_py(res.startPos)
        py_end_pos = u16_map.utf16_to_py(res.endPos)
        if py_end_pos <= start:
            # BI failed to progress, take the rest as a single token
            tokens.append(text[start:])
            break
        if py_start_pos > start:
            # BI skipped some text (e.g. control chars), include it as a token
            tokens.append(text[start : py_start_pos])
        tokens.append(text[py_start_pos : py_end_pos])
        start = py_end_pos
    return tokens


def anchor_wrong_in_window(window: str, wrong: str, search_pos: int, *, wrong_idx: int | None = None) -> int | None:
    """Find ``wrong`` in ``window`` starting at ``search_pos``, with ordered-scan fallback."""
    if not wrong:
        return None
    rel = window.find(wrong, search_pos)
    if rel >= 0:
        return rel
    if window.find(wrong) >= 0:
        grammar_obs("normalize_skip_duplicate", wrong=wrong, wrong_idx=wrong_idx, search_pos=search_pos)
    return None


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
    if suggestions == (" ",):
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


def _build_normalized_error(pos: int, length: int, it: dict[str, Any], correct: str) -> NormalizedProofError:
    """Construct a NormalizedProofError from the raw JSON payload properties."""
    reason = str(it.get("reason") or "")
    existing = str(it.get("rule_identifier") or "").strip()
    rule_id = existing if existing else f"{WA_G_RULE_PREFIX}{reason}"

    sugg = _proofreading_suggestions(it, correct)
    typ = str(it.get("type") or "grammar")
    provider_short = str(it.get("short_comment") or "").strip()
    provider_full = str(it.get("full_comment") or "").strip()
    comment = provider_short or reason
    short = f"({typ}) {comment}".strip() if comment else str(typ)
    full = provider_full or reason or short
    if provider_short or provider_full:
        short = f"{short} {_suggestion_hint(sugg)}"
    return NormalizedProofError(
        n_error_start=pos,
        n_error_length=length,
        suggestions=sugg,
        short_comment=short[:500],
        full_comment=full[:2000],
        rule_identifier=rule_id,
    )


def normalize_errors_for_text(full_text: str, n_slice_start: int, n_slice_end: int, items: Iterable[dict[str, Any]], ctx: Any = None, loc_key: str | None = None) -> list[NormalizedProofError]:
    """Map ``wrong`` substrings to absolute positions in ``full_text`` (Writer buffer)."""
    slice_end = min(n_slice_end, len(full_text))
    slice_start = max(0, min(n_slice_start, slice_end))
    window = full_text[slice_start:slice_end]
    results: list[NormalizedProofError] = []
    used_spans: list[tuple[int, int]] = []

    bi: Any = None
    locale: Any = None

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
            if bi is None:
                bi, locale = get_break_iterator_and_locale(ctx, loc_key)
            pos, length = _expand_span_for_overlap(pos, length, correct, full_text, bi, locale)
            span_end = min(pos + length, slice_end)
            pos = max(pos, slice_start)
            length = max(0, span_end - pos)
            expanded_wrong = full_text[pos : pos + length]
            if expanded_wrong == correct:
                continue

        # 3. Check for span collisions.
        # A zero-width insert is a point, not a character range. Recording it
        # in ``used_spans`` would make a later positive span that covers that
        # point look overlapping and drop a diagnostic that used to be kept.
        if length > 0:
            span = (pos, pos + length)
            if any(span_overlaps_range(span[0], span[1], s, e) for s, e in used_spans):
                continue
            used_spans.append(span)

        # 4. Build and append the NormalizedProofError
        results.append(_build_normalized_error(pos, length, it, correct))

    return results


def calculate_covered_span_end(active_spans: Sequence[SentenceSpan]) -> int:
    """Return the maximum end offset among active sentence spans, or 0 if empty."""
    if not active_spans:
        return 0
    return max(end for _start, end, _text in active_spans)


def reconcile_active_and_paragraph_spans(
    active_spans: Sequence[SentenceSpan],
    uncached_paragraph_spans: Sequence[SentenceSpan],
) -> list[SentenceSpan]:
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

