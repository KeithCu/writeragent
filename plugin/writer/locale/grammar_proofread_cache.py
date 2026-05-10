# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sentence-level LRU cache and fingerprinting for grammar proofreading.

Sentence *scheduling* (mapping LibreOffice ``doProofreading`` ranges to sentence spans)
lives in ``grammar_proofread_text`` next to ``split_into_sentences``: importing the
splitter here would circular-import (this module is already imported by ``grammar_proofread_text``).
"""

from __future__ import annotations

import collections
import hashlib
import re
import threading
from typing import Any

_CACHE_LOCK = threading.Lock()
_ignored_rules: set[str] = set()
MAX_CACHE_SIZE = 2048
# Limit how many recent entries we scan for incomplete-sentence prefix
# compaction on each cache_put_sentence. 10 is a good balance between
# effectiveness (catches typical typing chains) and CPU (few memory touches).
MAX_RECENT_INCOMPLETE_SCAN = 10

# Sentence-ending punctuation by script — single source for native grammar (proofreader
# gating, worker partial prompts, and cache LRU incomplete-prefix eviction).
# Matches Unicode 15.1 Sentence_Terminal (STerm); PropList.txt in Unicode UCD releases.
# fmt: off
GRAMMAR_SENTENCE_TERMINATORS: frozenset[str] = frozenset((
    "!", ".", "?",              # ASCII
    "…",                        # Horizontal ellipsis
    "։",                        # Armenian full stop
    "؟", "۔",                   # Arabic question mark / full stop
    "܀", "܁", "܂",              # Syriac
    "߹",                        # NKo exclamation mark
    "।", "॥",                   # Devanagari danda / double danda
    "၊", "။",                   # Myanmar
    "።", "፧", "፨",              # Ethiopic
    "᙮",                        # Canadian syllabics full stop
    "᠃", "᠉",                   # Mongolian full stop / Manchu full stop
    "᥄", "᥅",                   # Limbu
    "᪨", "᪩", "᪪", "᪫",        # Tai Tham
    "᭚", "᭛", "᭞", "᭟", "᭽", "᭾",  # Balinese
    "᰻",                        # Lepcha
    "᱾", "᱿",                   # Ol Chiki
    "‼", "‽", "⁇", "⁈", "⁉",   # Double/combined punctuation
    "⳹", "⳺", "⳻", "⳾",         # Coptic
    "⸮", "⸼",                   # Reversed question mark / stenographic full stop
    "。",                        # Ideographic full stop
    "꓿",                        # Lisu
    "꘎", "꘏",                   # Vai
    "꛳", "꛷",                   # Bamum
    "︑", "︒", "︕", "︖", "︙",  # Presentation forms (vertical)
    "﹒", "﹖", "﹗",             # Small forms
    "！", "．", "？",             # Fullwidth
    "｡",                        # Halfwidth ideographic full stop
    "𑅃",                        # Chakma question mark
    "𖫵",                        # Bassa Vah full stop
    "𖺘", "𖺚",                  # Medefaidrin
    "𛲟",                        # Duployan
    "𝪈",                        # Signwriting full stop
    "𞥞", "𞥟",                  # Adlam
))

# Characters skipped when scanning backward for the sentence end: brackets, closing quotes,
# and similar trail the period.
# Mostly Unicode closing punctuation (Pe/Pf); `"` `'` `>` added for prose that omits curly quotes.
# Regenerate Pe/Pf subset after a Unicode update:
#   import sys, unicodedata
#   chars = sorted(chr(cp) for cp in range(sys.maxunicode + 1)
#                  if unicodedata.category(chr(cp)) in ('Pe', 'Pf'))
#   print(frozenset(chars) | frozenset('"\'>'))

GRAMMAR_TRAILING_CLOSERS: frozenset[str] = frozenset((
    # ASCII Pe
    ")", "]", "}",
    # Pf: closing quotes (», ›, curly " ', and scholarly brackets)
    "»", "’", "”", "›", "⸃", "⸅", "⸊", "⸍", "⸝", "⸡",
    # CJK / fullwidth / halfwidth Pe
    "〉", "》", "」", "』", "】", "〕", "〗", "〙", "〛", "〞", "〟",
    "﴾", "︘", "︶", "︸", "︺", "︼", "︾", "﹀", "﹂", "﹄", "﹈",
    "﹚", "﹜", "﹞", "）", "］", "｝", "｠", "｣",
    # Latin / misc Pe (Tibetan, Ogham, sub/superscript, math, ornamental)
    "༻", "༽", "᚜",
    "⁆", "⁾", "₎", "⌉", "⌋",
    "❩", "❫", "❭", "❯", "❱", "❳", "❵",
    "⟆", "⟧", "⟩", "⟫", "⟭", "⟯",
    "⦄", "⦆", "⦈", "⦊", "⦌", "⦎", "⦐", "⦒", "⦔", "⦖", "⦘",
    "⧙", "⧛", "⧽",
    "⸣", "⸥", "⸧", "⸩",
    "⹖", "⹘", "⹚", "⹜",
    # ASCII informal closers (not Pe/Pf in Unicode but common in prose)
    '"', "'", ">",
))
# fmt: on

_SENTENCE_CACHE: collections.OrderedDict[str, tuple[str, str, bool, list[dict[str, Any]]]] = collections.OrderedDict()


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

    The regex below matches a **subset** of ``GRAMMAR_SENTENCE_TERMINATORS`` (common
    scripts only). ``looks_complete_sentence`` uses the full STerm set for eviction
    vs incomplete-prefix compaction — keys may still normalize via this narrower pattern.
    """
    s = text.rstrip()
    if not s:
        return s
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


