# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sentence-level LRU cache for grammar proofreading (ignore rules, fingerprint keys).

Sentence-boundary tables and ``looks_complete_sentence`` live in ``grammar_proofread_locale``.
"""

from __future__ import annotations

from typing import Any

from .grammar_persistence import get_persistence, grammar_registry
from .grammar_proofread_locale import (
    GRAMMAR_CACHE_NORMALIZATION_RE,
    MAX_CACHE_SIZE,
    MAX_RECENT_INCOMPLETE_SCAN,
    fingerprint_for_text,
    looks_complete_sentence,
)

def cache_clear(ctx: Any | None = None, doc_id: str | None = None) -> None:
    """Clear proofreading cache (e.g. tests)."""
    grammar_registry.clear_all(ctx)
    if doc_id and ctx:
        p = get_persistence(ctx, doc_id)
        if p:
            p.clear()


def ignore_rules_clear() -> None:
    with grammar_registry.lock:
        grammar_registry.ignored_rules.clear()


def ignore_rule_add(rule_id: str) -> None:
    with grammar_registry.lock:
        grammar_registry.ignored_rules.add(str(rule_id))


def ignored_rules_snapshot() -> set[str]:
    with grammar_registry.lock:
        return set(grammar_registry.ignored_rules)


def _normalize_for_sentence_cache(text: str) -> str:
    """Canonical form for cache key that preserves first sentence terminator.

    - rstrip() whitespace (preserves existing "Hello." vs "Hello. " behavior).
    - Keep everything up to and including the *first* sentence terminator.
    - Ignore any additional trailing punctuation after the first terminator.
    - This makes "Hello." and "Hello..." share a cache entry, and
      "Hello?" and "Hello?..." share one, but "Hello?" and "Hello." remain distinct.

    The regex below matches all sentence terminators in ``grammar_proofread_locale.GRAMMAR_SENTENCE_TERMINATORS``
    (the full Unicode STerm set). Both ``looks_complete_sentence`` and cache key normalization
    are fully aligned and use the exact same character set.
    """
    s = text.rstrip()
    if not s:
        return s
    match = GRAMMAR_CACHE_NORMALIZATION_RE.search(s)
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


def _populate_memory_cache_only(locale_key: str, sentence: str, errors: list[dict[str, Any]]) -> tuple[str, str, bool, str, list[dict[str, Any]]]:
    """Internal: populate memory cache only, no persistence, no compaction.

    Used by cache_get_sentence to warm cache from persistence without side effects.
    Returns (fp, canon, is_complete, key, clipped_errors).
    """
    canon = _normalize_for_sentence_cache(sentence)
    fp = fingerprint_for_text(canon)
    key = f"{sentence_cache_key_prefix(locale_key)}{fp}"
    clipped = _clip_errors_to_canonical_length(errors, len(canon))
    is_complete = _is_complete_sentence(canon)
    cloned_errors = [dict(e) for e in clipped]

    with grammar_registry.lock:
        grammar_registry.sentence_cache[key] = (fp, canon, is_complete, cloned_errors)
        grammar_registry.sentence_cache.move_to_end(key)
        while len(grammar_registry.sentence_cache) > MAX_CACHE_SIZE:
            grammar_registry.sentence_cache.popitem(last=False)

    return fp, canon, is_complete, key, cloned_errors


def cache_get_sentence(locale_key: str, sentence: str, ctx: Any | None = None, doc_id: str | None = None) -> list[dict[str, Any]] | None:
    """Return cached errors for this exact sentence (relative to sentence start = 0)."""
    key = make_sentence_key(locale_key, sentence)
    with grammar_registry.lock:
        hit = grammar_registry.sentence_cache.get(key)
        if hit:
            cached_fp, _canon, _is_complete, errors = hit
            if cached_fp == sentence_identity_fp(sentence):
                grammar_registry.sentence_cache.move_to_end(key)
                return list(errors)

    if ctx and doc_id:
        p = get_persistence(ctx, doc_id)
        if p:
            fp = sentence_identity_fp(sentence)
            persisted = p.get(fp)
            if persisted is not None:
                # Warm memory cache
                _populate_memory_cache_only(locale_key, sentence, persisted)
                return list(persisted)

    return None


def cache_put_sentence(
    locale_key: str,
    sentence: str,
    errors: list[dict[str, Any]],
    ctx: Any | None = None,
    doc_id: str | None = None,
) -> None:
    """Cache errors for this sentence text (errors must have offsets relative to sentence start)."""
    # Always populate memory cache for current session speed
    fp, canon, is_complete, key, clipped_errors = _populate_memory_cache_only(locale_key, sentence, errors)

    if ctx and doc_id:
        p = get_persistence(ctx, doc_id)
        if p:
            p.put(fp, locale_key, [dict(e) for e in clipped_errors])
        # Note: document mode skips incomplete-sentence prefix compaction logic (scans _SENTENCE_CACHE only)
        # but we still performed _populate_memory_cache_only above.
        return

    if not is_complete:
        with grammar_registry.lock:
            prefix = sentence_cache_key_prefix(locale_key)
            scan_count = 0
            to_remove: list[str] = []
            # Newest-first: typing chains keep superseded incompletes near the LRU end;
            # bounded scan finds the immediate predecessor quickly.
            # Prefix filter before scan_count — budget counts this locale only.
            for k, v in reversed(grammar_registry.sentence_cache.items()):
                if not k.startswith(prefix):
                    continue
                if scan_count >= MAX_RECENT_INCOMPLETE_SCAN:
                    break
                _other_fp, other_canon, other_complete, _ = v
                if should_evict_incomplete_prefix_predecessor(other_complete=other_complete, other_canon=other_canon, new_canon=canon):
                    to_remove.append(k)
                scan_count += 1
            for k in to_remove:
                grammar_registry.sentence_cache.pop(k, None)


def clear_sentence_cache(ctx: Any | None = None) -> None:
    """Clear sentence cache (for tests)."""
    cache_clear(ctx)

_SENTENCE_CACHE = grammar_registry.sentence_cache
