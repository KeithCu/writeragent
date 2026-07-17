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
            res.startPos = m.start()
            res.endPos = m.end()
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


def test_normalize_errors_preserves_harper_rule_identifier() -> None:
    full = "hello world."
    items = [
        {
            "wrong": "hello",
            "correct": "Hello",
            "type": "SentenceCapitalization",
            "reason": "Start with a capital letter.",
            "rule_identifier": "harper||SentenceCapitalization",
        }
    ]
    norms = gt.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 1
    assert norms[0].rule_identifier == "harper||SentenceCapitalization"
    assert norms[0].rule_identifier != "wa_g_rule||Start with a capital letter."


def test_normalize_harper_errors_uses_native_offsets_and_explains_blank_fixes() -> None:
    text = "can it    finded enny misteaks ?"
    items = [
        {
            "wrong": "    ",
            "correct": " ",
            "n_error_start": 6,
            "n_error_length": 4,
            "type": "Spaces",
            "reason": "There are 4 spaces where there should be only one.",
            "short_comment": "There are 4 spaces where there should be only one.",
            "full_comment": "There are 4 spaces where there should be only one.",
            "rule_identifier": "harper||Spaces",
            "suggestions": [" "],
        },
        {
            "wrong": " ",
            "correct": "",
            "n_error_start": 30,
            "n_error_length": 1,
            "type": "Spaces",
            "reason": "Unnecessary space at the end of the sentence.",
            "short_comment": "Unnecessary space at the end of the sentence.",
            "full_comment": "Unnecessary space at the end of the sentence.",
            "rule_identifier": "harper||Spaces",
            "suggestions": [""],
        },
        {
            "wrong": "enny",
            "correct": "envy",
            "n_error_start": 17,
            "n_error_length": 4,
            "type": "SpellCheck",
            "reason": "Did you mean to spell `enny` this way?",
            "short_comment": "Did you mean to spell `enny` this way?",
            "full_comment": "Did you mean to spell `enny` this way?",
            "rule_identifier": "harper||SpellCheck",
            "suggestions": ["envy", "jenny"],
        },
        {
            "wrong": "finded",
            "correct": "find ed",
            "n_error_start": 10,
            "n_error_length": 6,
            "type": "SplitWords",
            "reason": "`finded` should probably be written as `find ed`.",
            "short_comment": "`finded` should probably be written as `find ed`.",
            "full_comment": "`finded` should probably be written as `find ed`.",
            "rule_identifier": "harper||SplitWords",
            "suggestions": ["find ed", "found"],
        },
    ]

    norms = gt.normalize_errors_for_text(text, 0, len(text), items)

    assert [(item.n_error_start, item.n_error_length) for item in norms] == [(6, 4), (30, 1), (17, 4), (10, 6)]
    assert norms[0].suggestions == (" ",)
    assert "replace with one space" in norms[0].short_comment
    assert norms[1].suggestions == ("",)
    assert "delete the highlighted text" in norms[1].short_comment
    assert norms[2].suggestions == ("envy", "jenny")
    assert "Choose a replacement below" in norms[2].short_comment


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
    assert sents[0][1] == "Hello.  "
    assert sents[1][1] == "There."


def test_split_abbreviation_not_sentence_boundary() -> None:
    # Whitelisted abbreviations and initials are not sentence boundaries
    sents = gt.split_into_sentences(None, "en-US", "Dr. Johnson asked how I am.")
    assert len(sents) == 1, f"Expected 1 sentence, got {len(sents)}: {sents}"
    assert sents[0][1] == "Dr. Johnson asked how I am."

    sents = gt.split_into_sentences(None, "en-US", "Mr. Smith went to the U.S.A. last year.")
    assert len(sents) == 1, f"Expected 1 sentence, got {len(sents)}: {sents}"

    sents = gt.split_into_sentences(None, "en-US", "This is approx. the value.")
    assert len(sents) == 1, f"Expected 1 sentence for approx, got {len(sents)}: {sents}"

    # Multilingual tests:
    # German z.B.
    sents = gt.split_into_sentences(None, "de-DE", "Das ist z.B. ein Test.")
    assert len(sents) == 1, f"Expected 1 sentence for German z.B., got {len(sents)}: {sents}"

    # Russian ул.
    sents = gt.split_into_sentences(None, "ru-RU", "Мы живем на ул. Ленина.")
    assert len(sents) == 1, f"Expected 1 sentence for Russian ул., got {len(sents)}: {sents}"

    # Verify normal sentence splits don't get merged
    sents = gt.split_into_sentences(None, "en-US", "This is a error. How long does it take?")
    assert len(sents) == 2, f"Expected 2 sentences, got {len(sents)}: {sents}"


