# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sentence-level LRU cache for grammar proofreading (ignore rules, fingerprint keys).

Sentence-boundary tables and ``looks_complete_sentence`` live in ``grammar_proofread_locale``.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Iterable

log = logging.getLogger("writeragent.grammar")

from plugin.writer.locale import grammar_settings
from .grammar_persistence import SentenceCacheEntry, get_persistence, grammar_registry
from .grammar_proofread_locale import (
    MAX_CACHE_SIZE,
    MAX_RECENT_INCOMPLETE_SCAN,
    fingerprint_for_text,
    looks_complete_sentence,
    normalize_for_sentence_cache,
    sentence_identity_fp,
)


def _sanitize_checker_identity(identity: str) -> str:
    """Pipe is the L1 key delimiter; model ids must not split the key."""
    return identity.replace("|", "/")


def sentence_cache_key_prefix(locale_key: str, checker_identity: str | None = None) -> str:
    """Prefix for every sentence-cache OrderedDict key: ``sent|<locale>|<identity>|``."""
    ident_raw = grammar_settings.checker_identity() if checker_identity is None else checker_identity
    ident = _sanitize_checker_identity(ident_raw)
    return f"sent|{locale_key}|{ident}|"


def _sentence_l1_key(locale_key: str, checker_identity: str | None, fp: str) -> str:
    """Format an in-memory L1 sentence cache key (locale + checker identity + fingerprint)."""
    return f"{sentence_cache_key_prefix(locale_key, checker_identity)}{fp}"


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
        if effective_len != length:
            e = dict(e)
            e["n_error_length"] = effective_len
        clipped.append(e)
    return clipped


def dump_l1_sentence_cache() -> None:
    """Clear the global in-memory sentence LRU (all checker identities)."""
    with grammar_registry.lock:
        grammar_registry.sentence_cache.clear()


def recheck_active_document_grammar(ctx: Any) -> None:
    """Dump caches for the active document and ask Writer to walk it again."""
    dump_l1_sentence_cache()
    from plugin.framework.uno_context import get_active_document
    from plugin.writer.locale.grammar_persistence import persistence_for_model

    model = get_active_document(ctx)
    if model is not None:
        p = persistence_for_model(model)
        if p is not None:
            p.clear_and_persist()

    for pr in list(grammar_registry.live_proofreaders):
        pr.broadcast_proofread_again()


def _populate_memory_cache_only(
    locale_key: str,
    sentence: str,
    errors: list[dict[str, Any]],
    checker_identity: str | None = None,
) -> SentenceCacheEntry:
    """Internal: populate memory cache only, no persistence, no compaction.

    Used by cache_get_sentence to warm cache from persistence without side effects.
    Returns the entry stored in ``sentence_cache``:
    ``SentenceCacheEntry(fp, canon, is_complete, clipped_errors)``. The OrderedDict key is not
    part of that value.
    """
    canon = normalize_for_sentence_cache(sentence)
    fp = fingerprint_for_text(canon)
    key = _sentence_l1_key(locale_key, checker_identity, fp)
    clipped = _clip_errors_to_canonical_length(errors, len(canon))
    is_complete = looks_complete_sentence(canon)
    cloned_errors = [dict(e) for e in clipped]
    entry = SentenceCacheEntry(fp, canon, is_complete, cloned_errors)

    with grammar_registry.lock:
        grammar_registry.sentence_cache[key] = entry
        grammar_registry.sentence_cache.move_to_end(key)
        while len(grammar_registry.sentence_cache) > MAX_CACHE_SIZE:
            grammar_registry.sentence_cache.popitem(last=False)

    return entry


def _align_persistence_identity(p: Any, ident: str) -> None:
    """Apply checker identity to L2; evict L1 for a mid-session switch."""
    switched = p.ensure_identity(ident)
    if switched:
        evicted_ident = _sanitize_checker_identity(switched)
        needle = f"|{evicted_ident}|"
        with grammar_registry.lock:
            to_remove = [k for k in grammar_registry.sentence_cache if needle in k]
            for k in to_remove:
                grammar_registry.sentence_cache.pop(k, None)


