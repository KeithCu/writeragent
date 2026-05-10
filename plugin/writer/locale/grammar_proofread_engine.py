# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python helpers for AI grammar proofreading (JSON parsing, cache, offsets)."""

from __future__ import annotations

import hashlib
import logging
import collections
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, cast

import json_repair

from plugin.framework.json_utils import safe_json_loads
from .grammar_locale_registry import GRAMMAR_REGISTRY_LOCALE_TAGS as _GRAMMAR_REGISTRY_LOCALE_TAGS

log = logging.getLogger(__name__)
_grammar_diag = logging.getLogger("writeragent.grammar")

# Re-export: hyphenated tags for ``LinguisticWriterAgentGrammar.xcu`` and tests (see
# ``grammar_locale_registry`` — same list as repo-root ``locales/`` + ``en`` variants).
GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = _GRAMMAR_REGISTRY_LOCALE_TAGS

_CACHE_LOCK = threading.Lock()
_ignored_rules: set[str] = set()
MAX_CACHE_SIZE = 2048
# Limit how many recent entries we scan for incomplete-sentence prefix
# compaction on each cache_put_sentence. 10 is a good balance between
# effectiveness (catches typical typing chains) and CPU (few memory touches).
MAX_RECENT_INCOMPLETE_SCAN = 10


def cache_clear() -> None:
    """Clear proofreading cache (e.g. tests)."""
    with _CACHE_LOCK:
        _SENTENCE_CACHE.clear()


def ignore_rules_clear() -> None:
    with _CACHE_LOCK:
        _ignored_rules.clear()


def ignore_rule_add(rule_id: str) -> None:
    with _CACHE_LOCK:
        _ignored_rules.add(str(rule_id))


def ignored_rules_snapshot() -> set[str]:
    with _CACHE_LOCK:
        return set(_ignored_rules)


def fingerprint_for_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _normalize_for_sentence_cache(text: str) -> str:
    """Canonical form for cache key that preserves first sentence terminator.

    - rstrip() whitespace (preserves existing "Hello." vs "Hello. " behavior).
    - Keep everything up to and including the *first* sentence terminator.
    - Ignore any additional trailing punctuation after the first terminator.
    - This makes "Hello." and "Hello..." share a cache entry, and
      "Hello?" and "Hello?..." share one, but "Hello?" and "Hello." remain distinct.
    """
    s = text.rstrip()
    if not s:
        return s
    # Match up to first terminator, then any extra trailing punctuation to discard
    # Non-greedy match ensures we stop at the first terminator
    match = re.search(r"^(.*?[.!?…。！？])([.!?…。！？]*)$", s)
    if match:
        return match.group(1)
    return s


def sentence_identity_fp(sentence: str) -> str:
    """Stable fingerprint for cache lookup: normalize then hash (same key space as ``make_sentence_key``)."""
    return fingerprint_for_text(_normalize_for_sentence_cache(sentence))


def sentence_cache_key_prefix(locale_key: str) -> str:
    """Prefix for every sentence-cache OrderedDict key: ``sent|<locale>|``."""
    return f"sent|{locale_key}|"


def should_evict_incomplete_prefix_predecessor(*, other_complete: bool, other_canon: str, new_canon: str) -> bool:
    """LRU prefix compaction: drop an older incomplete entry if ``new_canon`` strictly extends it."""
    if other_complete:
        return False
    if len(other_canon) >= len(new_canon):
        return False
    return new_canon.startswith(other_canon)


# Sentence completeness helpers (mirrored from ai_grammar_proofreader.py
# to avoid import cycles). Used for deciding whether to evict incomplete
# prefix predecessors during cache_put_sentence.
_SENTENCE_TERMINATORS = frozenset((".", "!", "?", "…", "؟", "。", "！", "？", "।"))
_TRAILING_CLOSERS = frozenset(('"', "'", ")", "]", "}", ">", "»", "“", "‘", "」", "』", "）", "］", "〉", "》", "】", "〕", "〗", "〛"))


def _last_meaningful_char(text: str) -> str:
    """Return the last non-closer character (skipping quotes, brackets, etc.)."""
    if not text:
        return ""
    for ch in reversed(text.rstrip()):
        if ch in _TRAILING_CLOSERS:
            continue
        return ch
    return ""


