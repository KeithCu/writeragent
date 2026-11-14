# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for grammar text processing (sentence splitting, BreakIterator)."""

from __future__ import annotations

from typing import Any
from plugin.writer.locale import grammar_proofread_text as gt
from plugin.testing_runner import native_test


@native_test
def test_split_basic_two_sentences_native(ctx: Any) -> None:
    result = gt.split_into_sentences(ctx, "en-US", "Hello world. This is fine.")
    assert len(result) == 2
    assert result[0][1].strip() == "Hello world."
    assert result[1][1].strip() == "This is fine."


@native_test
def test_split_multilingual_terminators_native(ctx: Any) -> None:
    result = gt.split_into_sentences(ctx, "ja-JP", "これは文です。 次の文。")
    assert len(result) == 2


@native_test
def test_split_thai_spaces_native(ctx: Any) -> None:
    text = "สวัสดีครับ ผมชื่อสมชาย ยินดีที่ได้รู้จัก"
    result = gt.split_into_sentences(ctx, "th-TH", text)
    assert [t.strip() for _o, t in result] == ["สวัสดีครับ", "ผมชื่อสมชาย", "ยินดีที่ได้รู้จัก"]


@native_test
def test_split_abbreviation_heuristic_native(ctx: Any) -> None:
    text = "Prof. Smith went to Washington. Next sentence."
    result = gt.split_into_sentences(ctx, "en-US", text)
    assert len(result) == 2
    assert result[0][1].strip() == "Prof. Smith went to Washington."
    assert result[1][1].strip() == "Next sentence."


@native_test
def test_overlap_thai_native(ctx: Any) -> None:
    full = "ผมไปที่ร้านค้า"
    # use a correction that isn't a no-op after expansion.
    items = [{"wrong": "ไป", "correct": "เดินไปที่", "type": "grammar"}]
    norms_native = gt.normalize_errors_for_text(full, items, ctx=ctx, loc_key="th-TH")
    assert len(norms_native) == 1
    err = norms_native[0]
    assert full[err.n_error_start : err.n_error_start + err.n_error_length] == "ไปที่"


@native_test
def test_dialogue_fire_merge_native(ctx: Any) -> None:
    """P25: real BreakIterator splits at '!' inside quotes; merge must rejoin the utterance."""
    text = '"Fire! Fire!" he yelled. Next sentence.'
    spans = gt.candidate_sentence_spans_for_proofreading(ctx, "en-US", text)
    bodies = [t.strip() for _start, _end, t in spans]
    assert any("Fire! Fire!" in t for t in bodies)
    assert any("Next sentence." in t for t in bodies)
    assert '"Fire!' not in bodies


@native_test
def test_split_quoted_abbreviation_native(ctx: Any) -> None:
    text = 'He said "Dr. Smith went home." Next one.'
    result = gt.split_into_sentences(ctx, "en-US", text)
    bodies = [t.strip() for _o, t in result]
    assert any("Dr. Smith" in b for b in bodies)
    assert not any(b.endswith("Dr.") for b in bodies)
