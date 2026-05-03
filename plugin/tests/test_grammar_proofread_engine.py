# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar proofread engine (JSON, offsets, cache)."""

from __future__ import annotations

from plugin.modules.writer import grammar_proofread_engine as eng


def test_parse_grammar_json_empty() -> None:
    assert eng.parse_grammar_json("") == []
    assert eng.parse_grammar_json("not json") == []


def test_parse_grammar_json_valid() -> None:
    raw = '{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]}'
    items = eng.parse_grammar_json(raw)
    assert len(items) == 1
    assert items[0]["wrong"] == "they is"
    assert items[0]["correct"] == "they are"


def test_normalize_errors_for_text() -> None:
    full = "Hello they is here."
    items = [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}]
    norms = eng.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 1
    assert full[norms[0].n_error_start : norms[0].n_error_start + norms[0].n_error_length] == "they is"


def test_normalize_errors_respects_slice() -> None:
    full = "xx they is yy"
    items = [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": ""}]
    norms = eng.normalize_errors_for_text(full, 3, 12, items)
    assert len(norms) == 1
    assert norms[0].n_error_start >= 3


def test_sentence_cache_roundtrip() -> None:
    eng.cache_clear()
    assert eng.cache_get_sentence("en-US", "Hello world.") is None
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "wa_test"}]
    eng.cache_put_sentence("en-US", "Hello world.", errors)
    got = eng.cache_get_sentence("en-US", "Hello world.")
    assert got is not None
    assert len(got) == 1
    assert got[0]["n_error_start"] == 0
    assert eng.cache_get_sentence("fr-FR", "Hello world.") is None
    eng.cache_clear()


def test_sentence_cache_trailing_whitespace() -> None:
    """'Hello.' and 'Hello. ' share the same cache key (preserved behavior)."""
    eng.cache_clear()
    eng.cache_put_sentence("en-US", "Hello.", [{"n_error_start": 0, "n_error_length": 5}])
    got = eng.cache_get_sentence("en-US", "Hello. ")
    assert got is not None
    assert len(got) == 1
    eng.cache_clear()


def test_normalize_for_sentence_cache() -> None:
    """Test that first terminator is preserved but additional trailing punctuation is ignored."""
    norm = eng._normalize_for_sentence_cache
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
    eng.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "test"}]

    # Put with canonical form, get with extra punctuation
    eng.cache_put_sentence("en-US", "Hello.", errors)
    got1 = eng.cache_get_sentence("en-US", "Hello...")
    assert got1 is not None
    assert len(got1) == 1
    assert got1[0]["n_error_start"] == 0

    # Put with question, get with extra punctuation
    eng.cache_put_sentence("en-US", "Hello?", errors)
    got2 = eng.cache_get_sentence("en-US", "Hello?...!!")
    assert got2 is not None
    assert len(got2) == 1

    # Different first terminator should be cache miss
    assert eng.cache_get_sentence("en-US", "Hello?") is not None  # still in cache
    eng.cache_clear()  # reset for next test


def test_sentence_cache_trailing_punctuation_clipping() -> None:
    """Test that errors only on redundant trailing punctuation are clipped."""
    eng.cache_clear()
    # Error that would only be on the extra dots (start=6 is past "Hello." which is len 6)
    errors = [{"n_error_start": 6, "n_error_length": 3}]
    eng.cache_put_sentence("en-US", "Hello....", errors)
    got = eng.cache_get_sentence("en-US", "Hello.")
    assert got is not None
    assert len(got) == 0, "Error beyond canonical length should be clipped"

    # Error that spans into the redundant trailing punctuation gets trimmed
    eng.cache_put_sentence("en-US", "Hi there....", [{"n_error_start": 3, "n_error_length": 10}])
    got2 = eng.cache_get_sentence("en-US", "Hi there.")
    assert got2 is not None
    assert len(got2) == 1
    # "Hi there." is len 9, error starts at 3, so max length is 9-3=6
    assert got2[0]["n_error_start"] == 3
    assert got2[0]["n_error_length"] == 6

    # Error fully within canonical length is untouched
    eng.cache_put_sentence("en-US", "Bad grammar...", [{"n_error_start": 0, "n_error_length": 3}])
    got3 = eng.cache_get_sentence("en-US", "Bad grammar.")
    assert got3 is not None
    assert len(got3) == 1
    assert got3[0]["n_error_length"] == 3
    eng.cache_clear()


