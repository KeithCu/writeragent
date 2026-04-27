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
    """Typing 'This is' -> 'This is a' -> 'This is a story.' should keep only the longest."""
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
    # "Short." survives (seq=5), "The cat" dropped (prefix of longer),
    # "The cat sat on the mat." survives, "Unrelated paragraph." survives
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


# ---------------------------------------------------------------------------
# Sentence splitter tests
# ---------------------------------------------------------------------------

from plugin.modules.writer.grammar_proofread_engine import (
    cache_get_sentence,
    cache_put_sentence,
    clear_sentence_cache,
    make_sentence_key,
    split_into_sentences,
)


def test_split_basic_two_sentences() -> None:
    result = split_into_sentences("Hello world. This is fine.")
    assert len(result) == 2
    assert result[0] == (0, "Hello world.")
    assert result[1][1] == "This is fine."


def test_split_single_sentence() -> None:
    result = split_into_sentences("Just one.")
    assert len(result) == 1
    assert result[0] == (0, "Just one.")


def test_split_three_sentences() -> None:
    text = "First. Second. Third."
    result = split_into_sentences(text)
    assert len(result) == 3
    assert result[0][1] == "First."
    assert result[1][1] == "Second."
    assert result[2][1] == "Third."
    # Verify offsets are correct
    for offset, sent in result:
        assert text[offset : offset + len(sent)] == sent


def test_split_multilingual_terminators() -> None:
    result = split_into_sentences("これは文です。 次の文。")
    assert len(result) == 2


def test_split_question_and_exclamation() -> None:
    result = split_into_sentences("Really? Yes! Okay.")
    assert len(result) == 3


def test_split_empty_and_whitespace() -> None:
    assert split_into_sentences("") == []
    assert split_into_sentences("   ") == []


def test_split_no_terminator() -> None:
    """Text without sentence-ending punctuation stays as one segment."""
    result = split_into_sentences("hello world without punctuation")
    assert len(result) == 1
    assert result[0] == (0, "hello world without punctuation")


def test_split_preserves_offsets() -> None:
    """Offsets should correctly index back into the original text."""
    text = "Alpha bravo. Charlie delta. Echo foxtrot."
    result = split_into_sentences(text)
    for offset, sent in result:
        assert text[offset : offset + len(sent)] == sent


# ---------------------------------------------------------------------------
# Trailing whitespace normalization tests
# ---------------------------------------------------------------------------


def test_whitespace_normalization_cache_key() -> None:
    """'Hello.' and 'Hello. ' and 'Hello.\\n' should produce the same cache key."""
    key1 = make_sentence_key("en-US", "Hello.")
    key2 = make_sentence_key("en-US", "Hello. ")
    key3 = make_sentence_key("en-US", "Hello.\n")
    assert key1 == key2 == key3


def test_cache_hit_with_trailing_whitespace() -> None:
    """Putting 'Hello.' and getting 'Hello. ' should be a cache hit."""
    clear_sentence_cache()
    cache_put_sentence("en-US", "Hello.", [{"n_error_start": 0, "n_error_length": 5}])
    result = cache_get_sentence("en-US", "Hello. ")
    assert result is not None
    assert len(result) == 1
    assert result[0]["n_error_start"] == 0


def test_cache_roundtrip_per_sentence() -> None:
    """Simulate the per-sentence cache flow: store per sentence, retrieve per sentence."""
    clear_sentence_cache()
    # Simulate worker storing per-sentence errors
    cache_put_sentence("en-US", "This has a eror.", [
        {"n_error_start": 11, "n_error_length": 4, "suggestions": ("error",),
         "short_comment": "(spelling) typo", "full_comment": "typo",
         "rule_identifier": "wa_grammar_0_abc"},
    ])
    cache_put_sentence("en-US", "Second sent.", [])  # no errors

    # Simulate doProofreading lookup for a grown paragraph
    sentences = split_into_sentences("This has a eror. Second sent. Third.")
    assert len(sentences) == 3

    # First two should be cached
    assert cache_get_sentence("en-US", "This has a eror.") is not None
    assert cache_get_sentence("en-US", "Second sent.") is not None
    # Third is new → cache miss
    assert cache_get_sentence("en-US", "Third.") is None
