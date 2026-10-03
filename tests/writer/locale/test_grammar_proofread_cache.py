# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar proofread cache (sentence errors, LRU, prefix compaction)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale.grammar_proofread_cache import _normalize_for_sentence_cache

@pytest.fixture(autouse=True)
def clear_cache():
    gc.cache_clear()
    gc.ignore_rules_clear()
    yield
    gc.cache_clear()
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
    ident = gc.resolve_checker_identity().replace("|", "/")
    assert gc.sentence_cache_key_prefix("en-US") == f"sent|en-US|{ident}|"
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
        assert len(gc.grammar_registry.sentence_cache) == 1
        mock_gp.assert_called_once_with(ctx, "uid-1")


def test_l1_miss_across_checker_identity() -> None:
    """Harper and LLM identities do not share L1 rows for the same sentence."""
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "harper||SpellCheck"}]
    gc.cache_put_sentence("en-US", "Same sentence here.", errors, checker_identity="harper")
    assert gc.cache_get_sentence("en-US", "Same sentence here.", checker_identity="llm:gpt-test") is None
    got = gc.cache_get_sentence("en-US", "Same sentence here.", checker_identity="harper")
    assert got is not None
    assert got[0]["rule_identifier"] == "harper||SpellCheck"


def test_cross_document_l1_cache_hit() -> None:
    """Doc B hits Tier-1 global LRU after Doc A checked the same sentence (copy-paste scenario)."""
    from unittest.mock import MagicMock

    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "wa_test"}]
    mock_p_a = MagicMock()
    mock_p_b = MagicMock()
    mock_p_b.get.return_value = None
    ctx = MagicMock()

    def _get_persistence(_ctx: object, doc_id: str, **kwargs: object) -> MagicMock | None:
        if doc_id == "doc-a":
            return mock_p_a
        if doc_id == "doc-b":
            return mock_p_b
        return None

    with patch("plugin.writer.locale.grammar_proofread_cache.get_persistence", side_effect=_get_persistence):
        gc.cache_put_sentence("en-US", "Same sentence here.", errors, ctx=ctx, doc_id="doc-a")
        mock_p_a.put.assert_called_once()
        got = gc.cache_get_sentence("en-US", "Same sentence here.", ctx=ctx, doc_id="doc-b")
        assert got is not None
        assert len(got) == 1
        mock_p_b.get.assert_not_called()


def test_l1_hit_records_session_accessed_for_doc() -> None:
    """When a sentence hits L1 memory with a doc_id, it marks the sentence as session accessed for that doc."""
    from unittest.mock import MagicMock
    from plugin.writer.locale.grammar_persistence import DocumentPersistence

    ctx = MagicMock()
    model = MagicMock()
    with patch("plugin.doc.udprops.get_document_property", return_value=None):
        dp = DocumentPersistence(ctx, "doc-track", model=model)

    with patch("plugin.writer.locale.grammar_proofread_cache.get_persistence", return_value=dp):
        gc.cache_put_sentence("en-US", "Track me.", [])
        assert len(dp._session_accessed) == 0

        got = gc.cache_get_sentence("en-US", "Track me.", ctx=ctx, doc_id="doc-track")
        assert got == []
        fp = gc.sentence_identity_fp("Track me.")
        assert ("en-US", fp) in dp._session_accessed


def test_cache_clear_does_not_register_empty_persistence() -> None:
    """clear_all must not be followed by get_persistence, which would register a model-less instance."""
    from plugin.writer.locale.grammar_persistence import grammar_registry

    ctx = MagicMock()
    gc.cache_clear(ctx)
    assert "phantom-doc" not in grammar_registry.doc_persistence_instances