def _is_complete_sentence(canon: str) -> bool:
    """True if the canonical normalized text ends with a sentence terminator.

    Used to protect complete sentences from being evicted by incomplete ones
    during prefix compaction.
    """
    return _last_meaningful_char(canon) in _SENTENCE_TERMINATORS


@dataclass(frozen=True)
class NormalizedProofError:
    """One grammar issue with absolute offsets in the proofread buffer ``rText``."""

    n_error_start: int
    n_error_length: int
    suggestions: tuple[str, ...]
    short_comment: str
    full_comment: str
    rule_identifier: str


def _get_break_iterator_and_locale(ctx: Any, loc_key: str | None) -> tuple[Any, Any]:
    """Helper to initialize LO BreakIterator and Locale from a BCP-47 key."""
    if not ctx or not loc_key:
        return None, None
    try:
        import uno

        smgr = ctx.ServiceManager
        bi = smgr.createInstanceWithContext("com.sun.star.i18n.BreakIterator", ctx)
        parts = loc_key.split("-")
        if len(parts) > 1:
            loc = uno.createUnoStruct("com.sun.star.lang.Locale", Language=parts[0], Country=parts[1])
        else:
            loc = uno.createUnoStruct("com.sun.star.lang.Locale", Language=parts[0])
        return bi, loc
    except Exception as e:
        _grammar_diag.warning("[grammar] _get_break_iterator_and_locale failed: %s", e)
        return None, None


_GRAMMAR_JSON_RE = re.compile(r"\{[\s\S]*\}\s*$")


def parse_grammar_json(content: str) -> list[dict[str, Any]]:
    """Parse assistant message into a list of error dicts (wrong, correct, type, reason)."""
    if not content or not content.strip():
        return []
    text = content.strip()
    m = _GRAMMAR_JSON_RE.search(text)
    if m:
        text = m.group(0)
    data: Any = safe_json_loads(text)
    if not isinstance(data, Mapping):
        try:
            _grammar_diag.info("[grammar] parse_grammar_json: attempting json_repair")
            data = json_repair.repair_json(text, return_objects=True)
        except Exception as e:
            _grammar_diag.warning("[grammar] parse_grammar_json: json_repair failed: %s", e)
            return []
    if not isinstance(data, Mapping):
        return []
    root = cast("Mapping[str, Any]", data)
    raw = root.get("errors")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        row = cast("Mapping[str, Any]", item)
        wrong = row.get("wrong")
        correct = row.get("correct")
        if wrong is None or correct is None:
            continue
        out.append({"wrong": str(wrong), "correct": str(correct), "type": str(row.get("type", "grammar")), "reason": str(row.get("reason", ""))})
    return out


def _tokenize(text: str, break_iterator: Any = None, locale: Any = None) -> list[str]:
    """Split text into a list of word tokens and non-word (punctuation/whitespace) tokens.
    Uses LO's BreakIterator if available, otherwise falls back to a regex."""
    if not text:
        return []

    if break_iterator and locale:
        try:
            tokens = []
            start = 0
            while start < len(text):
                # 0 = WordType.ANY_WORD
                res = break_iterator.getWordBoundary(text, start, locale, 0, True)
                if res.endPos <= start:
                    break
                tokens.append(text[res.startPos : res.endPos])
                start = res.endPos

            # If the iterator perfectly covered the text, return the tokens.
            if sum(len(t) for t in tokens) == len(text):
                return tokens
        except Exception as e:
            _grammar_diag.debug("[grammar] _tokenize: BreakIterator failed: %s", e)

    return re.findall(r"\w+|\W+", text)


