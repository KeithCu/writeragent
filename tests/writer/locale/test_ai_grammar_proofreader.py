# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the main AI grammar proofreader entry point, worker, and integration scenarios."""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# --- UNO Mocks for non-native tests ---

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

from plugin.writer.locale import ai_grammar_proofreader as proofreader
from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale import grammar_proofread_text as gt
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem, GrammarWorkQueue
# =============================================================================
# Worker Tests (Mocked)
# =============================================================================

def test_worker_skips_when_agent_active_and_pause_enabled() -> None:
    def _get_config_bool(_ctx, key: str) -> bool:
        if key == "doc.grammar_proofreader_enabled":
            return True
        if key == "doc.grammar_proofreader_pause_during_agent":
            return True
        raise AssertionError(f"unexpected key: {key}")

    with (
        patch("plugin.framework.config.get_config_int", return_value=0),
        patch("plugin.framework.config.get_config_bool", side_effect=_get_config_bool),
        patch("plugin.framework.queue_executor.is_agent_active", return_value=True),
        patch("plugin.writer.locale.ai_grammar_proofreader.time.sleep"),
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
    ):
        proofreader._run_llm_and_cache(
            ctx=None,
            text="test",
            enqueue_seq=3,
            inflight_key="doc|en",
            grammar_bcp47="en-US",
        )
    client_cls.assert_not_called()

def test_apply_proofreading_end_positions_skips_space_after_sentence() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import _apply_proofreading_end_positions
    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0
    text = "Hi. Bye."
    r = Res()
    _apply_proofreading_end_positions(r, text, 3)
    assert r.nStartOfNextSentencePosition == 4
    assert r.nBehindEndOfSentencePosition == 4

def test_apply_proofreading_end_positions_skips_tab_after_sentence() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import _apply_proofreading_end_positions
    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0
    text = "Hi.\tBye."
    r = Res()
    _apply_proofreading_end_positions(r, text, 3)
    assert r.nStartOfNextSentencePosition == 4
    assert r.nBehindEndOfSentencePosition == 4

def test_sentence_terminators_cover_multilingual_cases() -> None:
    assert proofreader._looks_complete_sentence("Hello world.")
    assert proofreader._looks_complete_sentence("مرحبا بالعالم؟")
    assert proofreader._looks_complete_sentence("これは文です。")
    assert proofreader._looks_complete_sentence("यह एक वाक्य है।")
    assert not proofreader._looks_complete_sentence("incomplete clause")

def test_partial_threshold_counts_nonspace_chars() -> None:
    assert proofreader._count_nonspace_chars("a b c") == 3
    assert proofreader._count_nonspace_chars("too short") < proofreader.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS
    assert proofreader._count_nonspace_chars("this is long enough") >= proofreader.GRAMMAR_PARTIAL_MIN_NONSPACE_CHARS

def test_run_llm_skips_split_when_proofread_sentence_text_set() -> None:
    def _get_config_bool(_ctx: object, key: str) -> bool:
        if key == "doc.grammar_proofreader_enabled": return True
        if key == "doc.grammar_proofreader_pause_during_agent": return False
        raise AssertionError(f"unexpected key: {key}")
    def _split_must_not_run(*_a, **_k): raise AssertionError("split_into_sentences must not run")
    with (
        patch("plugin.framework.config.get_config_bool", side_effect=_get_config_bool),
        patch("plugin.framework.config.get_config_str", return_value=""),
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="m"),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.framework.queue_executor.is_agent_active", return_value=False),
        patch("plugin.framework.queue_executor.llm_request_lane") as lane_ctx,
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
        patch("plugin.writer.locale.ai_grammar_proofreader.time.sleep"),
        patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences", side_effect=_split_must_not_run),
        patch("plugin.writer.locale.grammar_proofread_json.parse_grammar_json", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence"),
    ):
        lane_ctx.return_value.__enter__ = MagicMock()
        lane_ctx.return_value.__exit__ = MagicMock()
        client_cls.return_value.chat_completion_sync.return_value = '{"errors":[]}'
        proofreader._run_llm_and_cache(None, "Hello.", 1, "d|en", "en-US")

def test_partial_sentence_adds_prompt_note() -> None:
    def _get_config_bool(_ctx, key: str) -> bool:
        if key == "doc.grammar_proofreader_enabled": return True
        if key == "doc.grammar_proofreader_pause_during_agent": return False
        raise AssertionError(f"unexpected key: {key}")
    with (
        patch("plugin.framework.config.get_config_bool", side_effect=_get_config_bool),
        patch("plugin.framework.config.get_config_str", return_value=""),
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="m"),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.framework.queue_executor.is_agent_active", return_value=False),
        patch("plugin.framework.queue_executor.llm_request_lane") as lane_ctx,
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
        patch("plugin.writer.locale.ai_grammar_proofreader.time.sleep"),
        patch("plugin.writer.locale.grammar_proofread_json.parse_grammar_json", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence"),
    ):
        lane_ctx.return_value.__enter__ = MagicMock()
        lane_ctx.return_value.__exit__ = MagicMock()
        client = client_cls.return_value
        client.chat_completion_sync.return_value = '{"errors":[]}'
        proofreader._run_llm_and_cache(None, "This is long enough...", 0, "doc|en", "en-US", partial_sentence=True)
    args, _ = client.chat_completion_sync.call_args
    assert "partial sentence" in args[0][0]["content"]