def test_dump_grammar_cache_for_recheck_clears_l1_and_active_l2_only() -> None:
    """Recheck dumps all of L1 but only the active document's L2 rows."""
    from plugin.writer.locale import grammar_persistence as gp

    ctx = MagicMock()
    model_a = object()
    model_b = object()
    gp.grammar_registry.clear_all(ctx)
    try:
        with patch("plugin.doc.udprops.get_document_property", return_value=None):
            pa = gp.DocumentPersistence(ctx, "doc-a", model=model_a)
            pb = gp.DocumentPersistence(ctx, "doc-b", model=model_b)
        gp.grammar_registry.doc_persistence_instances["doc-a"] = pa
        gp.grammar_registry.doc_persistence_instances["doc-b"] = pb
        pa.put("fp_a", "en-US", [{"n_error_start": 0, "n_error_length": 1}])
        pb.put("fp_b", "en-US", [{"n_error_start": 0, "n_error_length": 1}])
        gc.cache_put_sentence("en-US", "Hello there everyone.", [{"n_error_start": 0, "n_error_length": 1}])
        assert gc.cache_get_sentence("en-US", "Hello there everyone.") is not None

        def fake_uid(model: object) -> str:
            if model is model_a:
                return "uid-a"
            if model is model_b:
                return "uid-b"
            return ""

        with (
            patch("plugin.framework.uno_context.get_active_document", return_value=model_a),
            patch("plugin.framework.uno_context.get_runtime_uid", side_effect=fake_uid),
            patch.object(pa, "_persist_to_udprops"),
        ):
            gc.dump_grammar_cache_for_recheck(ctx)

        assert gc.cache_get_sentence("en-US", "Hello there everyone.") is None
        assert "fp_a" not in pa._entries
        assert "fp_b" in pb._entries
    finally:
        gp.clear_all_document_persistence(ctx)


def test_recheck_active_document_grammar_broadcasts_from_live_proofreader() -> None:
    from plugin.writer.locale.grammar_persistence import grammar_registry

    class _Live:
        def __init__(self) -> None:
            self.broadcast_proofread_again = MagicMock()

    pr = _Live()
    grammar_registry.register_live_proofreader(pr)
    try:
        with patch("plugin.writer.locale.grammar_proofread_cache.dump_grammar_cache_for_recheck") as dump:
            gc.recheck_active_document_grammar(MagicMock())
        dump.assert_called_once()
        pr.broadcast_proofread_again.assert_called_once()
    finally:
        grammar_registry.live_proofreaders.discard(pr)


def test_l2_same_sentence_is_locale_specific() -> None:
    """L1 miss must not serve another CharLocale's L2 errors or good row."""
    from plugin.writer.locale.grammar_persistence import DocumentPersistence

    ctx = MagicMock()
    model = MagicMock()
    with patch("plugin.doc.udprops.get_document_property", return_value=None):
        dp = DocumentPersistence(ctx, "doc-locales", model=model)
    en_errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "en-rule"}]
    fr_errors = [{"n_error_start": 1, "n_error_length": 2, "rule_identifier": "fr-rule"}]
    with patch("plugin.writer.locale.grammar_proofread_cache.get_persistence", return_value=dp):
        gc.cache_put_sentence("en-US", "Bonjour.", en_errors, ctx=ctx, doc_id="doc-locales")
        gc.cache_put_sentence("fr-FR", "Bonjour.", fr_errors, ctx=ctx, doc_id="doc-locales")
        gc.cache_put_sentence("en-US", "Clean sentence.", [], ctx=ctx, doc_id="doc-locales")
        gc.dump_l1_sentence_cache()
        got_en = gc.cache_get_sentence("en-US", "Bonjour.", ctx=ctx, doc_id="doc-locales")
        got_fr = gc.cache_get_sentence("fr-FR", "Bonjour.", ctx=ctx, doc_id="doc-locales")
        got_clean_fr = gc.cache_get_sentence("fr-FR", "Clean sentence.", ctx=ctx, doc_id="doc-locales")
        got_clean_en = gc.cache_get_sentence("en-US", "Clean sentence.", ctx=ctx, doc_id="doc-locales")
    assert got_en is not None and got_en[0]["rule_identifier"] == "en-rule"
    assert got_fr is not None and got_fr[0]["rule_identifier"] == "fr-rule"
    assert got_clean_fr is None
    assert got_clean_en == []

