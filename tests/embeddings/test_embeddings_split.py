# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_split."""

from __future__ import annotations

import importlib.util

import pytest

from plugin.embeddings import embeddings_split as split_mod


def _chunk_texts(text: str, *, prose: bool = True) -> list[str]:
    rows = split_mod.split_passage_to_chunk_meta(text, {"doc_url": "file:///x"}, prose=prose)
    return [str(row["text"]) for row in rows]


def test_merge_small_sentences_glue_to_floor(monkeypatch: pytest.MonkeyPatch):
    passage = "Yes. No. Why? OK."
    sentences = [
        (0, 4, "Yes."),
        (5, 8, "No."),
        (9, 14, "Why?"),
        (15, 19, "OK."),
    ]
    monkeypatch.setattr(split_mod, "split_passage_to_sentences", lambda _text, locale=None: sentences)
    chunks = _chunk_texts(passage)
    assert len(chunks) == 1
    assert chunks[0] == passage


def test_long_sentence_with_trailing_short_folds_tail(monkeypatch: pytest.MonkeyPatch):
    long_sent = "A" * split_mod.MIN_CHUNK
    short = "Hi."
    passage = f"{long_sent} {short}"
    sentences = [
        (0, len(long_sent), long_sent),
        (len(long_sent) + 1, len(long_sent) + 1 + len(short), short),
    ]
    monkeypatch.setattr(split_mod, "split_passage_to_sentences", lambda _text, locale=None: sentences)
    chunks = _chunk_texts(passage)
    assert len(chunks) == 1
    assert chunks[0] == passage


def test_trailing_small_remainder_folds_into_previous(monkeypatch: pytest.MonkeyPatch):
    first = "B" * split_mod.MIN_CHUNK
    tail = "End."
    passage = f"{first} {tail}"
    first_end = len(first)
    tail_start = first_end + 1
    sentences = [
        (0, first_end, first),
        (tail_start, tail_start + len(tail), tail),
    ]
    monkeypatch.setattr(split_mod, "split_passage_to_sentences", lambda _text, locale=None: sentences)
    chunks = _chunk_texts(passage)
    assert len(chunks) == 1
    assert chunks[0] == passage


def test_run_on_sentence_stays_one_chunk(monkeypatch: pytest.MonkeyPatch):
    run_on = "word " * 200
    passage = run_on.strip()
    sentences = [(0, len(passage), passage)]
    monkeypatch.setattr(split_mod, "split_passage_to_sentences", lambda _text, locale=None: sentences)
    chunks = _chunk_texts(passage)
    assert len(chunks) == 1
    assert chunks[0] == passage


def test_offsets_match_passage_slices(monkeypatch: pytest.MonkeyPatch):
    passage = "Alpha. Beta."
    sentences = [(0, 6, "Alpha."), (7, 12, "Beta.")]
    monkeypatch.setattr(split_mod, "split_passage_to_sentences", lambda _text, locale=None: sentences)
    rows = split_mod.split_passage_to_chunk_meta(passage, {"doc_url": "file:///x"}, prose=True)
    for row in rows:
        start = int(row["char_start"])
        end = int(row["char_end"])
        assert passage[start:end] == row["text"]


def test_non_prose_uses_recursive_char_splitter(monkeypatch: pytest.MonkeyPatch):
    long_piece = "x" * (split_mod.CHUNK_SIZE + 10)
    fake_splitter = type("S", (), {"split_text": lambda self, text: [text[: split_mod.CHUNK_SIZE], text[split_mod.CHUNK_SIZE :]]})()
    monkeypatch.setattr(split_mod, "_import_splitter", lambda: fake_splitter)
    rows = split_mod.split_passage_to_chunk_meta(long_piece, {"doc_url": "file:///x"}, prose=False)
    assert len(rows) == 2
    assert rows[0]["char_start"] == 0
    assert rows[0]["char_end"] == split_mod.CHUNK_SIZE


@pytest.mark.skipif(importlib.util.find_spec("icu4py") is None, reason="icu4py not installed")
def test_icu4py_integration_splits_sentences():
    passage = 'You asked "Why?". We answered "Why not?"'
    sentences = split_mod.split_passage_to_sentences(passage)
    assert len(sentences) >= 2
    rebuilt = "".join(piece for _start, _end, piece in sentences)
    assert rebuilt == passage


def test_locale_bcp47_forwarded_to_icu(monkeypatch: pytest.MonkeyPatch):
    passage = "Alpha. Beta."
    seen: list[str] = []

    def _fake_split(_text: str, locale: str = split_mod.DEFAULT_SENTENCE_LOCALE) -> list[tuple[int, int, str]]:
        seen.append(locale)
        return [(0, 6, "Alpha."), (7, 12, "Beta.")]

    monkeypatch.setattr(split_mod, "split_passage_to_sentences", _fake_split)
    split_mod.split_passage_to_chunk_meta(passage, {"doc_url": "file:///x"}, prose=True, locale_bcp47="de-DE")
    assert seen == ["de_DE@ss=standard"]


def test_whitespace_locale_uses_grammar_split(monkeypatch: pytest.MonkeyPatch):
    passage = "word one word two word three"
    called = False

    def _fake_whitespace(_passage: str) -> list[tuple[int, int, str]]:
        nonlocal called
        called = True
        return [(0, 5, "word "), (5, 9, "one "), (9, 14, "two "), (14, 19, "three")]

    monkeypatch.setattr(
        "plugin.writer.locale.grammar_proofread_locale.normalize_detected_bcp47",
        lambda tag: "th-TH" if tag else None,
    )
    monkeypatch.setattr(
        "plugin.writer.locale.grammar_proofread_locale.is_whitespace_sentence_locale",
        lambda key: key.startswith("th"),
    )
    monkeypatch.setattr(split_mod, "_split_passage_whitespace_to_sentences", _fake_whitespace)
    rows = split_mod.split_passage_to_chunk_meta(
        passage,
        {"doc_url": "file:///x"},
        prose=True,
        locale_bcp47="th-TH",
    )
    assert called
