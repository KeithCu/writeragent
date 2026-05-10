# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Queued grammar work units and batch deduplication (no UNO imports)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_grammar_diag = logging.getLogger("writeragent.grammar")


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
