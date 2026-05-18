# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar proofread cache (sentence errors, LRU, prefix compaction)."""

from __future__ import annotations

import pytest
from unittest.mock import patch
from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale.grammar_proofread_cache import _normalize_for_sentence_cache

@pytest.fixture(autouse=True)
def clear_cache():
    gc.cache_clear()
    gc.clear_sentence_cache()
    gc.ignore_rules_clear()
    yield
    gc.cache_clear()
    gc.clear_sentence_cache()
    gc.ignore_rules_clear()

def test_sentence_cache_roundtrip() -> None:
    assert gc.cache_get_sentence("en-US", "Hello world.") is None
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "wa_test"}]
    gc.cache_put_sentence("en-US", "Hello world.", errors)
    got = gc.cache_get_sentence("en-US", "Hello world.")
    assert got is not None
    assert len(got) == 1
    assert got[0]["n_error_start"] == 0
    assert gc.cache_get_sentence("fr-FR", "Hello world.") is None

def test_sentence_cache_trailing_whitespace() -> None:
    """'Hello.' and 'Hello. ' share the same cache key (preserved behavior)."""
    gc.cache_put_sentence("en-US", "Hello.", [{"n_error_start": 0, "n_error_length": 5}])
    got = gc.cache_get_sentence("en-US", "Hello. ")
    assert got is not None
    assert len(got) == 1

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
    assert norm("Wait……") == "Wait\u2026"
    assert norm("结束。") == "结束。"
    assert norm("结束。！？") == "结束。"
    assert norm("Hello, world.") == "Hello, world."
    assert norm("What? No!") == "What? No!"
    assert norm("") == ""
    assert norm("   ") == ""
    assert norm("Hello world") == "Hello world"
    assert norm("Hello. world") == "Hello. world"
    assert norm("Hello?\n") == "Hello?"
    assert norm("Done!  \t") == "Done!"

def test_sentence_cache_trailing_punctuation() -> None:
    """Test cache behavior with first-terminator preservation."""
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "test"}]
    gc.cache_put_sentence("en-US", "Hello.", errors)
    got1 = gc.cache_get_sentence("en-US", "Hello...")
    assert got1 is not None
    assert len(got1) == 1
    gc.cache_put_sentence("en-US", "Hello?", errors)
    got2 = gc.cache_get_sentence("en-US", "Hello?...!!")
    assert got2 is not None
    assert len(got2) == 1
    assert gc.cache_get_sentence("en-US", "Hello?") is not None

def test_sentence_cache_trailing_punctuation_clipping() -> None:
    """Test that errors only on redundant trailing punctuation are clipped."""
    errors = [{"n_error_start": 6, "n_error_length": 3}]
    gc.cache_put_sentence("en-US", "Hello....", errors)
    got = gc.cache_get_sentence("en-US", "Hello.")
    assert got is not None
    assert len(got) == 0, "Error beyond canonical length should be clipped"
    gc.cache_put_sentence("en-US", "Hi there....", [{"n_error_start": 3, "n_error_length": 10}])
    got2 = gc.cache_get_sentence("en-US", "Hi there.")
    assert got2 is not None
    assert len(got2) == 1
    assert got2[0]["n_error_start"] == 3
    assert got2[0]["n_error_length"] == 6
    gc.cache_put_sentence("en-US", "Bad grammar...", [{"n_error_start": 0, "n_error_length": 3}])
    got3 = gc.cache_get_sentence("en-US", "Bad grammar.")
    assert got3 is not None
    assert len(got3) == 1
    assert got3[0]["n_error_length"] == 3

def test_sentence_cache_different_first_terminator_no_cross_hit() -> None:
    """Sentences with different first terminators must not share cache."""
    gc.cache_put_sentence("en-US", "Done.", [{"n_error_start": 0, "n_error_length": 4}])
    assert gc.cache_get_sentence("en-US", "Done?") is None
    assert gc.cache_get_sentence("en-US", "Done!") is None
    assert gc.cache_get_sentence("en-US", "Done.") is not None

def test_ignore_rules_snapshot() -> None:
    gc.ignore_rule_add("rule_a")
    assert "rule_a" in gc.ignored_rules_snapshot()
    gc.ignore_rules_clear()
    assert gc.ignored_rules_snapshot() == set()

