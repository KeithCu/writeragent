# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sentence-level LRU cache for grammar proofreading (ignore rules, fingerprint keys).

Sentence-boundary tables and ``looks_complete_sentence`` live in ``grammar_proofread_locale``.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.config import grammar_checker_identity

from .grammar_persistence import get_persistence, grammar_registry
from .grammar_proofread_locale import (
    GRAMMAR_CACHE_NORMALIZATION_RE,
    MAX_CACHE_SIZE,
    MAX_RECENT_INCOMPLETE_SCAN,
    fingerprint_for_text,
    looks_complete_sentence,
)

log = logging.getLogger("writeragent.grammar")


def _sanitize_checker_identity(identity: str) -> str:
    """Pipe is the L1 key delimiter; model ids must not split the key."""
    return identity.replace("|", "/")


def resolve_checker_identity(checker_identity: str | None = None) -> str:
    """Return the L1/L2 checker identity, defaulting to the active grammar checker."""
    if checker_identity is not None:
        return checker_identity
    return grammar_checker_identity()

def cache_clear(ctx: Any | None = None) -> None:
    """Clear the in-memory proofreading cache (tests / reset).

    Always ``clear_all``: a follow-up ``get_persistence(doc_id)`` would register a
    fresh model-less ``DocumentPersistence``. Per-document teardown is
    ``grammar_registry.clear_for_doc``.
    """
    grammar_registry.clear_all(ctx)


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


def sentence_cache_key_prefix(locale_key: str, checker_identity: str | None = None) -> str:
    """Prefix for every sentence-cache OrderedDict key: ``sent|<locale>|<identity>|``."""
    ident = _sanitize_checker_identity(resolve_checker_identity(checker_identity))
    return f"sent|{locale_key}|{ident}|"


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
    """Clip or drop errors that reference positions beyond the canonical sentence length.

    ``effective_len <= 0`` must not drop Harper inserts
    (``n_error_length == 0``), and ``start >= canonical_len`` must
    not drop an insert at the canonical end. ``cache_put_sentence``
    would then store the sentence with no errors. A zero-width
    point with ``0 <= start <= canonical_len`` stays, including its
    suggestions and rule id. Positive lengths still clip, and still
    drop when they start at or past that end. Negative starts,
    non-ints, and bools are dropped.
    """
    clipped: list[dict[str, Any]] = []
    for e in errors:
        start = e.get("n_error_start", 0)
        length = e.get("n_error_length", 0)
        if (
            isinstance(start, bool)
            or isinstance(length, bool)
            or not isinstance(start, int)
            or not isinstance(length, int)
            or start < 0
            or length < 0
            or start > canonical_len
        ):
            continue
        if length == 0:
            # Missing length used to fall through ``effective_len <= 0`` and
            # drop. Only an explicit zero is an insert; an omitted length is
            # not one.
            if "n_error_length" in e:
                clipped.append(e)
            continue
        if start >= canonical_len:
            continue
        effective_len = min(length, canonical_len - start)
        if effective_len <= 0:
            continue
        if effective_len != length:
            e = dict(e)
            e["n_error_length"] = effective_len
        clipped.append(e)
    return clipped


def make_sentence_key(locale_key: str, sentence: str, checker_identity: str | None = None) -> str:
    """Cache key for a specific sentence text (locale + checker identity + fingerprint)."""
    return f"{sentence_cache_key_prefix(locale_key, checker_identity)}{sentence_identity_fp(sentence)}"


def evict_sentence_cache_for_identity(checker_identity: str) -> None:
    """Drop L1 rows for one checker identity (mid-session Harper ↔ LLM switch)."""
    ident = _sanitize_checker_identity(checker_identity)
    needle = f"|{ident}|"
    with grammar_registry.lock:
        to_remove = [k for k in grammar_registry.sentence_cache if needle in k]
        for k in to_remove:
            grammar_registry.sentence_cache.pop(k, None)


def dump_l1_sentence_cache() -> None:
    """Clear the global in-memory sentence LRU (all checker identities)."""
    with grammar_registry.lock:
        grammar_registry.sentence_cache.clear()


def dump_grammar_cache_for_recheck(ctx: Any) -> None:
    """Recheck: drop all of L1 and this active document's L2. Does not clear ignore rules."""
    dump_l1_sentence_cache()
    from plugin.framework.uno_context import get_active_document
    from plugin.writer.locale.grammar_persistence import persistence_for_model

    model = get_active_document(ctx)
    if model is None:
        return
    p = persistence_for_model(model)
    if p is None:
        return
    p.clear_and_persist()