def test_split_into_sentences_terminates_when_bi_stuck_on_abbrev() -> None:
    # Regression: text like "...UNO. <content>" was observed in production to make
    # bi.endOfSentence return a position <= the abbreviation period the inner loop was
    # trying to skip past, spinning forever. The main thread froze inside doProofreading
    # so LibreOffice could not close, and the debug log grew to hundreds of MB.
    text = "Foo UNO. bar baz."
    period_idx = text.index(".")  # 7

    call_count = {"n": 0}

    class StuckBI:
        def endOfSentence(self, _t, pos, _locale):
            call_count["n"] += 1
            assert call_count["n"] < 50, f"split_into_sentences looped ({call_count['n']} endOfSentence calls)"
            if pos <= period_idx:
                return period_idx + 1
            return period_idx

    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(StuckBI(), "en-US")):
        sents = gt.split_into_sentences(None, "en-US", text)

    assert sents, "must return at least one sentence span"
    last_start, last_text = sents[-1]
    assert last_start + len(last_text) == len(text)


def test_split_into_sentences_terminates_when_bi_returns_same_pos() -> None:
    # Defends the outer-loop guard at grammar_proofread_text.py "if end_pos <= pos".
    # Realistic LO limitation: BreakIterator for a script/locale whose ICU data is not
    # installed (e.g. Thai on a US system, rare African scripts) can return the same
    # position it was given, signalling "no sentence boundary found here".
    text = "Some text without any terminator BI understands"

    class StuckBI:
        calls = 0

        def endOfSentence(self, _t, pos, _locale):
            type(self).calls += 1
            assert type(self).calls < 50, f"split_into_sentences looped ({type(self).calls} endOfSentence calls)"
            return pos

    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(StuckBI(), "en-US")):
        sents = gt.split_into_sentences(None, "en-US", text)

    assert sents, "must return at least one sentence span"
    last_start, last_text = sents[-1]
    assert last_start + len(last_text) == len(text)


def test_tokenize_terminates_when_bi_word_boundary_does_not_advance() -> None:
    # Defends the _tokenize guard "if res.endPos <= start: ... break".
    # Without this guard, an under-equipped BreakIterator that returns endPos == start
    # would spin _tokenize forever during normalize_errors_for_text overlap expansion.
    text = "alpha beta gamma"

    class StuckWordBI:
        calls = 0

        def getWordBoundary(self, _t, pos, _locale, _wt, _dir):
            type(self).calls += 1
            assert type(self).calls < 50, f"_tokenize looped ({type(self).calls} getWordBoundary calls)"
            res = MagicMock()
            res.startPos = pos
            res.endPos = pos
            return res

        def endOfSentence(self, t, _pos, _locale):
            return len(t)

    toks = gt._tokenize(text, StuckWordBI(), "en-US")
    assert toks == [text], "stuck BI should produce a single fallback token covering the rest"


def test_split_into_sentences_handles_bi_past_end() -> None:
    # Some BreakIterator implementations may return a position past len(text)
    # (one-past-end with extra slack). Python slice clamping makes this safe;
    # this test pins that contract so a future refactor that adds explicit
    # indexing (e.g. text[end_pos] instead of slicing) would surface here.
    text = "Short text."

    class PastEndBI:
        def endOfSentence(self, t, _pos, _locale):
            return len(t) + 5

    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(PastEndBI(), "en-US")):
        sents = gt.split_into_sentences(None, "en-US", text)

    assert len(sents) == 1
    assert sents[0][0] == 0
    assert sents[0][1] == text


def test_split_into_sentences_thai_text_on_non_thai_locale() -> None:
    # Realistic LO limitation: user types Thai script in a document whose CharLocale
    # is en-US (or any non-Thai locale). BI uses en-US rules, finds no Latin sentence
    # terminator in the Thai text, and returns len(text) immediately. The whole buffer
    # should become one sentence and the call must terminate cleanly (no abbreviation
    # heuristic confusion from Thai characters).
    text = "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35 \u0e04\u0e23\u0e31\u0e1a"  # "sawatdi khrap"

    class WholeBufferBI:
        def endOfSentence(self, t, _pos, _locale):
            return len(t)

    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(WholeBufferBI(), "en-US")):
        sents = gt.split_into_sentences(None, "en-US", text)

    assert len(sents) == 1
    assert sents[0][0] == 0
    assert sents[0][1] == text


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

