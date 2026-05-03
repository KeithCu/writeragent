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
from plugin.modules.writer.grammar_locale_registry import (
    GRAMMAR_REGISTRY_LOCALE_TAGS as _GRAMMAR_REGISTRY_LOCALE_TAGS,
)

log = logging.getLogger(__name__)
_grammar_diag = logging.getLogger("writeragent.grammar")

# Re-export: hyphenated tags for ``LinguisticWriterAgentGrammar.xcu`` and tests (see
# ``grammar_locale_registry`` — same list as ``plugin/locales`` + ``en`` variants).
GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = _GRAMMAR_REGISTRY_LOCALE_TAGS

_CACHE_LOCK = threading.Lock()
_ignored_rules: set[str] = set()
MAX_CACHE_SIZE = 512


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
    match = re.search(r'^(.*?[.!?…。！？])([.!?…。！？]*)$', s)
    if match:
        return match.group(1)
    return s


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
        out.append(
            {
                "wrong": str(wrong),
                "correct": str(correct),
                "type": str(row.get("type", "grammar")),
                "reason": str(row.get("reason", "")),
            }
        )
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
                tokens.append(text[res.startPos:res.endPos])
                start = res.endPos
            
            # If the iterator perfectly covered the text, return the tokens.
            if sum(len(t) for t in tokens) == len(text):
                return tokens
        except Exception as e:
            _grammar_diag.debug("[grammar] _tokenize: BreakIterator failed: %s", e)

    return re.findall(r'\w+|\W+', text)


def normalize_errors_for_text(
    full_text: str,
    n_slice_start: int,
    n_slice_end: int,
    items: Iterable[dict[str, Any]],
    ignored: set[str] | None = None,
    ctx: Any = None,
    loc_key: str | None = None,
) -> list[NormalizedProofError]:
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
        if not wrong:
            continue
        
        rel = window.find(wrong, search_pos)
        if rel < 0:
            # If not found after search_pos, try finding from the beginning of the window
            # but log it as a possible out-of-order issue.
            rel = window.find(wrong)
            if rel < 0:
                continue
        
        pos = slice_start + rel
        length = len(wrong)
        if length <= 0:
            continue
        
        # Advance search_pos for the next item
        search_pos = rel + 1

        if correct:
            # Suffix overlap (forward expansion)
            suffix = full_text[pos + length:]
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
            results.append(
                NormalizedProofError(
                    n_error_start=pos,
                    n_error_length=length,
                    suggestions=sugg,
                    short_comment=short[:500],
                    full_comment=full[:2000],
                    rule_identifier=rule_id,
                )
            )
        except Exception as e:
            _grammar_diag.warning(
                "[grammar] normalize_errors_for_text: skipped item idx=%s: %s",
                idx,
                e,
                exc_info=True,
            )
    return results


# ---------------------------------------------------------------------------
# Sentence splitter (pure Python, no UNO)
# ---------------------------------------------------------------------------

# Split on sentence-ending punctuation followed by whitespace.
# Uses the same multilingual terminators as the proofreader's _looks_complete_sentence.
_SENTENCE_SPLIT_RE = re.compile(
    r'(?<=[.!?…؟。！？।])\s+'
)


def split_into_sentences(ctx: Any, locale_key: str, text: str) -> list[tuple[int, str]]:
    """Split *text* into ``(start_offset, sentence_text)`` pairs.

    For Thai, Lao, and Khmer, splits on whitespace.
    For other languages, uses LibreOffice's BreakIterator with an abbreviation heuristic.
    """
    if not text or not text.strip():
        return []

    if locale_key.startswith(("th", "lo", "km")):
        # Thai, Lao, Khmer: spaces indicate phrase/sentence boundaries
        _SPACE_RE = re.compile(r'\s+')
        result: list[tuple[int, str]] = []
        last = 0
        for m in _SPACE_RE.finditer(text):
            seg = text[last:m.start()]
            if seg:
                result.append((last, seg))
            last = m.end()
        tail = text[last:]
        if tail:
            result.append((last, tail))
        return result or [(0, text)]

    # For other languages, try to use LibreOffice BreakIterator
    bi, locale = _get_break_iterator_and_locale(ctx, locale_key)
    
    if not bi or not locale:
        # Fallback to regex if LO is unavailable
        result = []
        last = 0
        for m in _SENTENCE_SPLIT_RE.finditer(text):
            seg = text[last:m.start()]
            if seg:
                result.append((last, seg))
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
            
        # Abbreviation heuristic: merge BreakIterator splits that land
        # after a likely abbreviation (e.g. "Mr.", "Dr.", "vs.").
        # Only short words (<=3 chars) qualify to avoid false positives
        # on proper nouns and acronyms like "USA.", "Tom.", "NYC.".
        while end_pos < len(text):
            i = end_pos - 1
            while i >= pos and text[i].isspace():
                i -= 1
            if i >= pos and text[i] == '.':
                j = i - 1
                while j >= pos and not text[j].isspace() and text[j] not in '.!?':
                    j -= 1
                word = text[j+1:i]
                if 0 < len(word) <= 3 and word[0].isupper():
                    next_end = bi.endOfSentence(text, end_pos, locale)
                    if next_end > end_pos:
                        end_pos = next_end
                        continue
            break
            
        sentences.append((pos, text[pos:end_pos]))
        pos = end_pos
        
        # Skip trailing whitespace to the next sentence start
        while pos < len(text) and text[pos].isspace():
            pos += 1
            
    return sentences or [(0, text)]