def cache_get_sentence(
    locale_key: str,
    sentence: str,
    ctx: Any | None = None,
    doc_id: str | None = None,
    checker_identity: str | None = None,
) -> list[dict[str, Any]] | None:
    """Return cached errors for this exact sentence (relative to sentence start = 0)."""
    ident = grammar_settings.checker_identity() if checker_identity is None else checker_identity
    # The key already ends in fp; one fingerprint serves both (no double SHA-256).
    fp = sentence_identity_fp(sentence)
    key = _sentence_l1_key(locale_key, ident, fp)
    result = None
    with grammar_registry.lock:
        hit = grammar_registry.sentence_cache.get(key)
        if hit:
            grammar_registry.sentence_cache.move_to_end(key)
            result = copy.deepcopy(hit.errors)

    p = get_persistence(ctx, doc_id) if ctx and doc_id else None
    if p:
        _align_persistence_identity(p, ident)

    if result is not None:
        if p:
            p.mark_accessed(fp, locale_key)
        return result

    if p:
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
    ident = grammar_settings.checker_identity() if checker_identity is None else checker_identity
    # Tier 1 (global LRU) + Tier 2 (document persistence): always warm L1; L2 when doc_id is set.
    entry = _populate_memory_cache_only(
        locale_key, sentence, errors, ident
    )

    if ctx and doc_id:
        p = get_persistence(ctx, doc_id)
        if p:
            _align_persistence_identity(p, ident)
            p.put(entry.fingerprint, locale_key, entry.errors)
        # Note: document mode skips incomplete-sentence prefix compaction logic (scans _SENTENCE_CACHE only)
        # but we still performed _populate_memory_cache_only above.
        return

    if not entry.complete:
        with grammar_registry.lock:
            prefix = sentence_cache_key_prefix(locale_key, ident)
            scan_count = 0
            to_remove: list[str] = []
            # Newest-first: typing chains keep superseded incompletes near the LRU end;
            # bounded scan finds the immediate predecessor quickly.
            # Prefix filter before scan_count — budget counts this locale only.
            for k, other_entry in reversed(grammar_registry.sentence_cache.items()):
                if not k.startswith(prefix):
                    continue
                if scan_count >= MAX_RECENT_INCOMPLETE_SCAN:
                    break
                if (
                    not other_entry.complete
                    and len(other_entry.canonical) < len(entry.canonical)
                    and entry.canonical.startswith(other_entry.canonical)
                ):
                    to_remove.append(k)
                scan_count += 1
            for k in to_remove:
                grammar_registry.sentence_cache.pop(k, None)


def store_sentence_result(
    ctx: Any,
    doc_id: str,
    locale: str,
    text: str,
    raw_errors: Iterable[dict[str, Any]],
    checker_identity: str | None = None,
    *,
    alt_locale: str = "",
    ignore_set: Any | None = None,
) -> list[dict[str, Any]]:
    """Normalize raw errors, cache the sentence payload, and return active (non-ignored) errors."""
    from dataclasses import asdict
    from plugin.framework.grammar_rule_ids import grammar_bcp47_tags_match
    from .grammar_ignore_rules import IgnoreSet
    from .grammar_proofread_text import normalize_errors_for_text

    norm_errors = normalize_errors_for_text(text, raw_errors, ctx, locale)
    if ignore_set is None:
        ignore_set = IgnoreSet.for_document(ctx, doc_id)

    payload = [asdict(e) for e in norm_errors]
    cache_put_sentence(locale, text, payload, ctx=ctx, doc_id=doc_id, checker_identity=checker_identity)
    if alt_locale and not grammar_bcp47_tags_match(alt_locale, locale):
        log.debug("[grammar] Double caching for %s (detected %s)", alt_locale, locale)
        cache_put_sentence(alt_locale, text, payload, ctx=ctx, doc_id=doc_id, checker_identity=checker_identity)

    active_payload = [d for d in payload if ignore_set.allows(str(d.get("rule_identifier", "")))]
    return active_payload