def test_sentence_cache_different_first_terminator_no_cross_hit() -> None:
    """Sentences with different first terminators must not share cache."""
    eng.cache_clear()
    eng.cache_put_sentence("en-US", "Done.", [{"n_error_start": 0, "n_error_length": 4}])
    # "Done?" has a different first terminator — must be a miss
    assert eng.cache_get_sentence("en-US", "Done?") is None
    # "Done!" also different
    assert eng.cache_get_sentence("en-US", "Done!") is None
    # "Done." still a hit
    assert eng.cache_get_sentence("en-US", "Done.") is not None
    eng.cache_clear()


def test_ignore_rules_snapshot() -> None:
    eng.ignore_rules_clear()
    eng.ignore_rule_add("rule_a")
    assert "rule_a" in eng.ignored_rules_snapshot()
    eng.ignore_rules_clear()
    assert eng.ignored_rules_snapshot() == set()


def test_overlap_forward_expansion() -> None:
    full = "I went to the store."
    # LLM flagged "to" but correct was "to the" (which overlaps with " the store.")
    items = [{"wrong": "to", "correct": "to the", "type": "grammar"}]
    norms = eng.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op because expanding 'to' -> 'to the' matches 'correct'"

    items2 = [{"wrong": "to", "correct": "into the", "type": "grammar"}]
    norms2 = eng.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    err = norms2[0]
    expanded_wrong = full[err.n_error_start : err.n_error_start + err.n_error_length]
    assert expanded_wrong == "to the", f"Expected 'to the' but got {expanded_wrong}"


def test_overlap_backward_expansion() -> None:
    full = "He is a good man."
    # LLM flagged "good" but correct was "a good"
    items = [{"wrong": "good", "correct": "a good", "type": "grammar"}]
    norms = eng.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op because 'a good' was already there"

    items2 = [{"wrong": "good", "correct": "a very good", "type": "grammar"}]
    norms2 = eng.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    err = norms2[0]
    expanded_wrong = full[err.n_error_start : err.n_error_start + err.n_error_length]
    assert expanded_wrong == "a good"
    assert err.suggestions == ("a very good",)


def test_sentence_cache_incomplete_prefix_compaction() -> None:
    """Incomplete growing prefixes collapse to one LRU entry (newest wins)."""
    eng.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 3, "rule_identifier": "test"}]

    # Growing incomplete sentence
    eng.cache_put_sentence("en-US", "The", errors)
    eng.cache_put_sentence("en-US", "The qu", errors)
    eng.cache_put_sentence("en-US", "The quick", errors)
    eng.cache_put_sentence("en-US", "The quick brown", errors)

    # Should have collapsed to only the longest one
    assert eng.cache_get_sentence("en-US", "The") is None
    assert eng.cache_get_sentence("en-US", "The qu") is None
    assert eng.cache_get_sentence("en-US", "The quick") is None
    got = eng.cache_get_sentence("en-US", "The quick brown")
    assert got is not None
    assert len(got) == 1

    # Complete sentence should not be affected
    eng.cache_put_sentence("en-US", "The quick brown fox.", errors)
    assert eng.cache_get_sentence("en-US", "The quick brown fox.") is not None
    assert eng.cache_get_sentence("en-US", "The quick brown") is not None  # still there

    eng.cache_clear()


def test_sentence_cache_complete_not_evicted() -> None:
    """Complete sentences are protected from eviction by incomplete ones."""
    eng.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "test"}]

    eng.cache_put_sentence("en-US", "Hello world.", errors)  # complete
    eng.cache_put_sentence("en-US", "Hello world", errors)   # incomplete
    eng.cache_put_sentence("en-US", "Hello world is", errors)  # incomplete

    # Complete one should still be present
    assert eng.cache_get_sentence("en-US", "Hello world.") is not None
    # The incomplete ones may or may not collapse depending on exact order,
    # but the complete is never evicted.
    eng.cache_clear()


def test_sentence_cache_locale_isolation() -> None:
    """Different locales do not interfere with prefix compaction."""
    eng.cache_clear()
    errors = [{"n_error_start": 0, "n_error_length": 3, "rule_identifier": "test"}]

    eng.cache_put_sentence("en-US", "Hello", errors)
    eng.cache_put_sentence("fr-FR", "Bonjour", errors)
    eng.cache_put_sentence("en-US", "Hello there", errors)  # should evict "Hello" for en-US only

    assert eng.cache_get_sentence("en-US", "Hello") is None
    assert eng.cache_get_sentence("fr-FR", "Bonjour") is not None  # unaffected
    eng.cache_clear()
