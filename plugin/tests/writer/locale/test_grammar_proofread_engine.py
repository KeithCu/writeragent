# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar proofread engine (JSON, offsets, cache)."""

from __future__ import annotations

from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale import grammar_proofread_locale as gl
from plugin.writer.locale import grammar_proofread_text as gt
from plugin.writer.locale.grammar_proofread_cache import _normalize_for_sentence_cache

import pytest
from unittest.mock import MagicMock, patch

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



def test_parse_grammar_json_empty() -> None:
    assert gt.parse_grammar_json("") == []
    assert gt.parse_grammar_json("not json") == []


def test_parse_grammar_json_valid() -> None:
    raw = '{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]}'
    items = gt.parse_grammar_json(raw)
    assert len(items) == 1
    assert items[0]["wrong"] == "they is"
    assert items[0]["correct"] == "they are"


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
    """Two occurrences of the same ``wrong`` substring map to successive positions when listed in order."""
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
    """Split path attaches the matched whitespace run so double spaces are visible to the LLM."""
    sents = gt.split_into_sentences(None, "en-US", "Hello.  There.")
    assert len(sents) == 2
    assert sents[0][1].startswith("Hello.")
    assert "  " in sents[0][1]


def test_sentence_cache_roundtrip() -> None:
    gc.cache_clear()
    assert gc.cache_get_sentence("en-US", "Hello world.") is None
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "wa_test"}]
    gc.cache_put_sentence("en-US", "Hello world.", errors)
    got = gc.cache_get_sentence("en-US", "Hello world.")
    assert got is not None
    assert len(got) == 1
    assert got[0]["n_error_start"] == 0
    assert gc.cache_get_sentence("fr-FR", "Hello world.") is None
    gc.cache_clear()


def test_sentence_cache_trailing_whitespace() -> None:
    """'Hello.' and 'Hello. ' share the same cache key (preserved behavior)."""
    gc.cache_clear()
    gc.cache_put_sentence("en-US", "Hello.", [{"n_error_start": 0, "n_error_length": 5}])
    got = gc.cache_get_sentence("en-US", "Hello. ")
    assert got is not None
    assert len(got) == 1
    gc.cache_clear()


def test_normalize_for_sentence_cache() -> None:
    """Test that first terminator is preserved but additional trailing punctuation is ignored."""
    norm = _normalize_for_sentence_cache
    assert norm("Hello.") == "Hello."
    assert norm("Hello. ") == "Hello."
    assert norm("Hello...") == "Hello."
    assert norm("Hello....") == "Hello."
    assert norm("Hello?") == "Hello?"
    assert norm("Hello?...") == "Hello?"
    assert norm("Hello?!!") == "Hello?"
    assert norm("Are you there?") == "Are you there?"
    assert norm("Are you there?!") == "Are you there?"
    assert norm("Really!!!") == "Really!"
    assert norm("Wait……") == "Wait…"
    assert norm("结束。") == "结束。"
    assert norm("结束。！？") == "结束。"
    # Internal punctuation and no trailing punctuation unchanged
    assert norm("Hello, world.") == "Hello, world."
    assert norm("What? No!") == "What? No!"
    assert norm("") == ""
    assert norm("   ") == ""
    # No terminator at all — returned as-is
    assert norm("Hello world") == "Hello world"
    assert norm("Hello. world") == "Hello. world"
    # Single terminator with trailing whitespace
    assert norm("Hello?\n") == "Hello?"
    assert norm("Done!  \t") == "Done!"


def test_sentence_cache_trailing_punctuation() -> None:
    """Test cache behavior with first-terminator preservation.

    "Hello." and "Hello..." share cache; "Hello?" and "Hello?..." share cache.
    First terminator is significant.
    """
    gc.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "test"}]

    # Put with canonical form, get with extra punctuation
    gc.cache_put_sentence("en-US", "Hello.", errors)
    got1 = gc.cache_get_sentence("en-US", "Hello...")
    assert got1 is not None
    assert len(got1) == 1
    assert got1[0]["n_error_start"] == 0

    # Put with question, get with extra punctuation
    gc.cache_put_sentence("en-US", "Hello?", errors)
    got2 = gc.cache_get_sentence("en-US", "Hello?...!!")
    assert got2 is not None
    assert len(got2) == 1

    # Different first terminator should be cache miss
    assert gc.cache_get_sentence("en-US", "Hello?") is not None  # still in cache
    gc.cache_clear()  # reset for next test