def anchor_wrong_in_window(window: str, wrong: str, search_pos: int, *, wrong_idx: int | None = None) -> int | None:
    """Find ``wrong`` in ``window`` starting at ``search_pos``, with ordered-scan fallback.

    Returns relative offset within ``window``, or ``None`` if no acceptable match.
    """
    if not wrong:
        return None
    rel = window.find(wrong, search_pos)
    if rel >= 0:
        return rel
    rel = window.find(wrong)
    if rel < 0:
        return None
    # Out-of-order vs ordered scan: only accept a global match if it does not lie
    # **before** the next expected position (avoids anchoring to an earlier duplicate).
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

    bi, locale = _get_break_iterator_and_locale(ctx, loc_key)

    # Track last matched position to handle multiple occurrences of the same text.
    # We assume the LLM returns errors in the order they appear.
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

        # Advance search_pos for the next item
        search_pos = rel + 1

        if correct:
            # Suffix overlap (forward expansion)
            suffix = full_text[pos + length :]
            t_c = _tokenize(correct, bi, locale)
            t_s = _tokenize(suffix, bi, locale)
            for k in range(min(len(t_c), len(t_s)), 0, -1):
                if t_c[-k:] == t_s[:k]:
                    overlap_len = sum(len(t) for t in t_c[-k:])
                    length += overlap_len
                    break

            # Prefix overlap (backward expansion)
            prefix = full_text[:pos]
            t_p = _tokenize(prefix, bi, locale)
            for k in range(min(len(t_p), len(t_c)), 0, -1):
                if t_p[-k:] == t_c[:k]:
                    overlap_len = sum(len(t) for t in t_p[-k:])
                    pos -= overlap_len
                    length += overlap_len
                    break

            # No-op filter
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


# ---------------------------------------------------------------------------
# Sentence splitter (pure Python, no UNO)
# ---------------------------------------------------------------------------

# Split on sentence-ending punctuation followed by whitespace.
# Uses the same multilingual terminators as the proofreader's _looks_complete_sentence.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…؟。！？।])\s+")

# Words before ``.`` where BreakIterator often splits too early; extend merge (case-insensitive).
# Avoid very short ambiguous tokens (e.g. "no", "al") — those stay on the <=3-char Title-case rule.
_ABBREV_DOT_WORDS = frozenset(
    {
        "approx",
        "assoc",
        "dept",
        "prof",
        "univ",
        "ext",
        "fig",
        "vol",
        "misc",
        "vs",
        "etc",
        "mr",
        "mrs",
        "dr",
        "ms",
    }
)


def _word_before_period_is_abbrev(word: str) -> bool:
    if not word:
        return False
    if word.lower() in _ABBREV_DOT_WORDS:
        return True
    # Short title-case token (Mr., Dr.) without listing every honorific.
    return 0 < len(word) <= 3 and word[0].isupper()


def extend_through_trailing_whitespace(text: str, end_pos: int) -> int:
    """Return index after ``end_pos`` including any following whitespace on the same line."""
    ws_end = end_pos
    while ws_end < len(text) and text[ws_end].isspace():
        ws_end += 1
    return ws_end


def split_into_sentences(ctx: Any, locale_key: str, text: str) -> list[tuple[int, str]]:
    """Split *text* into ``(start_offset, sentence_text)`` pairs.

    For Thai, Lao, and Khmer, splits on whitespace.
    For other languages, uses LibreOffice's BreakIterator with an abbreviation heuristic.
    """
    if not text or not text.strip():
        return []

    if locale_key.startswith(("th", "lo", "km")):
        # Thai, Lao, Khmer: spaces indicate phrase/sentence boundaries.
        # Include the delimiter whitespace in each segment so the LLM can flag spacing issues.
        _SPACE_RE = re.compile(r"\s+")
        result: list[tuple[int, str]] = []
        last = 0
        for m in _SPACE_RE.finditer(text):
            seg = text[last : m.start()]
            ws = text[m.start() : m.end()]
            if seg:
                result.append((last, seg + ws))
            last = m.end()
        tail = text[last:]
        if tail:
            result.append((last, tail))
        return result or [(0, text)]

    # For other languages, try to use LibreOffice BreakIterator
    bi, locale = _get_break_iterator_and_locale(ctx, locale_key)

    if not bi or not locale:
        # Fallback to regex if LO is unavailable; include following whitespace in each segment.
        result = []
        last = 0
        for m in _SENTENCE_SPLIT_RE.finditer(text):
            seg = text[last : m.start()]
            ws = text[m.start() : m.end()]
            if seg:
                result.append((last, seg + ws))
            last = m.end()
        tail = text[last:]
        if tail:
            result.append((last, tail))
        return result or [(0, text)]

    pos = 0
    sentences = []

    while pos < len(text):
        end_pos = bi.endOfSentence(text, pos, locale)

        if end_pos <= pos:
            # Prevent infinite loop if endOfSentence gets stuck
            end_pos = len(text)

        # Abbreviation heuristic: merge BreakIterator splits that land after a likely abbreviation.
        while end_pos < len(text):
            i = end_pos - 1
            while i >= pos and text[i].isspace():
                i -= 1
            if i >= pos and text[i] == ".":
                j = i - 1
                while j >= pos and not text[j].isspace() and text[j] not in ".!?":
                    j -= 1
                word = text[j + 1 : i]
                if _word_before_period_is_abbrev(word):
                    next_end = bi.endOfSentence(text, end_pos, locale)
                    if next_end > end_pos:
                        end_pos = next_end
                        continue
            break

        # Include trailing whitespace after the sentence so the LLM can flag double spaces etc.
        ws_end = extend_through_trailing_whitespace(text, end_pos)

        sentences.append((pos, text[pos:ws_end]))
        pos = ws_end

    return sentences or [(0, text)]


