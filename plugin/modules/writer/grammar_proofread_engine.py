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
from typing import Any, Iterable, Mapping, Sequence, cast

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
# cache_key -> (text_fingerprint, tuple of normalized error dicts)
_proofread_cache: collections.OrderedDict[str, tuple[str, tuple[dict[str, Any], ...]]] = collections.OrderedDict()
_ignored_rules: set[str] = set()
MAX_CACHE_SIZE = 128


def cache_clear() -> None:
    """Clear proofreading cache (e.g. tests)."""
    with _CACHE_LOCK:
        _proofread_cache.clear()
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


def make_cache_key(
    doc_id: Any,
    locale_key: str,
    fingerprint: str = "",
    slice_start: int | None = None,
    slice_end: int | None = None,
) -> str:
    """Build a cache key. When *fingerprint* is set, include slice bounds so identical
    substring text at different document positions never shares one cache entry.
    """
    if fingerprint:
        if slice_start is not None and slice_end is not None:
            return f"{doc_id!s}|{locale_key}|{fingerprint}|{slice_start}:{slice_end}"
        return f"{doc_id!s}|{locale_key}|{fingerprint}"
    return f"{doc_id!s}|{locale_key}"


def cache_get(key: str, fingerprint: str) -> tuple[dict[str, Any], ...] | None:
    with _CACHE_LOCK:
        hit = _proofread_cache.get(key)
        if not hit:
            return None
        cached_fp, errors = hit
        if cached_fp != fingerprint:
            return None
        _proofread_cache.move_to_end(key)
        return errors


def cache_put(key: str, fingerprint: str, errors: Sequence[dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        _proofread_cache[key] = (fingerprint, tuple(errors))
        _proofread_cache.move_to_end(key)
        while len(_proofread_cache) > MAX_CACHE_SIZE:
            _proofread_cache.popitem(last=False)


@dataclass(frozen=True)
class NormalizedProofError:
    """One grammar issue with absolute offsets in the proofread buffer ``rText``."""

    n_error_start: int
    n_error_length: int
    suggestions: tuple[str, ...]
    short_comment: str
    full_comment: str
    rule_identifier: str


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


def normalize_errors_for_text(
    full_text: str,
    n_slice_start: int,
    n_slice_end: int,
    items: Iterable[dict[str, Any]],
    ignored: set[str] | None = None,
) -> list[NormalizedProofError]:
    """Map ``wrong`` substrings to absolute positions in ``full_text`` (Writer buffer)."""
    ignored = ignored or set()
    slice_end = min(n_slice_end, len(full_text))
    slice_start = max(0, min(n_slice_start, slice_end))
    window = full_text[slice_start:slice_end]
    results: list[NormalizedProofError] = []
    used_spans: list[tuple[int, int]] = []

    for idx, it in enumerate(items):
        wrong = it.get("wrong", "")
        correct = it.get("correct", "")
        if not wrong:
            continue
        rel = window.find(wrong)
        if rel >= 0:
            pos = slice_start + rel
        else:
            pos = full_text.find(wrong)
            if pos < 0 or pos < slice_start or pos + len(wrong) > slice_end:
                continue
        length = len(wrong)
        if length <= 0:
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


def split_into_sentences(text: str) -> list[tuple[int, str]]:
    """Split *text* into ``(start_offset, sentence_text)`` pairs.

    Splits on sentence-ending punctuation followed by whitespace.
    Each segment retains its punctuation; trailing whitespace is kept
    (normalization happens at cache-key time via ``rstrip``).

    Returns at least one element when *text* contains non-whitespace.
    """
    if not text or not text.strip():
        return []
    result: list[tuple[int, str]] = []
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


# --- Sentence-level cache (simple, text-based, no positions)
# Keyed by locale + sentence fingerprint (trailing whitespace stripped for normalization).
# Errors are relative to the start of that sentence (offset 0).
# This fulfills the requirement that identical sentence text always has the same errors,
# regardless of document position.

_SENTENCE_CACHE: collections.OrderedDict[str, tuple[str, list[dict[str, Any]]]] = collections.OrderedDict()


def make_sentence_key(locale_key: str, sentence: str) -> str:
    """Cache key for a specific sentence text (locale + fingerprint).

    Trailing whitespace is stripped so ``'Hello.'`` and ``'Hello. '`` share
    the same cache entry (handles the enter-at-end-of-paragraph edge case).
    """
    fp = fingerprint_for_text(sentence.rstrip())
    return f"sent|{locale_key}|{fp}"


def cache_get_sentence(locale_key: str, sentence: str) -> list[dict[str, Any]] | None:
    """Return cached errors for this exact sentence (relative to sentence start = 0).

    Trailing whitespace is stripped before fingerprint comparison.
    """
    key = make_sentence_key(locale_key, sentence)
    with _CACHE_LOCK:
        hit = _SENTENCE_CACHE.get(key)
        if not hit:
            return None
        cached_fp, errors = hit
        if cached_fp != fingerprint_for_text(sentence.rstrip()):
            return None
        _SENTENCE_CACHE.move_to_end(key)
        return list(errors)  # return copy


def cache_put_sentence(locale_key: str, sentence: str, errors: list[dict[str, Any]]) -> None:
    """Cache errors for this sentence text (errors must have offsets relative to sentence start).

    Trailing whitespace is stripped before fingerprinting.
    """
    key = make_sentence_key(locale_key, sentence)
    fp = fingerprint_for_text(sentence.rstrip())
    with _CACHE_LOCK:
        _SENTENCE_CACHE[key] = (fp, [dict(e) for e in errors])  # deep enough copy
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
    """Remove stale items from a batch: superseded keys and prefix subsets.

    Within each ``(doc_id, locale)`` group, if item A's slice text is a proper
    prefix of item B's slice text, A is dropped (B is the more-complete version
    from continued typing).  Additionally, if two items share the same
    ``inflight_key`` (same fingerprint), the one with the lower ``enqueue_seq``
    is dropped.
    """
    from collections import defaultdict

    # Step 1: supersede by sequence (same inflight_key → keep latest only)
    best_by_key: dict[str, GrammarWorkItem] = {}
    for item in batch:
        prev = best_by_key.get(item.inflight_key)
        if prev is None or item.enqueue_seq > prev.enqueue_seq:
            best_by_key[item.inflight_key] = item
    unique = list(best_by_key.values())

    # Step 2: prefix dedup within (doc_id, locale) groups
    groups: dict[tuple[str, str], list[GrammarWorkItem]] = defaultdict(list)
    for item in unique:
        groups[(item.doc_id, item.grammar_bcp47)].append(item)

    result: list[GrammarWorkItem] = []
    for _key, group in groups.items():
        # Sort longest-first so we can cheaply check "is X a prefix of Y?"
        group.sort(key=lambda x: len(x.full_text[x.n_start : x.n_end]), reverse=True)
        kept_texts: list[str] = []
        for item in group:
            slice_txt = item.full_text[item.n_start : item.n_end]
            # Drop if this text is a proper prefix of any already-kept text
            if any(
                longer.startswith(slice_txt) and longer != slice_txt
                for longer in kept_texts
            ):
                _grammar_diag.info(
                    "[grammar] queue dedup: dropped prefix len=%s "
                    "(superseded by longer text len=%s)",
                    len(slice_txt),
                    next(
                        len(t)
                        for t in kept_texts
                        if t.startswith(slice_txt) and t != slice_txt
                    ),
                )
                continue
            kept_texts.append(slice_txt)
            result.append(item)
    return result
