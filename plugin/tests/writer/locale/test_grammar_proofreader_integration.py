# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration tests for AI grammar proofreader (simulated typing patterns).

These tests verify the full doProofreading path with mocked UNO and LLM,
covering the Phase 3 scenarios from the real-time grammar checker plan:
- Rapid typing (deduplication collapses to single LLM request)
- Slow typing with cache warming (partial cache hits)
- Paragraph editing (only affected sentence is MISS)
- Concurrent/race conditions (worker returns during typing)
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# Mock UNO modules BEFORE importing any plugin modules
# This follows the same pattern as test_ai_grammar_proofreader_worker.py

def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


lang = _ensure_module("com.sun.star.lang")
ling = _ensure_module("com.sun.star.linguistic2")
text_mod = _ensure_module("com.sun.star.text")
setattr(lang, "Locale", type("Locale", (), {}))
setattr(lang, "XServiceDisplayName", type("XServiceDisplayName", (), {}))
setattr(lang, "XServiceInfo", type("XServiceInfo", (), {}))
setattr(lang, "XServiceName", type("XServiceName", (), {}))
setattr(lang, "XComponent", type("XComponent", (), {}))
setattr(ling, "XProofreader", type("XProofreader", (), {}))
setattr(ling, "XSupportedLocales", type("XSupportedLocales", (), {}))
setattr(text_mod, "TextMarkupType", type("TextMarkupType", (), {}))
unohelper_mod = _ensure_module("unohelper")
setattr(unohelper_mod, "Base", type("UnohelperBase", (object,), {}))
setattr(
    unohelper_mod,
    "ImplementationHelper",
    type(
        "ImplementationHelper",
        (),
        {"addImplementation": lambda self, *_args, **_kwargs: None},
    ),
)

# Mock uno module
uno_mod = _ensure_module("uno")
uno_mod.createUnoStruct = MagicMock()
uno_mod.getConstantByName = MagicMock(return_value=4)  # PROOFREADING
uno_mod.getComponentContext = MagicMock()


class FakeBI:
    """Fake BreakIterator for testing sentence splitting."""
    
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
    """Mock BreakIterator for all tests."""
    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(FakeBI(), "en-US")):
        yield


# Now import the modules under test
from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale import grammar_proofread_locale as gl
from plugin.writer.locale import grammar_proofread_text as gt
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem, GrammarWorkQueue
from plugin.writer.locale import ai_grammar_proofreader as proofreader