# --- Sentence-level cache (simple, text-based, no positions)
# Keyed by locale + sentence fingerprint. Normalization:
# - trailing whitespace is stripped (existing behavior)
# - any punctuation after the *first* sentence terminator is ignored for the cache key
#   ("Hello." and "Hello..." share a key; "Hello?" and "Hello?..." share one;
#    but "Hello?" and "Hello." remain distinct as the first terminator is significant).
# Errors are relative to the start of that (canonical) sentence (offset 0).
#
# Additional behavior for incomplete sentences (no terminator):
# - On cache_put_sentence, we scan up to MAX_RECENT_INCOMPLETE_SCAN=10
#   *newest* entries (OrderedDict end). If we find an incomplete strict-prefix
#   predecessor for the same locale, we evict it. This collapses long typing
#   chains ("The qu", "The qui", ..., "The quick brown fox") into 1 LRU slot.
# - Complete sentences are protected and never evicted by this rule.
# - This prevents LRU churn while keeping put cost tiny (bounded scan).
# - Complete/cross-document sentences continue to reuse perfectly.

_SENTENCE_CACHE: collections.OrderedDict[str, tuple[str, str, bool, list[dict[str, Any]]]] = collections.OrderedDict()


def _clip_errors_to_canonical_length(errors: list[dict[str, Any]], canonical_len: int) -> list[dict[str, Any]]:
    """Clip or drop errors that reference positions beyond the canonical sentence length.

    This prevents errors that targeted only the redundant trailing punctuation
    (e.g. the extra dots in "Hello....") from being stored against the shorter
    canonical form.
    """
    clipped: list[dict[str, Any]] = []
    for e in errors:
        start = e.get("n_error_start", 0)
        if start >= canonical_len:
            continue
        length = e.get("n_error_length", 0)
        effective_len = min(length, canonical_len - start)
        if effective_len <= 0:
            continue
        if effective_len != length:
            e = dict(e)  # shallow copy is sufficient here
            e["n_error_length"] = effective_len
        clipped.append(e)
    return clipped


def make_sentence_key(locale_key: str, sentence: str) -> str:
    """Cache key for a specific sentence text (locale + fingerprint).

    Uses _normalize_for_sentence_cache so that ``'Hello.'`` and ``'Hello...'``
    share the same cache entry, and ``'Hello?'`` and ``'Hello?... '`` share one,
    but the first terminator remains semantically significant.
    """
    return f"{sentence_cache_key_prefix(locale_key)}{sentence_identity_fp(sentence)}"


def cache_get_sentence(locale_key: str, sentence: str) -> list[dict[str, Any]] | None:
    """Return cached errors for this exact sentence (relative to sentence start = 0).

    Uses _normalize_for_sentence_cache before fingerprint comparison so that
    additional trailing punctuation after the first terminator is ignored.
    """
    key = make_sentence_key(locale_key, sentence)
    with _CACHE_LOCK:
        hit = _SENTENCE_CACHE.get(key)
        if not hit:
            return None
        cached_fp, _canon, _is_complete, errors = hit
        if cached_fp != sentence_identity_fp(sentence):
            return None
        _SENTENCE_CACHE.move_to_end(key)
        return list(errors)  # return copy


