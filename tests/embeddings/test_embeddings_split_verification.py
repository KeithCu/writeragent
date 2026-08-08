# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formal verification (Hypothesis + Deal) for embeddings text chunking and sentence span merging."""

from __future__ import annotations

from hypothesis import given, strategies as st

from plugin.embeddings.embeddings_split import (
    _merge_small_sentences_to_spans,
    _meta_chunks_from_spans,
)


@st.composite
def sentence_spans(draw: st.DrawFn) -> tuple[str, list[tuple[int, int, str]]]:
    sentences = draw(st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=10))
    passage = " ".join(sentences)
    result_spans: list[tuple[int, int, str]] = []
    idx = 0
    for sent in sentences:
        start = passage.find(sent, idx)
        if start < 0:
            start = idx
        end = start + len(sent)
        result_spans.append((start, end, sent))
        idx = end
    return passage, result_spans


@given(sentence_spans(), st.integers(min_value=10, max_value=200))
def test_merge_small_sentences_to_spans_invariants(
    data: tuple[str, list[tuple[int, int, str]]], min_chunk: int
) -> None:
    passage, sentences = data
    spans = _merge_small_sentences_to_spans(passage, sentences, min_chunk=min_chunk)
    assert isinstance(spans, list)
    prev_end = -1
    for start, end in spans:
        assert 0 <= start <= end <= len(passage)
        # Verify non-overlapping & monotonically increasing spans
        assert start >= prev_end
        prev_end = end


@given(st.text(), st.lists(st.tuples(st.integers(min_value=0, max_value=20), st.integers(min_value=0, max_value=20))), st.dictionaries(st.text(), st.text()))
def test_meta_chunks_from_spans_invariants(
    passage: str, raw_spans: list[tuple[int, int]], base_meta: dict[str, str]
) -> None:
    # Filter valid bounds for passage
    n = len(passage)
    valid_spans: list[tuple[int, int]] = []
    for s, e in raw_spans:
        start = min(s, n)
        end = min(max(s, e), n)
        valid_spans.append((start, end))

    chunks = _meta_chunks_from_spans(passage, valid_spans, base_meta)
    assert isinstance(chunks, list)
    for chunk in chunks:
        assert "char_start" in chunk
        assert "char_end" in chunk
        assert "text" in chunk
        assert chunk["text"] == passage[chunk["char_start"] : chunk["char_end"]]
