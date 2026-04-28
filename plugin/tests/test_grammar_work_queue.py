# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar work queue deduplication logic."""

from __future__ import annotations

from plugin.modules.writer.grammar_proofread_engine import (
    GrammarWorkItem,
    deduplicate_grammar_batch,
)


def _make_item(
    text: str,
    *,
    doc_id: str = "doc1",
    locale: str = "en-US",
    seq: int = 1,
    inflight_key: str = "",
) -> GrammarWorkItem:
    """Helper to build a work item with sensible defaults."""
    if not inflight_key:
        inflight_key = f"{doc_id}|{locale}|fp_{hash(text)}"
    return GrammarWorkItem(
        ctx=None,
        full_text=text,
        n_start=0,
        n_end=len(text),
        grammar_bcp47=locale,
        partial_sentence=False,
        doc_id=doc_id,
        inflight_key=inflight_key,
        enqueue_seq=seq,
    )


def test_prefix_dedup_typing_sequence() -> None:
    """Typing 'This is' -> 'This is a' -> 'This is a story.' keeps only newest."""
    items = [
        _make_item("This is", seq=1),
        _make_item("This is a", seq=2),
        _make_item("This is a story.", seq=3),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    surviving_text = result[0].full_text[result[0].n_start : result[0].n_end]
    assert surviving_text == "This is a story."


def test_prefix_dedup_different_paragraphs() -> None:
    """Two different paragraphs (non-prefix) should both survive."""
    items = [
        _make_item("Hello world.", seq=1),
        _make_item("Goodbye world.", seq=2),
    ]
    result = deduplicate_grammar_batch(items)
    texts = {r.full_text[r.n_start : r.n_end] for r in result}
    assert texts == {"Hello world.", "Goodbye world."}


def test_supersede_same_key() -> None:
    """Same inflight_key with different sequences -> only highest seq survives."""
    key = "doc1|en-US|fp_abc"
    items = [
        _make_item("Same text.", seq=1, inflight_key=key),
        _make_item("Same text.", seq=3, inflight_key=key),
        _make_item("Same text.", seq=2, inflight_key=key),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    assert result[0].enqueue_seq == 3


def test_mixed_dedup() -> None:
    """Combination of prefix dedup + supersede in one batch."""
    key = "doc1|en-US|fp_short"
    items = [
        # Two versions of the same key (supersede: keep seq=5)
        _make_item("Short.", seq=3, inflight_key=key),
        _make_item("Short.", seq=5, inflight_key=key),
        # A prefix chain (prefix dedup: keep longest)
        _make_item("The cat", seq=6),
        _make_item("The cat sat on the mat.", seq=7),
        # Unrelated paragraph
        _make_item("Unrelated paragraph.", seq=8),
    ]
    result = deduplicate_grammar_batch(items)
    texts = {r.full_text[r.n_start : r.n_end] for r in result}
    # "Short." survives (seq=5), "The cat" dropped (older prefix-related),
    # "The cat sat on the mat." survives (newer), "Unrelated paragraph." survives
    assert "Short." in texts
    assert "The cat sat on the mat." in texts
    assert "Unrelated paragraph." in texts
    assert "The cat" not in texts
    assert len(texts) == 3


def test_different_locales_not_deduped() -> None:
    """Same text in different locales should NOT be deduped (different groups)."""
    items = [
        _make_item("Bonjour le monde.", locale="fr-FR", seq=1),
        _make_item("Bonjour le monde.", locale="en-US", seq=2),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 2
    locales = {r.grammar_bcp47 for r in result}
    assert locales == {"fr-FR", "en-US"}


def test_newest_wins_over_longest_for_prefix_related_items() -> None:
    """A newer shorter prefix-related item must survive over older longer text."""
    items = [
        _make_item("What is going on", seq=10),
        _make_item("What is going", seq=11),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    item = result[0]
    assert item.enqueue_seq == 11
    assert item.full_text[item.n_start : item.n_end] == "What is going"


def test_reverse_prefix_chain_executes_only_latest() -> None:
    """Reverse chain reproducer: only newest item survives."""
    items = [
        _make_item("What is going on", seq=1),
        _make_item("What is going o", seq=2),
        _make_item("What is going", seq=3),
        _make_item("What is goin", seq=4),
        _make_item("What is goi", seq=5),
        _make_item("What is go", seq=6),
        _make_item("What is g", seq=7),
        _make_item("What is ", seq=8),
        _make_item("What is", seq=9),
        _make_item("W", seq=10),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    item = result[0]
    assert item.enqueue_seq == 10
    assert item.full_text[item.n_start : item.n_end] == "W"