def cache_put_sentence(locale_key: str, sentence: str, errors: list[dict[str, Any]]) -> None:
    """Cache errors for this sentence text (errors must have offsets relative to sentence start).

    Uses _normalize_for_sentence_cache + clipping so additional trailing
    punctuation after the first terminator does not affect the cache key or
    produce invalid offsets.

    For incomplete sentences (no terminator), we also perform a cheap
    newest-first scan (max MAX_RECENT_INCOMPLETE_SCAN=10 entries) to evict
    any recent incomplete strict-prefix predecessors for the same locale.
    This prevents LRU churn from a user typing a long sentence one character
    at a time. Complete sentences are never evicted by this logic.
    """
    canon = _normalize_for_sentence_cache(sentence)
    fp = fingerprint_for_text(canon)
    # Same assembly as ``make_sentence_key`` (single normalize — fp matches ``sentence_identity_fp``).
    key = f"{sentence_cache_key_prefix(locale_key)}{fp}"
    clipped = _clip_errors_to_canonical_length(errors, len(canon))
    is_complete = _is_complete_sentence(canon)

    with _CACHE_LOCK:
        _SENTENCE_CACHE[key] = (fp, canon, is_complete, [dict(e) for e in clipped])
        _SENTENCE_CACHE.move_to_end(key)

        # Prefix compaction for incomplete sentences. Scans newest-first
        # (OrderedDict end) and stops early. Cost is bounded and tiny.
        if not is_complete:
            scan_count = 0
            to_remove: list[str] = []
            prefix = sentence_cache_key_prefix(locale_key)
            # Snapshot to avoid modification-during-iteration
            for k, v in list(_SENTENCE_CACHE.items())[::-1]:
                if scan_count >= MAX_RECENT_INCOMPLETE_SCAN:
                    break
                if not k.startswith(prefix):
                    continue
                _other_fp, other_canon, other_complete, _ = v
                if should_evict_incomplete_prefix_predecessor(other_complete=other_complete, other_canon=other_canon, new_canon=canon):
                    to_remove.append(k)
                    break  # only the most recent matching predecessor
                scan_count += 1
            for k in to_remove:
                _SENTENCE_CACHE.pop(k, None)

        while len(_SENTENCE_CACHE) > MAX_CACHE_SIZE:
            _SENTENCE_CACHE.popitem(last=False)


def clear_sentence_cache() -> None:
    """Clear sentence cache (for tests)."""
    with _CACHE_LOCK:
        _SENTENCE_CACHE.clear()


# ---------------------------------------------------------------------------
# Work-queue dedup (pure Python, no UNO)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrammarWorkItem:
    """One queued grammar job (defined here so dedup tests avoid UNO imports)."""

    ctx: Any
    full_text: str
    n_start: int
    n_end: int
    grammar_bcp47: str
    partial_sentence: bool
    doc_id: str
    inflight_key: str
    enqueue_seq: int
    # Main-thread sentence text from doProofreading; when set, worker skips split_into_sentences
    # on the slice so substring BreakIterator cannot disagree with cache keys (see _run_llm_and_cache).
    proofread_sentence_text: str = ""


def deduplicate_grammar_batch(batch: list[GrammarWorkItem]) -> list[GrammarWorkItem]:
    """Return one queue item per ``inflight_key``, keeping the highest ``enqueue_seq``."""
    # --- Cross-sentence prefix bug (fixed): older code had a *second* pass that grouped
    # by (doc_id, locale) and dropped slice A if slice B was a string-prefix extension
    # of A (newest enqueue_seq wins). That wrongly dropped sentence 1 when sentence 2's
    # text started with sentence 1's text (e.g. "No." vs "No problem today.") — different
    # inflight_key values, unrelated timelines. One sentence while typing = one key.
    #
    # Do not add cross-key slice-text prefix logic here; tail-replace + this loop suffice.
    #
    # Alternatives if you redesign: (1) prefix-newest-wins restricted to *same*
    # inflight_key only — usually redundant after this map; (2) span-aware dedup using
    # overlapping [n_start,n_end); (3) keep distinct-key slices independent (current).
    # Regression: test_two_sentences_string_prefix_collision_both_survive.
    best_by_key: dict[str, GrammarWorkItem] = {}
    for item in batch:
        prev = best_by_key.get(item.inflight_key)
        # Same physical sentence / typing line: inflight_key matches → keep newer snapshot only.
        if prev is None or item.enqueue_seq > prev.enqueue_seq:
            best_by_key[item.inflight_key] = item
        elif prev is not None and item.enqueue_seq < prev.enqueue_seq:
            _grammar_diag.info("[grammar] queue dedup: dropped older same-key item seq=%s key=%s (newer seq=%s kept)", item.enqueue_seq, item.inflight_key, prev.enqueue_seq)
    return list(best_by_key.values())