# --- Fixtures ---

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear cache before/after each test."""
    gc.cache_clear()
    gc.clear_sentence_cache()
    yield
    gc.cache_clear()
    gc.clear_sentence_cache()


@pytest.fixture
def mock_config():
    """Mock config for tests."""
    with (
        patch("plugin.framework.config.get_config_bool") as mock_get_bool,
        patch("plugin.framework.config.get_config_str") as mock_get_str,
        patch("plugin.framework.config.get_config_int") as mock_get_int,
        patch("plugin.framework.config.get_text_model") as mock_get_model,
        patch("plugin.framework.config.get_api_config") as mock_get_api,
        patch("plugin.framework.logging.init_logging"),
        patch.object(proofreader, "uno_mod", uno_mod),
    ):
        mock_get_bool.side_effect = lambda ctx, key: {
            "doc.grammar_proofreader_enabled": True,
            "doc.grammar_proofreader_pause_during_agent": False,
        }.get(key, False)
        mock_get_str.return_value = ""
        mock_get_int.return_value = 0
        mock_get_model.return_value = "test-model"
        mock_get_api.return_value = {}
        
        yield


@pytest.fixture
def mock_locale():
    """Create a mock UNO Locale struct."""
    loc = MagicMock()
    loc.Language = "en"
    loc.Country = "US"
    loc.Variant = ""
    return loc


@pytest.fixture
def mock_queue():
    """Mock the grammar queue to track enqueues."""
    mock_q = MagicMock(spec=GrammarWorkQueue)
    with patch("plugin.writer.locale.ai_grammar_proofreader.grammar_queue", mock_q):
        yield mock_q


# --- Helper to create proofreader instance ---

def _make_proofreader(ctx: Any = None) -> Any:
    """Create a WriterAgentAiGrammarProofreader instance."""
    if ctx is None:
        ctx = MagicMock()
    
    pr = proofreader.WriterAgentAiGrammarProofreader(ctx)
    return pr


# =============================================================================
# Phase 3: Simulated Typing Sequences (Integration)
# =============================================================================


class TestRapidTypingDeduplication:
    """Scenario 1: User types rapidly - verify deduplication collapses to one LLM request."""

    def test_rapid_typing_same_inflight_key(self, mock_config, mock_locale, mock_queue):
        """User types incrementally, all use same inflight_key for deduplication."""
        pr = _make_proofreader()
        doc_id = "test-doc"
        
        # Track enqueues
        enqueued_items = []
        def track_enqueue(item):
            enqueued_items.append(item)
        
        mock_queue.enqueue.side_effect = track_enqueue
        
        # Simulate typing - use texts that pass threshold
        # All should have same inflight_key (same doc, locale, start=0)
        texts = [
            "The quick brown fox",  # 16 non-space - above threshold
            "The quick brown fox j",  # 17 non-space - above threshold
        ]
        for text in texts:
            pr.doProofreading(
                aDocumentIdentifier=doc_id,
                aText=text,
                aLocale=mock_locale,
                nStartOfSentencePosition=0,
                nSuggestedBehindEndOfSentencePosition=len(text),
                aProperties=(),
            )
        
        # Verify all enqueued items have same inflight_key
        # (deduplication happens at queue level with tail replace)
        assert len(enqueued_items) >= 1, "Should have at least 1 enqueue"
        keys = {item.inflight_key for item in enqueued_items}
        assert len(keys) == 1, f"Expected 1 inflight_key for all items, got {keys}"
        
        # Verify sequences are increasing
        seqs = [item.enqueue_seq for item in enqueued_items]
        assert seqs == sorted(seqs), f"Sequences should be increasing, got {seqs}"

    def test_rapid_typing_inflight_key_consistency(self, mock_config, mock_locale):
        """Rapid typing on same sentence position uses same inflight_key."""
        from plugin.writer.locale.grammar_proofread_text import grammar_inflight_key
        
        doc_id = "test-doc"
        locale_key = "en-US"
        sent_start = 0
        
        # All incremental texts at position 0 should have same inflight_key
        texts = ["Th", "The", "The ", "The q", "The qu"]
        keys = set()
        for text in texts:
            key = grammar_inflight_key(doc_id, locale_key, sent_start)
            keys.add(key)
        
        assert len(keys) == 1, f"Expected 1 inflight_key for same position, got {keys}"


class TestSlowTypingCacheWarming:
    """Scenario 2: User types, pauses (LLM fills cache), continues typing."""

    def test_slow_typing_cache_hit_on_second_pass(self, mock_config, mock_locale, mock_queue):
        """First pass: MISS. Simulate cache fill. Second pass: HIT."""
        pr = _make_proofreader()
        doc_id = "test-doc"
        sentence = "The boy runs."
        
        # Pre-populate cache with a known error
        cached_errors = [
            {
                "n_error_start": 4,
                "n_error_length": 3,
                "rule_identifier": "test_rule",
                "suggestions": ("run",),
                "short_comment": "test",
                "full_comment": "test error",
            }
        ]
        gc.cache_put_sentence("en-US", sentence, cached_errors)
        
        # Now call doProofreading - should return cached errors
        res = pr.doProofreading(
            aDocumentIdentifier=doc_id,
            aText=sentence,
            aLocale=mock_locale,
            nStartOfSentencePosition=0,
            nSuggestedBehindEndOfSentencePosition=len(sentence),
            aProperties=(),
        )
        
        # Should have errors from cache
        assert res is not None
        errs = res.aErrors
        assert len(errs) == 1, f"Expected 1 cached error, got {len(errs)}"
        assert errs[0].nErrorStart == 4
        assert errs[0].nErrorLength == 3
        
        # Queue should not be called for cache hits
        mock_queue.enqueue.assert_not_called()

    def test_slow_typing_partial_hit_extend_sentence(self, mock_config, mock_locale, mock_queue):
        """First sentence cached, user adds second sentence -> partial hit."""
        pr = _make_proofreader()
        doc_id = "test-doc"
        
        # Cache first sentence
        first_sentence = "The boy runs."
        cached_errors = [{"n_error_start": 4, "n_error_length": 3, "rule_identifier": "r1"}]
        gc.cache_put_sentence("en-US", first_sentence, cached_errors)
        
        # Now type extended text (two sentences)
        full_text = "The boy runs. He jumps."
        
        enqueued_items = []
        mock_queue.enqueue.side_effect = lambda item: enqueued_items.append(item)
        
        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            # Mock split to return our two sentences
            mock_split.return_value = [
                (0, first_sentence),
                (len(first_sentence), " He jumps."),
            ]
            
            res = pr.doProofreading(
                aDocumentIdentifier=doc_id,
                aText=full_text,
                aLocale=mock_locale,
                nStartOfSentencePosition=0,
                nSuggestedBehindEndOfSentencePosition=len(full_text),
                aProperties=(),
            )
        
        # Should have 1 cached sentence returned, 1 uncached sentence enqueued
        assert len(enqueued_items) == 1, f"Expected 1 enqueue for uncached sentence, got {len(enqueued_items)}"
        
        # Verify the enqueued item is for the second sentence
        item = enqueued_items[0]
        assert item.n_start == len(first_sentence)
        
        # Verify first sentence errors are returned
        errs = res.aErrors
        assert len(errs) == 1, f"Expected 1 cached error, got {len(errs)}"


class TestParagraphEditing:
    """Scenario 3: User edits middle sentence of a paragraph."""

    def test_paragraph_edit_only_middle_sentence_miss(self, mock_config, mock_locale, mock_queue):
        """3 cached sentences, edit middle -> only middle is cache MISS."""
        pr = _make_proofreader()
        doc_id = "test-doc"
        
        # Cache all three sentences
        sentences = ["First sentence.", "Second sentence.", "Third sentence."]
        for sent in sentences:
            gc.cache_put_sentence("en-US", sent, [{"n_error_start": 0, "n_error_length": 1, "rule_identifier": f"rule_{sent}"}])
        
        # Edit middle sentence
        edited_text = sentences[0] + " " + "SecondX sentence." + " " + sentences[2]
        
        enqueued_items = []
        mock_queue.enqueue.side_effect = lambda item: enqueued_items.append(item)
        
        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            # Mock split to return the three sentences (with edited middle)
            mock_split.return_value = [
                (0, sentences[0]),
                (len(sentences[0]) + 1, "SecondX sentence."),  # edited
                (len(sentences[0]) + 1 + len("SecondX sentence.") + 1, sentences[2]),
            ]
            
            res = pr.doProofreading(
                aDocumentIdentifier=doc_id,
                aText=edited_text,
                aLocale=mock_locale,
                nStartOfSentencePosition=0,
                nSuggestedBehindEndOfSentencePosition=len(edited_text),
                aProperties=(),
            )
        
        # Only the middle (edited) sentence should be enqueued
        assert len(enqueued_items) == 1, f"Expected 1 enqueue for edited sentence, got {len(enqueued_items)}"
        
        # Verify first and third sentences return cached errors
        errs = res.aErrors
        # First and third should have cached errors, middle doesn't (not cached yet)
        assert len(errs) == 2, f"Expected 2 cached errors (first + third), got {len(errs)}"


class TestIncrementalTyping:
    """Test LibreOffice's incremental nStart != 0 path."""

    def test_incremental_nonzero_start_only_overlapping_sentence(self, mock_config, mock_locale, mock_queue):
        """Nonzero nStart -> only sentence overlapping the range is checked."""
        pr = _make_proofreader()
        doc_id = "test-doc"
        full_text = "First. Second. Third."
        
        enqueued_items = []
        mock_queue.enqueue.side_effect = lambda item: enqueued_items.append(item)
        
        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            # Mock split to return three sentences
            mock_split.return_value = [
                (0, "First."),
                (7, "Second."),
                (15, "Third."),
            ]
            
            # Call with nStart pointing to second sentence
            res = pr.doProofreading(
                aDocumentIdentifier=doc_id,
                aText=full_text,
                aLocale=mock_locale,
                nStartOfSentencePosition=7,
                nSuggestedBehindEndOfSentencePosition=14,
                aProperties=(),
            )
        
        # Should only enqueue the second sentence (overlapping the range)
        assert len(enqueued_items) == 1
        item = enqueued_items[0]
        assert item.n_start == 7

    def test_incremental_start_zero_full_paragraph(self, mock_config, mock_locale, mock_queue):
        """nStart=0 -> all sentences in paragraph are candidates."""
        pr = _make_proofreader()
        doc_id = "test-doc"
        full_text = "First. Second. Third."
        
        enqueued_items = []
        mock_queue.enqueue.side_effect = lambda item: enqueued_items.append(item)
        
        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            mock_split.return_value = [
                (0, "First."),
                (7, "Second."),
                (15, "Third."),
            ]
            
            res = pr.doProofreading(
                aDocumentIdentifier=doc_id,
                aText=full_text,
                aLocale=mock_locale,
                nStartOfSentencePosition=0,
                nSuggestedBehindEndOfSentencePosition=len(full_text),
                aProperties=(),
            )
        
        # All three sentences should be enqueued (none cached)
        assert len(enqueued_items) == 3