def last_meaningful_char(text: str) -> str:
    """Return the last non-closer character (skipping quotes, brackets, etc.)."""
    if not text:
        return ""
    for ch in reversed(text.rstrip()):
        if ch in GRAMMAR_TRAILING_CLOSERS:
            continue
        return ch
    return ""


def looks_complete_sentence(text: str) -> bool:
    """True if ``text`` ends (after skipping trailing closers) with a sentence terminal."""
    return last_meaningful_char(text) in GRAMMAR_SENTENCE_TERMINATORS


def _is_complete_sentence(canon: str) -> bool:
    """True if the canonical normalized text ends with a sentence terminator (cache eviction)."""
    return looks_complete_sentence(canon)


def _clip_errors_to_canonical_length(errors: list[dict[str, Any]], canonical_len: int) -> list[dict[str, Any]]:
    """Clip or drop errors that reference positions beyond the canonical sentence length."""
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
            e = dict(e)
            e["n_error_length"] = effective_len
        clipped.append(e)
    return clipped


def make_sentence_key(locale_key: str, sentence: str) -> str:
    """Cache key for a specific sentence text (locale + fingerprint)."""
    return f"{sentence_cache_key_prefix(locale_key)}{sentence_identity_fp(sentence)}"


def cache_get_sentence(locale_key: str, sentence: str) -> list[dict[str, Any]] | None:
    """Return cached errors for this exact sentence (relative to sentence start = 0)."""
    key = make_sentence_key(locale_key, sentence)
    with _CACHE_LOCK:
        hit = _SENTENCE_CACHE.get(key)
        if not hit:
            return None
        cached_fp, _canon, _is_complete, errors = hit
        if cached_fp != sentence_identity_fp(sentence):
            return None
        _SENTENCE_CACHE.move_to_end(key)
        return list(errors)


def cache_put_sentence(locale_key: str, sentence: str, errors: list[dict[str, Any]]) -> None:
    """Cache errors for this sentence text (errors must have offsets relative to sentence start)."""
    canon = _normalize_for_sentence_cache(sentence)
    fp = fingerprint_for_text(canon)
    key = f"{sentence_cache_key_prefix(locale_key)}{fp}"
    clipped = _clip_errors_to_canonical_length(errors, len(canon))
    is_complete = _is_complete_sentence(canon)

    with _CACHE_LOCK:
        _SENTENCE_CACHE[key] = (fp, canon, is_complete, [dict(e) for e in clipped])
        _SENTENCE_CACHE.move_to_end(key)

        if not is_complete:
            scan_count = 0
            to_remove: list[str] = []
            prefix = sentence_cache_key_prefix(locale_key)
            for k, v in list(_SENTENCE_CACHE.items())[::-1]:
                if scan_count >= MAX_RECENT_INCOMPLETE_SCAN:
                    break
                if not k.startswith(prefix):
                    continue
                _other_fp, other_canon, other_complete, _ = v
                if should_evict_incomplete_prefix_predecessor(other_complete=other_complete, other_canon=other_canon, new_canon=canon):
                    to_remove.append(k)
                    break
                scan_count += 1
            for k in to_remove:
                _SENTENCE_CACHE.pop(k, None)

        while len(_SENTENCE_CACHE) > MAX_CACHE_SIZE:
            _SENTENCE_CACHE.popitem(last=False)


def clear_sentence_cache() -> None:
    """Clear sentence cache (for tests)."""
    with _CACHE_LOCK:
        _SENTENCE_CACHE.clear()
