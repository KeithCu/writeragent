# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grammar text processing (sentence splitting, error normalization, offsets)."""

from __future__ import annotations

from plugin.writer.locale import grammar_proofread_text as gt
from plugin.testing_runner import native_test

import pytest
from unittest.mock import MagicMock, patch

# --- Mocks for non-native tests ---

class FakeBI:
    def getWordBoundary(self, text, pos, locale, wordType, bDirection):
        import re
        res = MagicMock()
        m = re.compile(r"\w+|\W+").match(text, pos)
        if m:
            res.startPos = pos + m.start()
            res.endPos = pos + m.end()
        else:
            res.startPos = pos
            res.endPos = len(text)
        return res
        
    def endOfSentence(self, text, pos, locale):
        import re
        m = re.search(r'[.!?]', text[pos:])
        if m:
            return pos + m.end()
        return len(text)

@pytest.fixture(autouse=True)
def mock_bi():
    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(FakeBI(), "en-US")):
        yield

# =============================================================================
# Unit Tests (Mocked)
# =============================================================================

def test_normalize_errors_for_text() -> None:
    full = "Hello they is here."
    items = [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}]
    norms = gt.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 1
    assert full[norms[0].n_error_start : norms[0].n_error_start + norms[0].n_error_length] == "they is"

def test_normalize_errors_respects_slice() -> None:
    full = "xx they is yy"
    items = [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": ""}]
    norms = gt.normalize_errors_for_text(full, 3, 12, items)
    assert len(norms) == 1
    assert norms[0].n_error_start >= 3

def test_normalize_errors_duplicate_wrong_two_occurrences_ordered() -> None:
    full = "bob x bob"
    items = [
        {"wrong": "bob", "correct": "Bob", "type": "spelling", "reason": ""},
        {"wrong": "bob", "correct": "Bob", "type": "spelling", "reason": ""},
    ]
    norms = gt.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 2
    assert norms[0].n_error_start == 0
    assert norms[1].n_error_start == 6

def test_split_includes_inter_sentence_whitespace() -> None:
    sents = gt.split_into_sentences(None, "en-US", "Hello.  There.")
    assert len(sents) == 2
    assert sents[0][1].startswith("Hello.")
    assert "  " in sents[0][1]

def test_overlap_forward_expansion() -> None:
    full = "I went to the store."
    items = [{"wrong": "to", "correct": "to the", "type": "grammar"}]
    norms = gt.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op"
    items2 = [{"wrong": "to", "correct": "into the", "type": "grammar"}]
    norms2 = gt.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    err = norms2[0]
    assert full[err.n_error_start : err.n_error_start + err.n_error_length] == "to the"

def test_overlap_backward_expansion() -> None:
    full = "He is a good man."
    items = [{"wrong": "good", "correct": "a good", "type": "grammar"}]
    norms = gt.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op"
    items2 = [{"wrong": "good", "correct": "a very good", "type": "grammar"}]
    norms2 = gt.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    assert full[norms2[0].n_error_start : norms2[0].n_error_start + norms2[0].n_error_length] == "a good"

def test_extend_through_trailing_whitespace() -> None:
    assert gt.extend_through_trailing_whitespace("Hi.  There", 3) == 5
    assert gt.extend_through_trailing_whitespace("word", 4) == 4

def test_anchor_wrong_in_window() -> None:
    assert gt.anchor_wrong_in_window("hello bob there", "bob", 0) == 6
    assert gt.anchor_wrong_in_window("bob x bob", "bob", 0) == 0
    assert gt.anchor_wrong_in_window("bob x bob", "bob", 1) == 6
    assert gt.anchor_wrong_in_window("", "x", 0) is None