class TestConcurrencyAndRaces:
    """Phase 4: Concurrency edge cases."""

    def test_worker_returns_while_typing_next_character(self, mock_config, mock_locale, mock_queue):
        """Simulate: worker returns LLM result just as user types next character."""
        from plugin.writer.locale.grammar_work_queue import inflight_superseded
        
        doc_id = "test-doc"
        
        # Simulate: first call enqueues seq=1 - use text long enough to pass threshold
        enqueued_items = []
        mock_queue.enqueue.side_effect = lambda item: enqueued_items.append(item)
        
        pr = _make_proofreader()
        pr.doProofreading(
            aDocumentIdentifier=doc_id,
            aText="The quick brown fox jumps",  # 21 non-space chars - above threshold
            aLocale=mock_locale,
            nStartOfSentencePosition=0,
            nSuggestedBehindEndOfSentencePosition=22,
            aProperties=(),
        )
        
        # Verify enqueue was called
        assert len(enqueued_items) >= 1, f"Expected at least 1 enqueue, got {len(enqueued_items)}"
        first_item = enqueued_items[0]
        first_seq = first_item.enqueue_seq
        first_key = first_item.inflight_key
        
        # Simulate: worker is processing seq=1, but user types another char
        # New enqueue with seq=2 for same key
        latest_seq = {first_key: first_seq + 1}
        
        # The old item should be stale
        assert inflight_superseded(latest_seq, first_key, first_seq) is True
        assert inflight_superseded(latest_seq, first_key, first_seq + 1) is False

    def test_short_incomplete_sentence_skips_enqueue(self, mock_config, mock_locale, mock_queue):
        """Sentences below GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS are not enqueued."""
        pr = _make_proofreader()
        doc_id = "test-doc"
        
        short_texts = [
            "a",
            "ab", 
            "abc",
            "too short",  # 9 non-space chars, below 15 threshold
        ]
        
        enqueued_items = []
        mock_queue.enqueue.side_effect = lambda item: enqueued_items.append(item)
        
        for text in short_texts:
            pr.doProofreading(
                aDocumentIdentifier=doc_id,
                aText=text,
                aLocale=mock_locale,
                nStartOfSentencePosition=0,
                nSuggestedBehindEndOfSentencePosition=len(text),
                aProperties=(),
            )
        
        # Short texts should not be enqueued
        # (they don't meet the threshold)
        for text in short_texts:
            assert not any(
                item.full_text == text 
                for item in enqueued_items
            ), f"Short text '{text}' should not be enqueued"