def recheck_active_document_grammar(ctx: Any) -> None:
    """Dump caches for the active document and ask Writer to walk it again."""
    dump_grammar_cache_for_recheck(ctx)
    for pr in list(grammar_registry.live_proofreaders):
        fn = getattr(pr, "broadcast_proofread_again", None)
        if not callable(fn):
            continue
        try:
            fn()
        except Exception:
            log.debug("[grammar] PROOFREAD_AGAIN broadcast failed", exc_info=True)


def _populate_memory_cache_only(
    locale_key: str,
    sentence: str,
    errors: list[dict[str, Any]],
    checker_identity: str | None = None,
) -> tuple[str, str, bool, list[dict[str, Any]]]:
    """Internal: populate memory cache only, no persistence, no compaction.

    Used by cache_get_sentence to warm cache from persistence without side effects.
    Returns the 4-tuple stored in ``sentence_cache``:
    ``(fp, canon, is_complete, clipped_errors)``. The OrderedDict key is not
    part of that value.
    """
    canon = _normalize_for_sentence_cache(sentence)
    fp = fingerprint_for_text(canon)
    key = f"{sentence_cache_key_prefix(locale_key, checker_identity)}{fp}"
    clipped = _clip_errors_to_canonical_length(errors, len(canon))
    is_complete = _is_complete_sentence(canon)
    cloned_errors = [dict(e) for e in clipped]

    with grammar_registry.lock:
        grammar_registry.sentence_cache[key] = (fp, canon, is_complete, cloned_errors)
        grammar_registry.sentence_cache.move_to_end(key)
        while len(grammar_registry.sentence_cache) > MAX_CACHE_SIZE:
            grammar_registry.sentence_cache.popitem(last=False)

    return fp, canon, is_complete, cloned_errors


def _align_persistence_identity(p: Any, ident: str) -> None:
    """Apply checker identity to L2; evict L1 for a mid-session switch."""
    switched = p.ensure_identity(ident)
    if switched:
        evict_sentence_cache_for_identity(switched)


def cache_get_sentence(
    locale_key: str,
    sentence: str,
    ctx: Any | None = None,
    doc_id: str | None = None,
    checker_identity: str | None = None,
) -> list[dict[str, Any]] | None:
    """Return cached errors for this exact sentence (relative to sentence start = 0)."""
    ident = resolve_checker_identity(checker_identity)
    key = make_sentence_key(locale_key, sentence, ident)
    fp = sentence_identity_fp(sentence)
    result = None
    with grammar_registry.lock:
        hit = grammar_registry.sentence_cache.get(key)
        if hit and hit[0] == fp:
            grammar_registry.sentence_cache.move_to_end(key)
            result = list(hit[3])

    if result is not None:
        if ctx and doc_id:
            p = get_persistence(ctx, doc_id)
            if p:
                _align_persistence_identity(p, ident)
                p.mark_accessed(fp, locale_key)
        return result

    if ctx and doc_id:
        p = get_persistence(ctx, doc_id)
        if p:
            _align_persistence_identity(p, ident)
            # L2 is (locale, fingerprint). Fingerprint alone would return the
            # other CharLocale's errors, including a good (empty) row.
            persisted = p.get(fp, locale_key)
            if persisted is not None:
                _populate_memory_cache_only(locale_key, sentence, persisted, ident)
                return list(persisted)

    return None


def cache_put_sentence(
    locale_key: str,
    sentence: str,
    errors: list[dict[str, Any]],
    ctx: Any | None = None,
    doc_id: str | None = None,
    checker_identity: str | None = None,
) -> None:
    """Cache errors for this sentence text (errors must have offsets relative to sentence start)."""
    ident = resolve_checker_identity(checker_identity)
    # Tier 1 (global LRU) + Tier 2 (document persistence): always warm L1; L2 when doc_id is set.
    fp, canon, is_complete, clipped_errors = _populate_memory_cache_only(
        locale_key, sentence, errors, ident
    )

    if ctx and doc_id:
        p = get_persistence(ctx, doc_id)
        if p:
            _align_persistence_identity(p, ident)
            p.put(fp, locale_key, [dict(e) for e in clipped_errors])
        # Note: document mode skips incomplete-sentence prefix compaction logic (scans _SENTENCE_CACHE only)
        # but we still performed _populate_memory_cache_only above.
        return

    if not is_complete:
        with grammar_registry.lock:
            prefix = sentence_cache_key_prefix(locale_key, ident)
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
                _other_fp, other_canon, other_complete, _unused = v
                if should_evict_incomplete_prefix_predecessor(other_complete=other_complete, other_canon=other_canon, new_canon=canon):
                    to_remove.append(k)
                scan_count += 1
            for k in to_remove:
                grammar_registry.sentence_cache.pop(k, None)