def test_sentence_cache_trailing_punctuation_clipping() -> None:
    """Test that errors only on redundant trailing punctuation are clipped."""
    gc.cache_clear()
    # Error that would only be on the extra dots (start=6 is past "Hello." which is len 6)
    errors = [{"n_error_start": 6, "n_error_length": 3}]
    gc.cache_put_sentence("en-US", "Hello....", errors)
    got = gc.cache_get_sentence("en-US", "Hello.")
    assert got is not None
    assert len(got) == 0, "Error beyond canonical length should be clipped"

    # Error that spans into the redundant trailing punctuation gets trimmed
    gc.cache_put_sentence("en-US", "Hi there....", [{"n_error_start": 3, "n_error_length": 10}])
    got2 = gc.cache_get_sentence("en-US", "Hi there.")
    assert got2 is not None
    assert len(got2) == 1
    # "Hi there." is len 9, error starts at 3, so max length is 9-3=6
    assert got2[0]["n_error_start"] == 3
    assert got2[0]["n_error_length"] == 6

    # Error fully within canonical length is untouched
    gc.cache_put_sentence("en-US", "Bad grammar...", [{"n_error_start": 0, "n_error_length": 3}])
    got3 = gc.cache_get_sentence("en-US", "Bad grammar.")
    assert got3 is not None
    assert len(got3) == 1
    assert got3[0]["n_error_length"] == 3
    gc.cache_clear()


def test_sentence_cache_different_first_terminator_no_cross_hit() -> None:
    """Sentences with different first terminators must not share cache."""
    gc.cache_clear()
    gc.cache_put_sentence("en-US", "Done.", [{"n_error_start": 0, "n_error_length": 4}])
    # "Done?" has a different first terminator — must be a miss
    assert gc.cache_get_sentence("en-US", "Done?") is None
    # "Done!" also different
    assert gc.cache_get_sentence("en-US", "Done!") is None
    # "Done." still a hit
    assert gc.cache_get_sentence("en-US", "Done.") is not None
    gc.cache_clear()


def test_ignore_rules_snapshot() -> None:
    gc.ignore_rules_clear()
    gc.ignore_rule_add("rule_a")
    assert "rule_a" in gc.ignored_rules_snapshot()
    gc.ignore_rules_clear()
    assert gc.ignored_rules_snapshot() == set()


def test_overlap_forward_expansion() -> None:
    full = "I went to the store."
    # LLM flagged "to" but correct was "to the" (which overlaps with " the store.")
    items = [{"wrong": "to", "correct": "to the", "type": "grammar"}]
    norms = gt.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op because expanding 'to' -> 'to the' matches 'correct'"

    items2 = [{"wrong": "to", "correct": "into the", "type": "grammar"}]
    norms2 = gt.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    err = norms2[0]
    expanded_wrong = full[err.n_error_start : err.n_error_start + err.n_error_length]
    assert expanded_wrong == "to the", f"Expected 'to the' but got {expanded_wrong}"


def test_overlap_backward_expansion() -> None:
    full = "He is a good man."
    # LLM flagged "good" but correct was "a good"
    items = [{"wrong": "good", "correct": "a good", "type": "grammar"}]
    norms = gt.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op because 'a good' was already there"

    items2 = [{"wrong": "good", "correct": "a very good", "type": "grammar"}]
    norms2 = gt.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    err = norms2[0]
    expanded_wrong = full[err.n_error_start : err.n_error_start + err.n_error_length]
    assert expanded_wrong == "a good"
    assert err.suggestions == ("a very good",)


