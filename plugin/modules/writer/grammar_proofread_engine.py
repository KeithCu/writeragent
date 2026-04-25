# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python helpers for AI grammar proofreading (JSON parsing, cache, offsets)."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from plugin.framework.errors import safe_json_loads

log = logging.getLogger(__name__)
_grammar_diag = logging.getLogger("writeragent.grammar")

# Hyphenated tags for ``LinguisticWriterAgentGrammar.xcu`` ``Locales`` (one ``oor:string-list``
# ``<value>``, space-separated — same pattern as Lightproof / bundled LO grammar extensions).
GRAMMAR_REGISTRY_LOCALE_TAGS: tuple[str, ...] = ("en-US", "en-GB")

_CACHE_LOCK = threading.Lock()
# cache_key -> (text_fingerprint, tuple of normalized error dicts)
_proofread_cache: dict[str, tuple[str, tuple[dict[str, Any], ...]]] = {}
_ignored_rules: set[str] = set()


def cache_clear() -> None:
    """Clear proofreading cache (e.g. tests)."""
    with _CACHE_LOCK:
        _proofread_cache.clear()


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
    n_start: int,
    n_end: int,
    locale_key: str,
) -> str:
    return f"{doc_id!s}|{n_start}|{n_end}|{locale_key}"


def cache_get(key: str, fingerprint: str) -> tuple[dict[str, Any], ...] | None:
    with _CACHE_LOCK:
        hit = _proofread_cache.get(key)
        if not hit:
            return None
        cached_fp, errors = hit
        if cached_fp != fingerprint:
            return None
        return errors


def cache_put(key: str, fingerprint: str, errors: Sequence[dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        _proofread_cache[key] = (fingerprint, tuple(errors))


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
    try:
        data = safe_json_loads(text)
    except Exception as e:
        _grammar_diag.warning("[grammar] parse_grammar_json: JSON parse failed: %s", e, exc_info=True)
        return []
    if not isinstance(data, Mapping):
        return []
    raw = data.get("errors")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        wrong = item.get("wrong")
        correct = item.get("correct")
        if wrong is None or correct is None:
            continue
        out.append(
            {
                "wrong": str(wrong),
                "correct": str(correct),
                "type": str(item.get("type", "grammar")),
                "reason": str(item.get("reason", "")),
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