# --- Sentence-level cache (simple, text-based, no positions)
# Keyed by locale + sentence fingerprint. Normalization:
# - trailing whitespace is stripped (existing behavior)
# - any punctuation after the *first* sentence terminator is ignored for the cache key
#   ("Hello." and "Hello..." share a key; "Hello?" and "Hello?..." share one;
#    but "Hello?" and "Hello." remain distinct as the first terminator is significant).
# Errors are relative to the start of that (canonical) sentence (offset 0).
# This fulfills the requirement that semantically equivalent sentence text
# always has the same errors, regardless of document position.

_SENTENCE_CACHE: collections.OrderedDict[str, tuple[str, list[dict[str, Any]]]] = collections.OrderedDict()


def _clip_errors_to_canonical_length(
    errors: list[dict[str, Any]], canonical_len: int
) -> list[dict[str, Any]]:
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
    fp = fingerprint_for_text(_normalize_for_sentence_cache(sentence))
    return f"sent|{locale_key}|{fp}"


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
        cached_fp, errors = hit
        if cached_fp != fingerprint_for_text(_normalize_for_sentence_cache(sentence)):
            return None
        _SENTENCE_CACHE.move_to_end(key)
        return list(errors)  # return copy


def cache_put_sentence(locale_key: str, sentence: str, errors: list[dict[str, Any]]) -> None:
    """Cache errors for this sentence text (errors must have offsets relative to sentence start).

    Uses _normalize_for_sentence_cache + clipping so additional trailing
    punctuation after the first terminator does not affect the cache key or
    produce invalid offsets.
    """
    canon = _normalize_for_sentence_cache(sentence)
    fp = fingerprint_for_text(canon)
    key = f"sent|{locale_key}|{fp}"
    clipped = _clip_errors_to_canonical_length(errors, len(canon))
    with _CACHE_LOCK:
        _SENTENCE_CACHE[key] = (fp, [dict(e) for e in clipped])  # deep enough copy
        _SENTENCE_CACHE.move_to_end(key)
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
    """One unit of grammar work to be processed by the sequential queue worker.

    Lives here (not in ``ai_grammar_proofreader``) so the dedup logic can be
    unit-tested without UNO imports.
    """

    ctx: Any
    full_text: str
    n_start: int
    n_end: int
    grammar_bcp47: str
    partial_sentence: bool
    doc_id: str
    inflight_key: str
    enqueue_seq: int


def deduplicate_grammar_batch(
    batch: list[GrammarWorkItem],
) -> list[GrammarWorkItem]:
    """Remove stale items from a batch using newest-first semantics.

    Within each ``(doc_id, locale)`` group, prefix-related conflicts are resolved
    in favor of the newest item (highest ``enqueue_seq``), regardless of text
    length. This avoids cases where an older longer text wins over a newer
    shorter text from the same typing timeline.

    Additionally, if two items share the same ``inflight_key`` (same document
    identifier + locale), the one with the lower ``enqueue_seq`` is dropped.
    """
    from collections import defaultdict

    # Step 1: supersede by sequence (same inflight_key → keep latest only)
    best_by_key: dict[str, GrammarWorkItem] = {}
    for item in batch:
        prev = best_by_key.get(item.inflight_key)
        if prev is None or item.enqueue_seq > prev.enqueue_seq:
            best_by_key[item.inflight_key] = item
        elif prev is not None and item.enqueue_seq < prev.enqueue_seq:
            _grammar_diag.info(
                "[grammar] queue dedup: dropped older same-key item seq=%s key=%s (newer seq=%s kept)",
                item.enqueue_seq,
                item.inflight_key,
                prev.enqueue_seq,
            )
    unique = list(best_by_key.values())

    # Step 2: prefix dedup within (doc_id, locale) groups (newest-first)
    groups: dict[tuple[str, str], list[GrammarWorkItem]] = defaultdict(list)
    for item in unique:
        groups[(item.doc_id, item.grammar_bcp47)].append(item)

    result: list[GrammarWorkItem] = []
    for _key, group in groups.items():
        # Newest first: prefix-related conflicts keep the most recent request.
        group.sort(key=lambda x: x.enqueue_seq, reverse=True)
        kept_texts: list[str] = []
        kept_seqs: list[int] = []
        for item in group:
            slice_txt = item.full_text[item.n_start : item.n_end]
            # Drop if this text conflicts by prefix relation with any newer kept text.
            if any(
                (kept.startswith(slice_txt) or slice_txt.startswith(kept))
                and kept != slice_txt
                for kept in kept_texts
            ):
                conflict_idx = next(
                    i
                    for i, kept in enumerate(kept_texts)
                    if (kept.startswith(slice_txt) or slice_txt.startswith(kept))
                    and kept != slice_txt
                )
                newer_seq = kept_seqs[conflict_idx]
                _grammar_diag.info(
                    "[grammar] queue dedup: dropped older prefix-related item seq=%s len=%s "
                    "(newer seq=%s kept)",
                    item.enqueue_seq,
                    len(slice_txt),
                    newer_seq,
                )
                continue
            kept_texts.append(slice_txt)
            kept_seqs.append(item.enqueue_seq)
            result.append(item)
    return result
