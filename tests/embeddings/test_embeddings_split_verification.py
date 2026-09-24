# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formal verification (Hypothesis + Deal) for embeddings text chunking and sentence span merging.

Hypothesis: light under ``make verify``; deep via ``make vhs``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import deal
import pytest

from plugin.embeddings.embeddings_fs import LocaleTextRun
from plugin.embeddings.embeddings_split import (
    _deal_chunk_base_meta_ok,
    _deal_passage_text_ok,
    _merge_small_sentences_to_spans,
    _meta_chunks_from_spans,
    _split_non_prose_passage_to_spans,
    _split_passage_whitespace_to_sentences,
    _split_prose_passage_to_spans,
    split_passage_locale_runs_to_chunk_meta,
    split_passage_to_sentences,
)
from plugin.framework.deal_shim import DEAL_MAX_SOURCE
from tests.strip_bundle import deal_pre_present, expect_pre_or_body
from tests.vhs_budget import vhs_max_examples


def test_merge_small_sentences_rejects_negative_start() -> None:
    """CrossHair counterexample: negative start violated post ``0 <= s[0]``; pre must reject it."""
    expect_pre_or_body(
        lambda: _merge_small_sentences_to_spans("", [(-1, 0, "")], min_chunk=1),
        body_result=[],
    )


def test_embeddings_split_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(_merge_small_sentences_to_spans):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "x" * (DEAL_MAX_SOURCE + 1)
    with pytest.raises(deal.PreContractError):
        _merge_small_sentences_to_spans(too_long, [], min_chunk=1)
    with pytest.raises(deal.PreContractError):
        _meta_chunks_from_spans(too_long, [], {})
    with pytest.raises(deal.PreContractError):
        split_passage_to_sentences(too_long)
    with pytest.raises(deal.PreContractError):
        _split_passage_whitespace_to_sentences(too_long)
    with pytest.raises(deal.PreContractError):
        _split_prose_passage_to_spans(too_long)
    with pytest.raises(deal.PreContractError):
        _split_non_prose_passage_to_spans(too_long)
    with pytest.raises(deal.PreContractError):
        # Pytest run-locale pre is still ascii_bounded; CrossHair is None-only.
        split_passage_locale_runs_to_chunk_meta(
            "x",
            [LocaleTextRun(char_start=0, char_end=1, locale_bcp47="\x80\x00")],
            {},
            prose=True,
        )


def test_deal_passage_text_ok_pytest_accepts_normal_text() -> None:
    """Pytest domain keeps str_bounded (NUL allowed); CrossHair rejects NUL/controls."""
    assert _deal_passage_text_ok("Hello\nworld")
    assert _deal_passage_text_ok("")
    assert _deal_passage_text_ok("\x00")  # pytest: str_bounded; CrossHair pre rejects


def test_chunk_meta_accepts_file_url_doc_url() -> None:
    """Pytest meta values are str_bounded(DEAL_MAX_SOURCE); TOKEN (64) is too short."""
    url = "file:///home/runner/work/writeragent/writeragent/tmp/pytest-0/test_paragraph_chunks_from_path0/a.odt"
    assert len(url) > 64
    assert _deal_chunk_base_meta_ok({"doc_url": url, "para_index": 0, "file_mtime": 1.0})
    # Non-ascii meta is legal under pytest; CrossHair still ascii_bounded TOKEN.
    assert _deal_chunk_base_meta_ok({"": "\x80"})


def test_merge_small_sentences_rejects_out_of_order_spans() -> None:
    """CrossHair: out-of-order triples folded to ``(1, 0)``; pre requires sequential spans."""
    expect_pre_or_body(
        lambda: _merge_small_sentences_to_spans("", [(1, 1, ""), (0, 0, "")], min_chunk=1),
        body_result=[(1, 1)],
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
@settings(max_examples=vhs_max_examples(50, 500), deadline=None)
def test_hypothesis_merge_small_sentences_to_spans_invariants(
    data: tuple[str, list[tuple[int, int, str]]], min_chunk: int
) -> None:
    """Phase 8 #4: merged spans stay in-bounds, non-overlapping, monotonic."""
    passage, sentences = data
    spans = _merge_small_sentences_to_spans(passage, sentences, min_chunk=min_chunk)
    assert isinstance(spans, list)
    prev_end = -1
    for start, end in spans:
        assert 0 <= start <= end <= len(passage)
        assert start >= prev_end
        prev_end = end


@given(
    st.text(max_size=80),
    st.lists(st.tuples(st.integers(min_value=0, max_value=20), st.integers(min_value=0, max_value=20)), max_size=12),
    st.dictionaries(st.text(max_size=12), st.text(max_size=12), max_size=4),
)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_meta_chunks_from_spans_invariants(
    passage: str, raw_spans: list[tuple[int, int]], base_meta: dict[str, str]
) -> None:
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