def test_sentence_cache_incomplete_prefix_compaction() -> None:
    """Incomplete growing prefixes collapse to one LRU entry (newest wins)."""
    gc.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 3, "rule_identifier": "test"}]

    # Growing incomplete sentence
    gc.cache_put_sentence("en-US", "The", errors)
    gc.cache_put_sentence("en-US", "The qu", errors)
    gc.cache_put_sentence("en-US", "The quick", errors)
    gc.cache_put_sentence("en-US", "The quick brown", errors)

    # Should have collapsed to only the longest one
    assert gc.cache_get_sentence("en-US", "The") is None
    assert gc.cache_get_sentence("en-US", "The qu") is None
    assert gc.cache_get_sentence("en-US", "The quick") is None
    got = gc.cache_get_sentence("en-US", "The quick brown")
    assert got is not None
    assert len(got) == 1

    # Complete sentence should not be affected
    gc.cache_put_sentence("en-US", "The quick brown fox.", errors)
    assert gc.cache_get_sentence("en-US", "The quick brown fox.") is not None
    assert gc.cache_get_sentence("en-US", "The quick brown") is not None  # still there

    gc.cache_clear()


def test_sentence_cache_complete_not_evicted() -> None:
    """Complete sentences are protected from eviction by incomplete ones."""
    gc.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "test"}]

    gc.cache_put_sentence("en-US", "Hello world.", errors)  # complete
    gc.cache_put_sentence("en-US", "Hello world", errors)   # incomplete
    gc.cache_put_sentence("en-US", "Hello world is", errors)  # incomplete

    # Complete one should still be present
    assert gc.cache_get_sentence("en-US", "Hello world.") is not None
    # The incomplete ones may or may not collapse depending on exact order,
    # but the complete is never evicted.
    gc.cache_clear()


def test_sentence_cache_locale_isolation() -> None:
    """Different locales do not interfere with prefix compaction."""
    gc.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 3, "rule_identifier": "test"}]

    gc.cache_put_sentence("en-US", "Hello", errors)
    gc.cache_put_sentence("fr-FR", "Bonjour", errors)
    gc.cache_put_sentence("en-US", "Hello there", errors)  # should evict "Hello" for en-US only

    assert gc.cache_get_sentence("en-US", "Hello") is None
    assert gc.cache_get_sentence("fr-FR", "Bonjour") is not None  # unaffected
    gc.cache_clear()


def test_sentence_cache_key_prefix_and_identity_fp() -> None:
    assert gc.sentence_cache_key_prefix("en-US") == "sent|en-US|"
    fp = gc.sentence_identity_fp("Hello.")
    assert gc.make_sentence_key("en-US", "Hello.") == f"{gc.sentence_cache_key_prefix('en-US')}{fp}"


def test_should_evict_incomplete_prefix_predecessor() -> None:
    ev = gc.should_evict_incomplete_prefix_predecessor
    assert ev(other_complete=True, other_canon="Hi", new_canon="Hi there") is False
    assert ev(other_complete=False, other_canon="Hello", new_canon="Hell") is False
    assert ev(other_complete=False, other_canon="The qu", new_canon="The quick") is True
    assert ev(other_complete=False, other_canon="same", new_canon="same") is False


def test_extend_through_trailing_whitespace() -> None:
    assert gt.extend_through_trailing_whitespace("Hi.  There", 3) == 5
    assert gt.extend_through_trailing_whitespace("word", 4) == 4


def test_anchor_wrong_in_window() -> None:
    assert gt.anchor_wrong_in_window("hello bob there", "bob", 0) == 6
    assert gt.anchor_wrong_in_window("bob x bob", "bob", 0) == 0
    assert gt.anchor_wrong_in_window("bob x bob", "bob", 1) == 6
    assert gt.anchor_wrong_in_window("", "x", 0) is None
    assert gt.anchor_wrong_in_window("aa aa", "aa", 4) is None  # global match before search_pos — reject


def test_looks_complete_sentence_matches_proofreader_gating() -> None:
    """Same predicate as ``ai_grammar_proofreader`` — includes STerm chars beyond ASCII."""
    assert gl.looks_complete_sentence("Hello world.") is True
    assert gl.looks_complete_sentence("incomplete clause") is False
    # Armenian full stop U+0589 — was missing from the old narrow cache-only set
    assert gl.looks_complete_sentence("Բարև։") is True
    assert "։" in gl.GRAMMAR_SENTENCE_TERMINATORS
    # Trailing closer before terminal (same logic as proofreader + cache eviction)
    assert gl.looks_complete_sentence('She said "hello."') is True
    assert gl.last_meaningful_char('She said "hello."') == "."