class TestEnabledDisabled:
    """Test behavior when grammar checker is disabled."""

    def test_disabled_returns_empty_result(self, mock_locale):
        """When disabled, doProofreading returns empty result without errors."""
        with (
            patch("plugin.framework.config.get_config_bool") as mock_get_bool,
            patch.object(proofreader, "uno_mod", uno_mod),
            patch("plugin.framework.logging.init_logging"),
        ):
            mock_get_bool.return_value = False  # Disabled
            
            pr = _make_proofreader()
            res = pr.doProofreading(
                aDocumentIdentifier="test-doc",
                aText="Some text with errors.",
                aLocale=mock_locale,
                nStartOfSentencePosition=0,
                nSuggestedBehindEndOfSentencePosition=22,
                aProperties=(),
            )
            
            assert res is not None
            # Should return empty errors
            assert len(res.aErrors) == 0

    def test_disabled_skips_queue_and_llm(self, mock_locale, mock_queue):
        """When disabled, no queue operations or LLM calls."""
        with (
            patch("plugin.framework.config.get_config_bool") as mock_get_bool,
            patch.object(proofreader, "uno_mod", uno_mod),
            patch("plugin.framework.logging.init_logging"),
        ):
            mock_get_bool.return_value = False  # Disabled
            
            pr = _make_proofreader()
            pr.doProofreading(
                aDocumentIdentifier="test-doc",
                aText="Some text.",
                aLocale=mock_locale,
                nStartOfSentencePosition=0,
                nSuggestedBehindEndOfSentencePosition=11,
                aProperties=(),
            )
            
            # Queue should not be called
            mock_queue.enqueue.assert_not_called()