def test_sentence_cache_incomplete_prefix_compaction() -> None:
    """Incomplete growing prefixes collapse to one LRU entry (newest wins)."""
    errors = [{"n_error_start": 0, "n_error_length": 3, "rule_identifier": "test"}]
    gc.cache_put_sentence("en-US", "The", errors)
    gc.cache_put_sentence("en-US", "The qu", errors)
    gc.cache_put_sentence("en-US", "The quick", errors)
    gc.cache_put_sentence("en-US", "The quick brown", errors)
    assert gc.cache_get_sentence("en-US", "The") is None
    assert gc.cache_get_sentence("en-US", "The qu") is None
    assert gc.cache_get_sentence("en-US", "The quick") is None
    got = gc.cache_get_sentence("en-US", "The quick brown")
    assert got is not None
    assert len(got) == 1
    gc.cache_put_sentence("en-US", "The quick brown fox.", errors)
    assert gc.cache_get_sentence("en-US", "The quick brown fox.") is not None
    assert gc.cache_get_sentence("en-US", "The quick brown") is not None

def test_sentence_cache_complete_not_evicted() -> None:
    """Complete sentences are protected from eviction by incomplete ones."""
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "test"}]
    gc.cache_put_sentence("en-US", "Hello world.", errors)
    gc.cache_put_sentence("en-US", "Hello world", errors)
    gc.cache_put_sentence("en-US", "Hello world is", errors)
    assert gc.cache_get_sentence("en-US", "Hello world.") is not None

def test_sentence_cache_locale_isolation() -> None:
    """Different locales do not interfere with prefix compaction."""
    errors = [{"n_error_start": 0, "n_error_length": 3, "rule_identifier": "test"}]
    gc.cache_put_sentence("en-US", "Hello", errors)
    gc.cache_put_sentence("fr-FR", "Bonjour", errors)
    gc.cache_put_sentence("en-US", "Hello there", errors)
    assert gc.cache_get_sentence("en-US", "Hello") is None
    assert gc.cache_get_sentence("fr-FR", "Bonjour") is not None

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

def test_whitespace_normalization_cache_key() -> None:
    """Test cache key normalization: whitespace + trailing punctuation after first terminator."""
    key1 = gc.make_sentence_key("en-US", "Hello.")
    key2 = gc.make_sentence_key("en-US", "Hello. ")
    key3 = gc.make_sentence_key("en-US", "Hello.\n")
    key4 = gc.make_sentence_key("en-US", "Hello...")
    key5 = gc.make_sentence_key("en-US", "Hello?...")
    key6 = gc.make_sentence_key("en-US", "Hello?")
    assert key1 == key2 == key3 == key4
    assert key5 == key6
    assert key1 != key6

def test_cache_hit_with_trailing_whitespace() -> None:
    """Putting 'Hello.' and getting 'Hello. ' should be a cache hit."""
    gc.cache_put_sentence("en-US", "Hello.", [{"n_error_start": 0, "n_error_length": 5}])
    result = gc.cache_get_sentence("en-US", "Hello. ")
    assert result is not None
    assert len(result) == 1
    assert result[0]["n_error_start"] == 0

def test_cache_persistence_fallback() -> None:
    """Test that cache_get_sentence falls back to persistence and populates memory cache."""
    from unittest.mock import MagicMock
    from plugin.writer.locale.grammar_persistence import DocumentPersistence
    
    ctx = MagicMock()
    mock_p = MagicMock(spec=DocumentPersistence)
    errors = [{"wrong": "test", "correct": "TEST", "n_error_start": 0, "n_error_length": 4}]
    mock_p.get.return_value = errors
    
    with patch("plugin.writer.locale.grammar_proofread_cache.get_persistence", return_value=mock_p):
        # 1. Get from persistence (memory miss)
        got = gc.cache_get_sentence("en-US", "Persistence test.", ctx=ctx, doc_id="doc1")
        assert got == errors
        mock_p.get.assert_called_once()
        
        # 2. Subsequent call should hit memory (mock_p.get NOT called again)
        got2 = gc.cache_get_sentence("en-US", "Persistence test.", ctx=ctx, doc_id="doc1")
        assert got2 == errors
        assert mock_p.get.call_count == 1


def test_document_mode_populates_memory_cache() -> None:
    """When a doc_id is set, it uses document persistence but still populates memory cache for speed."""
    from unittest.mock import MagicMock
    from plugin.writer.locale.grammar_persistence import DocumentPersistence

    ctx = MagicMock()
    mock_p = MagicMock(spec=DocumentPersistence)
    mock_p.get.return_value = [{"n_error_start": 0, "n_error_length": 1, "rule_identifier": "t"}]

    with patch("plugin.writer.locale.grammar_proofread_cache.get_persistence", return_value=mock_p) as mock_gp:
        got = gc.cache_get_sentence("en-US", "Hello.", ctx=ctx, doc_id="uid-1")
        assert got is not None
        assert len(gc._SENTENCE_CACHE) == 1
        mock_gp.assert_called_once_with(ctx, "uid-1")