# =============================================================================
# Integration Tests (Mocked typing patterns)
# =============================================================================

@pytest.fixture
def mock_config_fixture():
    with (
        patch("plugin.framework.config.get_config_bool") as mock_get_bool,
        patch("plugin.framework.config.get_config_str") as mock_get_str,
        patch("plugin.framework.config.get_config_int") as mock_get_int,
        patch("plugin.framework.client.model_fetcher.get_text_model") as mock_get_model,
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
def mock_locale_fixture():
    loc = MagicMock()
    loc.Language = "en"
    loc.Country = "US"
    loc.Variant = ""
    return loc

@pytest.fixture
def mock_queue_fixture():
    mock_q = MagicMock(spec=GrammarWorkQueue)
    with patch("plugin.writer.locale.ai_grammar_proofreader.grammar_queue", mock_q):
        yield mock_q

@pytest.fixture(autouse=True)
def _reset_grammar_caches():
    """Clear both the global LRU and the per-doc DocumentPersistence map.

    Under ``USE_SQLITE_CACHE=False`` ``DocumentPersistence`` instances live in a
    module-level dict keyed by doc id and would otherwise leak warm state across
    tests (any test that reuses ``doc_id="test-doc"`` would see stale entries).
    """
    from plugin.writer.locale import grammar_persistence as gp

    gc.cache_clear()
    gp._doc_persistence_instances.clear()
    yield
    gc.cache_clear()
    gp._doc_persistence_instances.clear()

def _make_proofreader(ctx: Any = None) -> Any:
    if ctx is None: ctx = MagicMock()
    return proofreader.WriterAgentAiGrammarProofreader(ctx)

class TestTypingIntegration:
    def test_rapid_typing_deduplication(self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture):
        pr = _make_proofreader()
        enqueued_items = []
        mock_queue_fixture.enqueue.side_effect = lambda item: enqueued_items.append(item)
        # For incomplete sentences, the key is stable even for short typing bursts
        texts = ["The quick brown fox", "The quick brown fox j"]
        for text in texts:
            pr.doProofreading("test-doc", text, mock_locale_fixture, 0, len(text), ())
        assert len(enqueued_items) >= 2
        keys = {item.inflight_key for item in enqueued_items}
        # Both share the 'INCOMPLETE_WRITER_AGENT_INTERNAL_STRING' key
        assert len(keys) == 1
        assert "INCOMPLETE_WRITER_AGENT_INTERNAL_STRING" in list(keys)[0]

    def test_slow_typing_cache_hit(self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture):
        # Pass ctx + doc_id so write and read target the same cache layer under both
        # USE_SQLITE_CACHE=True (global SQLite/JSON singleton) and USE_SQLITE_CACHE=False
        # (per-doc DocumentPersistence keyed by doc_id). doProofreading reads with
        # ctx=self.ctx, doc_id="test-doc"; the test must match that.
        pr = _make_proofreader()
        sentence = "The boy runs."
        gc.cache_put_sentence("en-US", sentence, [{"n_error_start": 4, "n_error_length": 3, "rule_identifier": "r1"}], ctx=pr.ctx, doc_id="test-doc")
        res = pr.doProofreading("test-doc", sentence, mock_locale_fixture, 0, len(sentence), ())
        assert len(res.aErrors) == 1
        mock_queue_fixture.enqueue.assert_not_called()

    def test_paragraph_edit_middle_miss(self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture):
        pr = _make_proofreader()
        sentences = ["First sentence.", "Second sentence.", "Third sentence."]
        for sent in sentences:
            gc.cache_put_sentence("en-US", sent, [{"n_error_start": 0, "n_error_length": 1, "rule_identifier": "r"}], ctx=pr.ctx, doc_id="test-doc")
        edited_text = sentences[0] + " SecondX sentence. " + sentences[2]
        enqueued_items = []
        mock_queue_fixture.enqueue.side_effect = lambda item: enqueued_items.append(item)
        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            mock_split.return_value = [(0, sentences[0]), (len(sentences[0]) + 1, "SecondX sentence."), (len(sentences[0]) + 1 + len("SecondX sentence.") + 1, sentences[2])]
            res = pr.doProofreading("test-doc", edited_text, mock_locale_fixture, 0, len(edited_text), ())
        assert len(enqueued_items) == 1
        assert len(res.aErrors) == 2
