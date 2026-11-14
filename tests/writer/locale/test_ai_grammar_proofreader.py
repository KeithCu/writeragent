# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the main AI grammar proofreader entry point, worker, and integration scenarios."""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from plugin.writer.locale.grammar_settings import GrammarProfile

uno_mod = sys.modules["uno"]


def _safe_get_constant(name: str) -> int:
    return 4 if "PROOFREADING" in name else 1


@pytest.fixture(autouse=True)
def _stub_uno_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uno_mod, "createUnoStruct", MagicMock(side_effect=lambda *_a, **_k: types.SimpleNamespace()))
    monkeypatch.setattr(uno_mod, "getConstantByName", MagicMock(side_effect=_safe_get_constant))
    monkeypatch.setattr(uno_mod, "getComponentContext", MagicMock())


def _proofreader_get_config_side_effect(
    key: str,
    *,
    enabled: str = "llm",
    pause: bool = False,
) -> Any:
    if key == "doc.grammar_proofreader_enabled":
        return enabled
    if key == "doc.grammar_proofreader_pause_during_agent":
        return pause
    if key in ("additional_instructions", "doc.grammar_proofreader_custom_prompt"):
        return ""
    return 0


class FakeBI:
    def getWordBoundary(self, text, pos, locale, wordType, bDirection):
        import re

        m_u16 = Utf16OffsetMap(text)
        py_pos = m_u16.utf16_to_py(pos)
        res = MagicMock()
        m = re.compile(r"\w+|\W+").match(text, py_pos)
        if m:
            res.startPos = m_u16.py_to_utf16(m.start())
            res.endPos = m_u16.py_to_utf16(m.end())
        else:
            res.startPos = pos
            res.endPos = m_u16.py_to_utf16(len(text))
        return res

    def endOfSentence(self, text, pos, locale):
        import re

        m_u16 = Utf16OffsetMap(text)
        py_pos = m_u16.utf16_to_py(pos)
        m = re.search(r'[.!?]', text[py_pos:])
        if m:
            return m_u16.py_to_utf16(py_pos + m.end())
        return m_u16.py_to_utf16(len(text))

@pytest.fixture(autouse=True)
def mock_bi():
    with patch("plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale", return_value=(FakeBI(), "en-US")):
        yield

@pytest.fixture(autouse=True)
def _ensure_grammar_debug_logging():
    import logging
    old_level = proofreader.log.level
    proofreader.log.setLevel(logging.DEBUG)
    yield
    proofreader.log.setLevel(old_level)

@pytest.fixture(autouse=True)
def _reset_grammar_worker_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grammar_worker, "_grammar_worker_tls", threading.local())

from plugin.writer.locale import ai_grammar_proofreader as proofreader
from plugin.writer.locale import grammar_proofread_cache as gc
from plugin.writer.locale.grammar_proofread_text import Utf16OffsetMap

from plugin.writer.locale.grammar_proofread_locale import (
    looks_complete_sentence,
)
from plugin.writer.locale.grammar_proofread_text import NormalizedProofError
from plugin.writer.locale import grammar_worker
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem, GrammarWorkQueue
from plugin.writer.locale.grammar_worker import GrammarWorkerContext, run_llm_and_cache_batch


def _run_llm_one(
    ctx: Any,
    text: str,
    enqueue_seq: int,
    inflight_key: str,
    grammar_bcp47: str,
    partial_sentence: bool = False,
    **kwargs: Any,
) -> None:
    grammar_worker._grammar_worker_tls = threading.local()
    item = GrammarWorkItem(
        ctx=ctx,
        text=text,
        grammar_bcp47=grammar_bcp47,
        partial_sentence=partial_sentence,
        doc_id="",
        inflight_key=inflight_key,
        enqueue_seq=enqueue_seq,
    )
    run_llm_and_cache_batch(
        [item],
        grammar_queue_instance=kwargs.get("grammar_queue_instance") or GrammarWorkQueue(),
    )
# =============================================================================
# Worker Tests (Mocked)
# =============================================================================

def test_worker_skips_when_agent_active_and_pause_enabled() -> None:
    with (
        patch(
            "plugin.framework.config.get_config",
            side_effect=lambda key: _proofreader_get_config_side_effect(key, pause=True),
        ),
        patch("plugin.framework.send_cancellation.is_agent_active", return_value=True),
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
    ):
        _run_llm_one(
            ctx=None,
            text="test",
            enqueue_seq=3,
            inflight_key="doc|en",
            grammar_bcp47="en-US",
        )
    client_cls.assert_not_called()


def test_worker_does_not_pause_local_provider_when_agent_active() -> None:
    """LanguageTool / Vale keep checking while chat runs; pause is LLM-only (Harper never uses the queue)."""

    with (
        patch(
            "plugin.framework.config.get_config",
            side_effect=lambda key: _proofreader_get_config_side_effect(key, enabled="languagetool", pause=True),
        ),
        patch("plugin.framework.send_cancellation.is_agent_active", return_value=True),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check") as run_check,
        patch(
            "plugin.writer.locale.grammar_proofread_cache.cache_get_sentence",
            return_value=None,
        ),
    ):
        _run_llm_one(
            ctx=None,
            text="test",
            enqueue_seq=3,
            inflight_key="doc|en",
            grammar_bcp47="en-US",
        )
    run_check.assert_called_once()


def test_classify_errors_against_window_counts_before_in_after() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import classify_errors_against_window

    errors = [
        {"n_error_start": 2, "n_error_length": 3},
        {"n_error_start": 20, "n_error_length": 4},
        {"n_error_start": 40, "n_error_length": 2},
        {"n_error_start": 18, "n_error_length": 5},
    ]
    cls = classify_errors_against_window(errors, 20, 30)
    assert cls["before_window"] == 1
    assert cls["in_window"] == 1
    assert cls["after_window"] == 1
    assert cls["straddle"] == 1


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("Hi. Bye.", id="test_apply_proofreading_end_positions_skips_space_after_sentence"),
        pytest.param("Hi.\tBye.", id="test_apply_proofreading_end_positions_skips_tab_after_sentence"),
    ],
)
def test_apply_proofreading_end_positions_skips_space_after_sentence(value) -> None:
    from plugin.writer.locale.ai_grammar_proofreader import _apply_proofreading_end_positions
    class Res:
        nStartOfNextSentencePosition = 0
        nBehindEndOfSentencePosition = 0
    text = value
    r = Res()
    _apply_proofreading_end_positions(r, text, 3, Utf16OffsetMap(text))
    assert r.nStartOfNextSentencePosition == 4
    assert r.nBehindEndOfSentencePosition == 4


def test_sentence_terminators_cover_multilingual_cases() -> None:
    assert looks_complete_sentence("Hello world.")
    assert looks_complete_sentence("مرحبا بالعالم？")
    assert looks_complete_sentence("これは文です。")
    assert looks_complete_sentence("यह एक वाक्य है।")
    assert not looks_complete_sentence("incomplete clause")

def test_partial_sentence_adds_prompt_note() -> None:
    with (
        patch(
            "plugin.framework.config.get_config",
            side_effect=_proofreader_get_config_side_effect,
        ),
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="m"),
        patch("plugin.framework.config.get_api_config", return_value={}),
        patch("plugin.framework.send_cancellation.is_agent_active", return_value=False),
        patch("plugin.framework.llm_lanes.grammar_llm_request_gate") as lane_ctx,
        patch("plugin.framework.client.llm_client.LlmClient") as client_cls,
        patch("plugin.writer.locale.grammar_proofread_json.parse_grammar_json", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.normalize_errors_for_text", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence"),
    ):
        lane_ctx.return_value.__enter__ = MagicMock()
        lane_ctx.return_value.__exit__ = MagicMock()
        client = client_cls.return_value
        client.chat_completion_sync.return_value = '{"errors":[]}'
        _run_llm_one(None, "This is long enough...", 0, "doc|en", "en-US", partial_sentence=False)
        prompt_full = client.chat_completion_sync.call_args[0][0][0]["content"]
        _run_llm_one(None, "This is long enough...", 0, "doc|en", "en-US", partial_sentence=True)
        prompt_partial = client.chat_completion_sync.call_args[0][0][0]["content"]
    assert prompt_partial != prompt_full

# =============================================================================
# Integration Tests (Mocked typing patterns)
# =============================================================================

@pytest.fixture
def mock_config_fixture():
    with (
        patch("plugin.framework.config.get_config") as mock_get_config,
        patch("plugin.framework.client.model_fetcher.get_text_model") as mock_get_model,
        patch("plugin.framework.config.get_api_config") as mock_get_api,
        patch("plugin.framework.logging.init_logging"),
        patch.object(proofreader, "uno_mod", uno_mod),
    ):
        mock_get_config.side_effect = _proofreader_get_config_side_effect
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

    gc.grammar_registry.clear_all()
    gp.grammar_registry.doc_persistence_instances.clear()
    yield
    gc.grammar_registry.clear_all()
    gp.grammar_registry.doc_persistence_instances.clear()

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
        # Pass doc_id so write and read target the same per-document cache; doProofreading reads with doc_id="test-doc".
        pr = _make_proofreader()
        sentence = "The boy runs."
        gc.cache_put_sentence("en-US", sentence, [{"n_error_start": 4, "n_error_length": 3, "rule_identifier": "r1"}], doc_id="test-doc")
        res = pr.doProofreading("test-doc", sentence, mock_locale_fixture, 0, len(sentence), ())
        assert len(res.aErrors) == 1
        mock_queue_fixture.enqueue.assert_not_called()

    def test_paragraph_edit_middle_miss(self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture):
        pr = _make_proofreader()
        sentences = ["First sentence.", "Second sentence.", "Third sentence."]
        for sent in sentences:
            gc.cache_put_sentence("en-US", sent, [{"n_error_start": 0, "n_error_length": 1, "rule_identifier": "r"}], doc_id="test-doc")
        edited_text = sentences[0] + " SecondX sentence. " + sentences[2]
        enqueued_items = []
        mock_queue_fixture.enqueue.side_effect = lambda item: enqueued_items.append(item)
        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            mock_split.return_value = [(0, sentences[0]), (len(sentences[0]) + 1, "SecondX sentence."), (len(sentences[0]) + 1 + len("SecondX sentence.") + 1, sentences[2])]
            res = pr.doProofreading("test-doc", edited_text, mock_locale_fixture, 0, len(edited_text), ())
        assert len(enqueued_items) == 1
        assert len(res.aErrors) == 2

    def test_partial_cache_hit_returns_cached_errors_and_enqueues_uncached(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """Characterization: partial miss returns cached squiggles immediately and enqueues only misses."""
        pr = _make_proofreader()
        s1, s2, s3 = "Alpha sentence.", "Beta sentence.", "Gamma sentence."
        paragraph = f"{s1} {s2} {s3}"
        gc.cache_put_sentence("en-US", s1, [{"n_error_start": 0, "n_error_length": 1, "rule_identifier": "r1"}], doc_id="test-doc")
        gc.cache_put_sentence("en-US", s3, [{"n_error_start": 0, "n_error_length": 1, "rule_identifier": "r3"}], doc_id="test-doc")

        enqueued_items: list[Any] = []
        mock_queue_fixture.enqueue.side_effect = lambda item: enqueued_items.append(item)

        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            off2 = len(s1) + 1
            off3 = off2 + len(s2) + 1
            mock_split.return_value = [(0, s1), (off2, s2), (off3, s3)]
            res = pr.doProofreading("test-doc", paragraph, mock_locale_fixture, 0, len(paragraph), ())

        assert len(res.aErrors) == 2
        assert len(enqueued_items) == 1
        assert enqueued_items[0].text == s2

    def test_do_proofreading_splits_paragraph_once(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        pr = _make_proofreader()
        paragraph = "Sentence one. Sentence two. Sentence three."
        with patch.object(
            proofreader,
            "candidate_sentence_spans_for_proofreading",
            wraps=proofreader.candidate_sentence_spans_for_proofreading,
        ) as mock_split:
            pr.doProofreading("test-doc", paragraph, mock_locale_fixture, 0, 10, ())
        assert mock_split.call_count == 1

    def test_do_proofreading_exception_returns_empty_result(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """Characterization: unexpected errors in span resolution return an empty result, not a crash."""
        pr = _make_proofreader()
        with patch("plugin.writer.locale.ai_grammar_proofreader.resolve_work_spans", side_effect=RuntimeError("span resolution failed")):
            res = pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
        assert res.aErrors == ()
        mock_queue_fixture.enqueue.assert_not_called()

    def test_do_proofreading_does_not_retry_failed_empty_result(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """A failed ProofreadingResult create must not be attempted again.

        The handler used to call _create_empty_result a second time with the
        same arguments. That second failure escaped onto the Linguistic worker.
        """
        pr = _make_proofreader()
        with patch.object(
            proofreader,
            "_create_empty_result",
            side_effect=RuntimeError("createUnoStruct failed"),
        ) as mock_create:
            with pytest.raises(RuntimeError, match="createUnoStruct failed"):
                pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
        assert mock_create.call_count == 1

    def test_harper_incremental_returns_only_active_sentence_errors(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """Harper incremental calls must not re-attach other sentences' cached errors.

        The LLM path still returns the whole paragraph (async-cache trick). Harper
        already returned sentence 1 on the call Writer made for sentence 1.
        """
        pr = _make_proofreader()
        pr._profile = GrammarProfile("harper", "harper", False)
        s1, s2 = "First sentence.", "Second sentence."
        paragraph = f"{s1} {s2}"
        off2 = len(s1) + 1
        gc.cache_put_sentence(
            "en-US",
            s1,
            [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "harper||s1"}],
            doc_id="test-doc",
            checker_identity="harper",
        )
        s2_payload = [
            {
                "wrong": "Second",
                "correct": "Seconds",
                "n_error_start": 0,
                "n_error_length": 6,
                "rule_identifier": "harper||s2",
                "suggestions": ["Seconds"],
                "reason": "spelling",
                "type": "spelling",
            }
        ]

        def _lint(text: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            assert text == s2
            return {"errors": s2_payload}

        with (
            patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
            patch("plugin.writer.locale.harper.harper_try_lint", side_effect=_lint) as mock_lint,
        ):
            mock_split.return_value = [(0, s1), (off2, s2)]
            res = pr.doProofreading("test-doc", paragraph, mock_locale_fixture, off2, len(paragraph), ())

        mock_queue_fixture.enqueue.assert_not_called()
        mock_lint.assert_called_once()
        assert len(res.aErrors) == 1
        assert res.aErrors[0].nErrorStart == off2
        assert res.aErrors[0].aRuleIdentifier == "harper||s2"

    def test_harper_n_start_zero_returns_whole_paragraph(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """``n_start == 0`` is a paragraph pass: Harper still returns every sentence."""
        pr = _make_proofreader()
        pr._profile = GrammarProfile("harper", "harper", False)
        s1, s2 = "First sentence.", "Second sentence."
        paragraph = f"{s1} {s2}"
        off2 = len(s1) + 1
        cache_kwargs: dict[str, Any] = {"doc_id": "test-doc", "checker_identity": "harper"}
        gc.cache_put_sentence(
            "en-US",
            s1,
            [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "harper||s1"}],
            **cache_kwargs,
        )
        gc.cache_put_sentence(
            "en-US",
            s2,
            [{"n_error_start": 0, "n_error_length": 6, "rule_identifier": "harper||s2"}],
            **cache_kwargs,
        )

        with (
            patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
            patch("plugin.writer.locale.harper.harper_try_lint") as mock_lint,
        ):
            mock_split.return_value = [(0, s1), (off2, s2)]
            cached = pr.doProofreading("test-doc", paragraph, mock_locale_fixture, 0, len(paragraph), ())

        mock_lint.assert_not_called()
        mock_queue_fixture.enqueue.assert_not_called()
        assert {(e.nErrorStart, e.aRuleIdentifier) for e in cached.aErrors} == {
            (0, "harper||s1"),
            (off2, "harper||s2"),
        }

        gc.grammar_registry.clear_all()
        lint_by_text = {
            s1: {
                "wrong": "First",
                "correct": "1st",
                "n_error_start": 0,
                "n_error_length": 5,
                "rule_identifier": "harper||s1",
                "suggestions": ["1st"],
                "reason": "spelling",
                "type": "spelling",
            },
            s2: {
                "wrong": "Second",
                "correct": "2nd",
                "n_error_start": 0,
                "n_error_length": 6,
                "rule_identifier": "harper||s2",
                "suggestions": ["2nd"],
                "reason": "spelling",
                "type": "spelling",
            },
        }

        def _lint(text: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"errors": [lint_by_text[text]]}

        with (
            patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
            patch("plugin.writer.locale.harper.harper_try_lint", side_effect=_lint) as mock_lint,
        ):
            mock_split.return_value = [(0, s1), (off2, s2)]
            linted = pr.doProofreading("test-doc", paragraph, mock_locale_fixture, 0, len(paragraph), ())

        assert mock_lint.call_count == 2
        assert {(e.nErrorStart, e.aRuleIdentifier) for e in linted.aErrors} == {
            (0, "harper||s1"),
            (off2, "harper||s2"),
        }

    def test_harper_fast_path_returns_lint_errors_without_enqueue(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """Cache miss + Harper lint: returns lint errors synchronously without enqueuing."""
        pr = _make_proofreader()
        pr._profile = GrammarProfile("harper", "harper", False)
        sentence = "This are an test."
        payload = {
            "wrong": "are",
            "correct": "is",
            "n_error_start": 5,
            "n_error_length": 3,
            "rule_identifier": "harper||Agreement",
            "suggestions": ["is"],
            "reason": "grammar",
            "type": "grammar",
        }

        def _lint(text: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            assert text == sentence
            return {"errors": [payload]}

        with (
            patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
            patch("plugin.writer.locale.harper.harper_try_lint", side_effect=_lint),
        ):
            mock_split.return_value = [(0, sentence)]
            res = pr.doProofreading("test-doc", sentence, mock_locale_fixture, 0, len(sentence), ())

        mock_queue_fixture.enqueue.assert_not_called()
        assert len(res.aErrors) == 1
        assert res.aErrors[0].aRuleIdentifier == "harper||Agreement"

    def test_harper_fast_path_empty_lint_returns_no_errors(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """True empty Harper lint returns empty aErrors."""
        pr = _make_proofreader()
        pr._profile = GrammarProfile("harper", "harper", False)
        sentence = "This is fine."

        with (
            patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
            patch("plugin.writer.locale.harper.harper_try_lint", return_value={"errors": []}),
            patch("plugin.writer.locale.grammar_proofread_cache.normalize_errors_for_text", return_value=[]),
        ):
            mock_split.return_value = [(0, sentence)]
            res = pr.doProofreading("test-doc", sentence, mock_locale_fixture, 0, len(sentence), ())

        assert res.aErrors == ()

    def test_harper_skips_non_english_locale_before_lint(
        self, mock_config_fixture, mock_queue_fixture
    ) -> None:
        pr = _make_proofreader()
        pr._profile = GrammarProfile("harper", "harper", True)
        de_loc = MagicMock()
        de_loc.Language = "de"
        de_loc.Country = "DE"
        de_loc.Variant = ""
        assert pr.hasLocale(de_loc) is False
        with patch("plugin.writer.locale.harper.harper_try_lint") as mock_lint:
            res = pr.doProofreading("test-doc", "Hallo.", de_loc, 0, 6, ())
        mock_lint.assert_not_called()
        assert res.aErrors == ()

    def test_harper_lints_british_english_with_en_gb_tag(
        self, mock_config_fixture, mock_queue_fixture
    ) -> None:
        pr = _make_proofreader()
        pr._profile = GrammarProfile("harper", "harper", True)
        gb_loc = MagicMock()
        gb_loc.Language = "en"
        gb_loc.Country = "GB"
        gb_loc.Variant = ""
        sentence = "They is here."

        def _lint(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            assert kwargs.get("bcp47") == "en-GB"
            return {"errors": []}

        with (
            patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
            patch("plugin.writer.locale.harper.harper_try_lint", side_effect=_lint) as mock_lint,
        ):
            mock_split.return_value = [(0, sentence)]
            pr.doProofreading("test-doc", sentence, gb_loc, 0, len(sentence), ())
        mock_lint.assert_called()

    def test_llm_provider_does_not_use_harper_locale_gate(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        pr = _make_proofreader()
        pr._profile = GrammarProfile("llm", "llm:test", False)
        de_loc = MagicMock()
        de_loc.Language = "de"
        de_loc.Country = "DE"
        de_loc.Variant = ""
        with (
            patch("plugin.writer.locale.harper.harper_try_lint") as mock_lint,
            patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
        ):
            mock_split.return_value = [(0, "Hallo.")]
            pr.doProofreading("test-doc", "Hallo.", de_loc, 0, 6, ())
        mock_lint.assert_not_called()
        mock_queue_fixture.enqueue.assert_called()

    def test_llm_incremental_still_returns_other_sentences_cached_errors(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """Queued providers keep the async trick: sentence-2 call still carries sentence 1."""
        pr = _make_proofreader()
        s1, s2 = "First sentence.", "Second sentence."
        paragraph = f"{s1} {s2}"
        off2 = len(s1) + 1
        gc.cache_put_sentence(
            "en-US",
            s1,
            [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "r1"}],
            doc_id="test-doc",
        )
        enqueued_items: list[Any] = []
        mock_queue_fixture.enqueue.side_effect = lambda item: enqueued_items.append(item)

        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            mock_split.return_value = [(0, s1), (off2, s2)]
            res = pr.doProofreading("test-doc", paragraph, mock_locale_fixture, off2, len(paragraph), ())

        assert len(res.aErrors) == 1
        assert res.aErrors[0].nErrorStart == 0
        assert res.aErrors[0].aRuleIdentifier == "r1"
        assert len(enqueued_items) == 1
        assert enqueued_items[0].text == s2

    def test_do_proofreading_marshals_persistence_bind_off_main_thread(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """First off-thread call hops once to bind; second call uses fast path without hop."""
        pr = _make_proofreader()
        mock_model = MagicMock()
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=False),
            patch(
                "plugin.framework.queue_executor.main.call",
                side_effect=lambda fn, *args, timeout=30.0, bound_scope=None, **kwargs: fn(*args, **kwargs),
            ) as mock_exec,
            patch("plugin.writer.locale.ai_grammar_proofreader._ensure_persistence_bound", return_value=mock_model) as mock_bind,
            patch("plugin.framework.uno_context.get_runtime_uid", return_value="rt-1"),
        ):
            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            assert mock_exec.call_count == 1
            assert mock_bind.call_count == 1
            assert pr._doc_id_for_ignore["rt-1"] == "test-doc"

            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            assert mock_exec.call_count == 1
            assert mock_bind.call_count == 1

    def test_do_proofreading_marshals_runtime_uid_off_main_thread(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """First call hops once and records uid; second call does not execute on main thread."""
        pr = _make_proofreader()
        model = MagicMock()
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=False),
            patch(
                "plugin.framework.queue_executor.main.call",
                side_effect=lambda fn, *args, timeout=30.0, bound_scope=None, **kwargs: fn(*args, **kwargs),
            ) as mock_exec,
            patch(
                "plugin.writer.locale.ai_grammar_proofreader._ensure_persistence_bound",
                return_value=model,
            ) as mock_bind,
            patch("plugin.framework.uno_context.get_runtime_uid", return_value="rt-1"),
            patch(
                "plugin.writer.locale.ai_grammar_proofreader.candidate_sentence_spans_for_proofreading",
                return_value=[],
            ),
        ):
            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            assert pr._doc_id_for_ignore["rt-1"] == "test-doc"
            assert mock_exec.call_count == 1
            assert mock_bind.call_count == 1

            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            assert mock_exec.call_count == 1
            assert mock_bind.call_count == 1

    def test_do_proofreading_failed_bind_backoff(
        self, mock_config_fixture, mock_locale_fixture, mock_queue_fixture
    ) -> None:
        """Failed bind sets a 1.5s backoff; calls inside TTL do not hop, call after deadline does."""
        pr = _make_proofreader()
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=False),
            patch(
                "plugin.framework.queue_executor.main.call",
                side_effect=lambda fn, *args, timeout=30.0, bound_scope=None, **kwargs: fn(*args, **kwargs),
            ) as mock_exec,
            patch("plugin.writer.locale.ai_grammar_proofreader._ensure_persistence_bound", return_value=None) as mock_bind,
            patch(
                "plugin.writer.locale.ai_grammar_proofreader.candidate_sentence_spans_for_proofreading",
                return_value=[],
            ),
        ):
            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            assert mock_exec.call_count == 1
            assert mock_bind.call_count == 1

            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            assert mock_exec.call_count == 1
            assert mock_bind.call_count == 1

            pr._failed_bind_deadlines["test-doc"] = time.monotonic() - 1.0

            pr.doProofreading("test-doc", "Hello.", mock_locale_fixture, 0, 6, ())
            assert mock_exec.call_count == 2
            assert mock_bind.call_count == 2


def _writer_model() -> MagicMock:
    model = MagicMock()
    model.supportsService.side_effect = lambda service: service == "com.sun.star.text.TextDocument"
    return model


def test_ensure_persistence_bound_passes_current_text_component() -> None:
    """The linguistic id is not a model lookup. Bind the desktop's Writer."""
    from plugin.writer.locale import grammar_persistence as gp

    ctx = MagicMock()
    writer = _writer_model()
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = writer
    gp.grammar_registry.clear_all()
    try:
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=True),
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("plugin.doc.udprops.get_document_property", return_value=None) as mock_load,
        ):
            proofreader._ensure_persistence_bound(ctx, "2")
            again_loads = mock_load.call_count
            proofreader._ensure_persistence_bound(ctx, "2")
        bound = gp.get_persistence("2")
        assert bound is not None
        assert bound.model is writer
        assert mock_load.call_count == again_loads
    finally:
        gp.grammar_registry.clear_all()


def test_ensure_persistence_bound_uses_only_open_writer() -> None:
    """Current component is not Writer; the single open Writer is this call."""
    from plugin.writer.locale import grammar_persistence as gp

    ctx = MagicMock()
    writer = _writer_model()
    calc = MagicMock()
    calc.supportsService.return_value = False
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.side_effect = [writer]
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = calc
    desktop.getComponents.return_value = comps
    gp.grammar_registry.clear_all()
    try:
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=True),
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("plugin.doc.udprops.get_document_property", return_value=None),
        ):
            proofreader._ensure_persistence_bound(ctx, "4")
        bound = gp.get_persistence("4")
        assert bound is not None
        assert bound.model is writer
    finally:
        gp.grammar_registry.clear_all()


@pytest.mark.parametrize("writer_count", [0, 2])
def test_ensure_persistence_bound_does_not_guess_or_bind_without_unique_writer(writer_count: int) -> None:
    """Zero or several open Writers with a non-RuntimeUID key must not register a bind."""
    from plugin.writer.locale import grammar_persistence as gp

    ctx = MagicMock()
    writers = [_writer_model() for idx in range(writer_count)]
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True] * writer_count + [False]
    enum.nextElement.side_effect = writers
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    desktop.getComponents.return_value.createEnumeration.return_value = enum
    gp.grammar_registry.clear_all()
    try:
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=True),
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("plugin.framework.uno_context.get_runtime_uid", return_value=""),
        ):
            proofreader._ensure_persistence_bound(ctx, "2")
        assert gp.get_document_model_for_id("2") is None
        assert "2" not in gp.grammar_registry.doc_persistence_instances
    finally:
        gp.grammar_registry.clear_all()


def test_proofreader_broadcast_proofread_again_notifies_listeners(
    mock_config_fixture, mock_locale_fixture
) -> None:
    del mock_locale_fixture
    pr = _make_proofreader()
    listener = MagicMock()
    with patch("plugin.writer.locale.harper.runtime.is_ready", return_value=False):
        assert pr.addLinguServiceEventListener(listener) is True
    pr.broadcast_proofread_again()
    listener.processLinguServiceEvent.assert_called_once()
    event = listener.processLinguServiceEvent.call_args[0][0]
    assert event.nEvent == 8
    assert pr.removeLinguServiceEventListener(listener) is True


def test_broadcast_proofread_again_without_listeners_recovers_on_first_attach(
    mock_config_fixture, mock_locale_fixture
) -> None:
    """Empty-listener PROOFREAD_AGAIN must not drop the re-walk forever."""
    del mock_locale_fixture
    pr = _make_proofreader()
    listener = MagicMock()
    pr.broadcast_proofread_again()
    assert pr._pending_proofread_again is True
    listener.processLinguServiceEvent.assert_not_called()
    with (
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("plugin.writer.locale.harper.runtime.is_ready", return_value=False),
    ):
        assert pr.addLinguServiceEventListener(listener) is True
    listener.processLinguServiceEvent.assert_called_once()
    assert listener.processLinguServiceEvent.call_args[0][0].nEvent == 8
    assert pr._pending_proofread_again is False
    extra = MagicMock()
    with (
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("plugin.writer.locale.harper.runtime.is_ready", return_value=True),
    ):
        pr._profile = GrammarProfile("harper", "harper", False)
        assert pr.addLinguServiceEventListener(extra) is True
    listener.processLinguServiceEvent.assert_called_once()
    extra.processLinguServiceEvent.assert_not_called()


def test_first_listener_while_harper_ready_triggers_one_rewalk(
    mock_config_fixture, mock_locale_fixture
) -> None:
    del mock_locale_fixture
    pr = _make_proofreader()
    pr._profile = GrammarProfile("harper", "harper", False)
    first = MagicMock()
    second = MagicMock()
    with (
        patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        patch("plugin.writer.locale.harper.runtime.is_ready", return_value=True),
    ):
        assert pr.addLinguServiceEventListener(first) is True
        assert pr.addLinguServiceEventListener(second) is True
        assert pr.addLinguServiceEventListener(first) is True
    first.processLinguServiceEvent.assert_called_once()
    second.processLinguServiceEvent.assert_not_called()
    assert first.processLinguServiceEvent.call_args[0][0].nEvent == 8


def test_first_listener_while_harper_idle_does_not_rewalk(
    mock_config_fixture, mock_locale_fixture
) -> None:
    del mock_locale_fixture
    pr = _make_proofreader()
    pr._profile = GrammarProfile("harper", "harper", False)
    listener = MagicMock()
    with (
        patch("plugin.framework.queue_executor.main.post") as mock_post,
        patch("plugin.writer.locale.harper.runtime.is_ready", return_value=False),
    ):
        assert pr.addLinguServiceEventListener(listener) is True
    mock_post.assert_not_called()
    listener.processLinguServiceEvent.assert_not_called()
    assert pr._pending_proofread_again is False


def test_first_listener_ready_rewalk_skips_non_harper_provider(
    mock_config_fixture, mock_locale_fixture
) -> None:
    del mock_locale_fixture
    pr = _make_proofreader()
    listener = MagicMock()
    with (
        patch("plugin.framework.queue_executor.main.post") as mock_post,
        patch("plugin.writer.locale.harper.runtime.is_ready", return_value=True),
    ):
        assert pr.addLinguServiceEventListener(listener) is True
    mock_post.assert_not_called()
    listener.processLinguServiceEvent.assert_not_called()


def test_ensure_writeragent_proofreader_configured_triggers_harper_warmup() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import ensure_writeragent_proofreader_configured

    ctx = MagicMock()
    with (
        patch("plugin.framework.config.init_config"),
        patch("plugin.writer.locale.grammar_settings.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.user_config_dir", return_value="/tmp/lo-user"),
        patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_warmup,
    ):
        ensure_writeragent_proofreader_configured(ctx)
        mock_warmup.assert_called_once()
        assert mock_warmup.call_args.kwargs["user_config_dir"] == "/tmp/lo-user"


def test_ensure_writeragent_proofreader_configured_skips_warmup_without_config_dir() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import ensure_writeragent_proofreader_configured

    ctx = MagicMock()
    with (
        patch("plugin.framework.config.init_config"),
        patch("plugin.writer.locale.grammar_settings.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.user_config_dir", return_value=""),
        patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_warmup,
    ):
        ensure_writeragent_proofreader_configured(ctx)
        mock_warmup.assert_not_called()


def test_writeragent_proofreader_init_does_not_start_harper() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    ctx = MagicMock()
    with (
        patch("plugin.framework.logging.init_logging"),
        patch("plugin.writer.locale.grammar_persistence.grammar_registry.register_live_proofreader"),
        patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_warmup,
    ):
        WriterAgentAiGrammarProofreader(ctx)
        mock_warmup.assert_not_called()

def test_try_harper_fast_path_emits_status_when_ready() -> None:
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    ctx = MagicMock()
    with (
        patch("plugin.framework.logging.init_logging"),
        patch("plugin.writer.locale.grammar_persistence.grammar_registry.register_live_proofreader"),
    ):
        pr = WriterAgentAiGrammarProofreader(ctx)
    pr._profile = GrammarProfile("harper", "harper", False)
    combined: list = []
    with (
        patch("plugin.framework.config.user_config_dir", return_value="/tmp"),
        patch("plugin.writer.locale.harper.harper_try_lint", return_value={"errors": []}),
        patch("plugin.writer.locale.ai_grammar_proofreader.get_grammar_status") as mock_status_fn,
        patch("plugin.writer.locale.grammar_ignore_rules.doc_ignored_rules", return_value=set()),
        patch("plugin.writer.locale.grammar_ignore_rules.ignored_rules_snapshot", return_value=()),
        patch("plugin.writer.locale.grammar_proofread_cache.normalize_errors_for_text", return_value=[]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence"),
    ):
        pr._try_harper_fast_path("doc", "en-US", [(0, 18, "This are an test.")], combined, checker_identity="harper")
    mock_status = mock_status_fn.return_value
    mock_status.start.assert_called_once()
    assert mock_status.start.call_args.args == ("This are an test.",)
    assert mock_status.start.call_args.kwargs.get("routine") is True
    mock_status.request.assert_called_once()
    assert mock_status.request.call_args.args == ("This are an test.",)
    assert mock_status.request.call_args.kwargs.get("routine") is True
    mock_status.done.assert_called_once()
    assert mock_status.done.call_args.args == ("This are an test.",)
    assert mock_status.done.call_args.kwargs.get("routine") is True


def test_try_harper_fast_path_emits_starting_when_ensure(
    mock_config_fixture, mock_locale_fixture, mock_queue_fixture
) -> None:
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    ctx = MagicMock()
    with (
        patch("plugin.framework.logging.init_logging"),
        patch("plugin.writer.locale.grammar_persistence.grammar_registry.register_live_proofreader"),
    ):
        pr = WriterAgentAiGrammarProofreader(ctx)
    pr._profile = GrammarProfile("harper", "harper", False)
    with (
        patch("plugin.framework.config.user_config_dir", return_value="/tmp"),
        patch("plugin.writer.locale.harper.harper_try_lint", return_value=None),
        patch("plugin.writer.locale.ai_grammar_proofreader.get_grammar_status") as mock_status_fn,
    ):
        pr._try_harper_fast_path("doc", "en-US", [(0, 5, "Hello.")], [], checker_identity="harper")
    mock_status = mock_status_fn.return_value
    mock_status.start.assert_called_once()
    assert mock_status.start.call_args.args == ("Hello.",)
    assert mock_status.start.call_args.kwargs.get("routine") is True
    mock_status.request.assert_called_once()
    assert mock_status.request.call_args.args == ("Hello.",)
    assert mock_status.request.call_args.kwargs.get("routine") is None
    mock_status.done.assert_not_called()

    with (
        patch("plugin.framework.config.user_config_dir", return_value="/tmp"),
        patch("plugin.writer.locale.harper.harper_try_lint", return_value=None),
        patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences", return_value=[(0, "Hello.")]),
    ):
        pr.doProofreading("doc", "Hello.", mock_locale_fixture, 0, len("Hello."), ())
    mock_queue_fixture.enqueue.assert_not_called()

def test_ensure_persistence_bound_prioritizes_runtime_uid_over_current_component() -> None:
    """It must bind the correct Writer job doc even if Draw/Impress is focused."""
    from plugin.writer.locale import grammar_persistence as gp

    ctx = MagicMock()
    draw_comp = MagicMock()
    draw_comp.supportsService.return_value = False

    writer_match = _writer_model()
    writer_other = _writer_model()

    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = [writer_other, writer_match]

    comps = MagicMock()
    comps.createEnumeration.return_value = enum

    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = draw_comp
    desktop.getComponents.return_value = comps

    gp.grammar_registry.clear_all()
    try:
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=True),
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("plugin.doc.udprops.get_document_property", return_value=None),
            patch("plugin.framework.uno_context.get_runtime_uid", side_effect=["other-id", "match-id"]),
        ):
            proofreader._ensure_persistence_bound(ctx, "match-id")
        bound = gp.get_persistence("match-id")
        assert bound is not None
        assert bound.model is writer_match
    finally:
        gp.grammar_registry.clear_all()

def test_ignore_rule_fails_loudly_when_doc_id_unresolved(mock_config_fixture, mock_locale_fixture) -> None:
    pr = _make_proofreader()
    with patch("plugin.writer.locale.ai_grammar_proofreader._ignore_rule_on_main") as mock_ignore, \
         patch.object(pr, "_resolve_doc_id_for_ignore", return_value=None):
        with pytest.raises(RuntimeError, match="ignoreRule failed: no active document found"):
            pr.ignoreRule("test-rule", mock_locale_fixture)
    mock_ignore.assert_not_called()

def test_reset_ignore_rules_fails_loudly_when_doc_id_unresolved(mock_config_fixture) -> None:
    pr = _make_proofreader()
    with patch("plugin.writer.locale.ai_grammar_proofreader._reset_ignore_rules_on_main") as mock_reset, \
         patch.object(pr, "_resolve_doc_id_for_ignore", return_value=None):
        with pytest.raises(RuntimeError, match="resetIgnoreRules failed: no active document found"):
            pr.resetIgnoreRules()
    mock_reset.assert_not_called()

def test_enqueue_misses_persists_identity_from_active_provider(mock_config_fixture, mock_locale_fixture, mock_queue_fixture) -> None:
    pr = _make_proofreader()

    with patch("plugin.writer.locale.grammar_settings.provider", return_value="languagetool"):
        pr.doProofreading("test-doc", "Some text.", mock_locale_fixture, 0, 10, ())

    mock_queue_fixture.enqueue.assert_called_once()
    item = mock_queue_fixture.enqueue.call_args[0][0]
    assert item.provider == "languagetool"
    assert item.checker_identity == "languagetool"


def test_llm_checker_identity_cache_roundtrip_preserves_rows_and_ignores(
    mock_config_fixture, mock_locale_fixture, mock_queue_fixture
) -> None:
    """LLM results cached with checker_identity avoid identity-switch clearing of L2 and ignores."""
    from plugin.writer.locale import grammar_persistence as gp
    from plugin.writer.locale.grammar_worker import process_grammar_results

    pr = _make_proofreader()
    s1 = "This are wrong."
    s2 = "They is wrong."

    with (
        patch("plugin.writer.locale.grammar_settings.checker_identity", return_value="llm:gpt-test"),
        patch("plugin.writer.locale.grammar_settings.provider", return_value="llm"),
    ):
        pr.doProofreading("test-doc", s1, mock_locale_fixture, 0, len(s1), ())
        assert mock_queue_fixture.enqueue.call_count == 1
        item1 = mock_queue_fixture.enqueue.call_args[0][0]
        assert item1.provider == "llm"
        assert item1.checker_identity == "llm:gpt-test"

        ec = GrammarWorkerContext(
            ctx=pr.ctx,
            client=MagicMock(),
            gq=GrammarWorkQueue(),
            model="gpt-test",
            grammar_bcp47="en-US",
            max_tok=512,
        )
        err1 = {
            "wrong": "are",
            "correct": "is",
            "n_error_start": 5,
            "n_error_length": 3,
            "rule_identifier": "grammar||Agreement",
            "suggestions": ["is"],
            "reason": "grammar",
            "type": "grammar",
        }
        process_grammar_results([(item1, s1)], [[err1]], "en-US", "", 10, ec)

        mock_queue_fixture.enqueue.reset_mock()
        res1 = pr.doProofreading("test-doc", s1, mock_locale_fixture, 0, len(s1), ())
        assert len(res1.aErrors) == 1
        assert res1.aErrors[0].aRuleIdentifier == "grammar||Agreement"
        mock_queue_fixture.enqueue.assert_not_called()

        p = gp.get_persistence("test-doc")
        assert p is not None
        p._ignored_rules.add("some-ignored-rule")

        pr.doProofreading("test-doc", s2, mock_locale_fixture, 0, len(s2), ())
        assert mock_queue_fixture.enqueue.call_count == 1
        item2 = mock_queue_fixture.enqueue.call_args[0][0]
        assert item2.provider == "llm"
        assert item2.checker_identity == "llm:gpt-test"

        err2 = {
            "wrong": "is",
            "correct": "are",
            "n_error_start": 5,
            "n_error_length": 2,
            "rule_identifier": "grammar||Agreement2",
            "suggestions": ["are"],
            "reason": "grammar",
            "type": "grammar",
        }
        process_grammar_results([(item2, s2)], [[err2]], "en-US", "", 10, ec)

        mock_queue_fixture.enqueue.reset_mock()
        combined_text = f"{s1} {s2}"
        with patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split:
            mock_split.return_value = [(0, s1), (len(s1) + 1, s2)]
            res_both = pr.doProofreading("test-doc", combined_text, mock_locale_fixture, 0, len(combined_text), ())

        assert len(res_both.aErrors) == 2
        mock_queue_fixture.enqueue.assert_not_called()
        assert len(p._entries) == 2
        assert "some-ignored-rule" in p._ignored_rules


def test_local_provider_checker_identity_and_persistence(
    mock_config_fixture, mock_locale_fixture, mock_queue_fixture
) -> None:
    """Local providers (LanguageTool, Harper) use provider as checker_identity and keep cache rows."""
    from plugin.writer.locale import grammar_persistence as gp
    from plugin.writer.locale.grammar_worker import process_grammar_results

    pr = _make_proofreader()
    s1 = "This are wrong."
    s2 = "They is wrong."

    with patch("plugin.writer.locale.grammar_settings.provider", return_value="languagetool"):
        pr.doProofreading("test-doc", s1, mock_locale_fixture, 0, len(s1), ())
        assert mock_queue_fixture.enqueue.call_count == 1
        item1 = mock_queue_fixture.enqueue.call_args[0][0]
        assert item1.provider == "languagetool"
        assert item1.checker_identity == "languagetool"

        ec = GrammarWorkerContext(
            ctx=pr.ctx,
            client=MagicMock(),
            gq=GrammarWorkQueue(),
            model="",
            grammar_bcp47="en-US",
            max_tok=512,
        )
        err1 = {
            "wrong": "are",
            "correct": "is",
            "n_error_start": 5,
            "n_error_length": 3,
            "rule_identifier": "lt||Agreement",
            "suggestions": ["is"],
            "reason": "grammar",
            "type": "grammar",
        }
        process_grammar_results([(item1, s1)], [[err1]], "en-US", "", 10, ec)

        mock_queue_fixture.enqueue.reset_mock()
        pr.doProofreading("test-doc", s2, mock_locale_fixture, 0, len(s2), ())
        assert mock_queue_fixture.enqueue.call_count == 1
        item2 = mock_queue_fixture.enqueue.call_args[0][0]
        assert item2.provider == "languagetool"
        assert item2.checker_identity == "languagetool"

        err2 = {
            "wrong": "is",
            "correct": "are",
            "n_error_start": 5,
            "n_error_length": 2,
            "rule_identifier": "lt||Agreement2",
            "suggestions": ["are"],
            "reason": "grammar",
            "type": "grammar",
        }
        process_grammar_results([(item2, s2)], [[err2]], "en-US", "", 10, ec)

        p = gp.get_persistence("test-doc")
        assert p is not None
        assert len(p._entries) == 2


def test_harper_partial_hit_converts_uno_tuple_once(
    mock_config_fixture, mock_locale_fixture, mock_queue_fixture
) -> None:
    """Partial hit (one cached, one Harper fast path) converts UNO errors exactly once."""
    pr = _make_proofreader()
    pr._profile = GrammarProfile("harper", "harper", False)

    s1 = "This are wrong."
    s2 = "They is wrong."
    paragraph = f"{s1} {s2}"
    off2 = len(s1) + 1

    gc.cache_put_sentence(
        "en-US",
        s1,
        [{"n_error_start": 5, "n_error_length": 3, "rule_identifier": "r1"}],
        doc_id="test-doc",
    )

    lint_error = {
        "wrong": "is",
        "correct": "are",
        "n_error_start": 5,
        "n_error_length": 2,
        "rule_identifier": "harper||s2",
        "suggestions": ["are"],
        "reason": "grammar",
        "type": "grammar",
    }

    with (
        patch("plugin.writer.locale.grammar_proofread_text.split_into_sentences") as mock_split,
        patch("plugin.writer.locale.harper.harper_try_lint", return_value={"errors": [lint_error]}),
        patch.object(proofreader, "_cached_errors_to_uno_tuple", wraps=proofreader._cached_errors_to_uno_tuple) as mock_convert,
    ):
        mock_split.return_value = [(0, s1), (off2, s2)]
        res = pr.doProofreading("test-doc", paragraph, mock_locale_fixture, 0, len(paragraph), ())

    assert len(res.aErrors) == 2
    assert mock_convert.call_count == 1


def test_ignore_rule_resolves_doc_id_from_map(mock_config_fixture, mock_locale_fixture) -> None:
    """ignoreRule resolves doc_id from _doc_id_for_ignore map when RuntimeUID matches."""
    pr = _make_proofreader()
    pr._doc_id_for_ignore["rt-42"] = "doc-42"
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = MagicMock()
    with (
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        patch("plugin.framework.uno_context.get_runtime_uid", return_value="rt-42"),
        patch("plugin.framework.queue_executor.main.is_current", return_value=True),
        patch("plugin.writer.locale.ai_grammar_proofreader._ignore_rule_on_main") as mock_ignore,
    ):
        pr.ignoreRule("test-rule", mock_locale_fixture)
        mock_ignore.assert_called_once_with(pr.ctx, "doc-42", "test-rule")


def test_emoji_shifts_cached_error_offsets(mock_config_fixture, mock_locale_fixture) -> None:
    pr = _make_proofreader()
    text = "👋 they is fine."
    # Python offsets: '👋' is 0, ' ' is 1, 'they is' is [2:9].
    # In UTF-16: '👋' is 0..2, ' ' is 2, 'they is' is [3:10].
    gc.cache_put_sentence(
        "en-US",
        text,
        [{
            "n_error_start": 2,
            "n_error_length": 7,
            "suggestions": ("they are",),
            "short_comment": "agr",
            "full_comment": "agr",
            "rule_identifier": "wa_g_rule||agr",
        }],
        doc_id="doc-emoji",
    )
    res = pr.doProofreading("doc-emoji", text, mock_locale_fixture, 0, len(text), ())
    assert res is not None
    errs = tuple(res.aErrors)
    assert len(errs) == 1
    e0 = errs[0]
    assert e0.nErrorStart == 3
    assert e0.nErrorLength == 7
    u16_encoded = text.encode("utf-16le")
    u16_slice = u16_encoded[e0.nErrorStart * 2 : (e0.nErrorStart + e0.nErrorLength) * 2].decode("utf-16le")
    assert u16_slice == "they is"


def test_nonzero_start_after_emoji_selects_sentence(mock_config_fixture, mock_locale_fixture) -> None:
    pr = _make_proofreader()
    text = "👋 Hello. Next sentence."
    # "👋 Hello. " in UTF-16 has length 11. "Next sentence." starts at UTF-16 offset 11.
    gc.cache_put_sentence(
        "en-US",
        "Next sentence.",
        [{
            "n_error_start": 0,
            "n_error_length": 4,
            "suggestions": ("Previous",),
            "short_comment": "test",
            "full_comment": "test",
            "rule_identifier": "wa_g_rule||test",
        }],
        doc_id="doc-emoji-next",
    )
    res = pr.doProofreading("doc-emoji-next", text, mock_locale_fixture, 11, 25, ())
    assert res is not None
    errs = tuple(res.aErrors)
    assert len(errs) == 1


def test_harper_fast_path_caches_ignored_rules_and_excludes_from_status(mock_config_fixture) -> None:
    pr = _make_proofreader()
    pr._profile = GrammarProfile("harper", "harper", False)
    work_spans = [(0, 15, "They is fine.")]
    combined: list[Any] = []
    norm_err = NormalizedProofError(
        n_error_start=0,
        n_error_length=7,
        suggestions=("They are",),
        short_comment="agr",
        full_comment="agr",
        rule_identifier="harper||agr",
    )
    with (
        patch("plugin.framework.config.user_config_dir", return_value="/tmp"),
        patch("plugin.writer.locale.harper.harper_try_lint", return_value={"errors": [{"wrong": "They is"}]}),
        patch("plugin.writer.locale.grammar_ignore_rules.doc_ignored_rules", return_value={"agr"}),
        patch("plugin.writer.locale.grammar_ignore_rules.ignored_rules_snapshot", return_value=()),
        patch("plugin.writer.locale.grammar_proofread_cache.normalize_errors_for_text", return_value=[norm_err]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put,
        patch("plugin.writer.locale.ai_grammar_proofreader.get_grammar_status") as mock_status_fn,
    ):
        pr._try_harper_fast_path("test-doc", "en-US", work_spans, combined, checker_identity="harper")
        mock_put.assert_called_once()
        cached_payload = mock_put.call_args[0][2]
        # Raw error is stored in cache despite being ignored
        assert len(cached_payload) == 1
        assert cached_payload[0]["rule_identifier"] == "harper||agr"
        mock_done = mock_status_fn.return_value.done
        mock_done.assert_called_once()
        assert mock_done.call_args.args == (work_spans[-1][2],)
        assert mock_done.call_args.kwargs.get("routine") is True


def test_reset_ignore_rules_broadcasts_and_subsequent_call_returns_error(
    mock_config_fixture, mock_locale_fixture
) -> None:
    pr = _make_proofreader()
    text = "Hello they is fine."
    rid = "wa_g_rule||agr"
    gc.cache_put_sentence(
        "en-US",
        text,
        [{
            "n_error_start": 6,
            "n_error_length": 7,
            "suggestions": ("they are",),
            "short_comment": "agr",
            "full_comment": "agr",
            "rule_identifier": rid,
        }],
        doc_id="doc-reset",
    )
    listener = MagicMock()
    pr.addLinguServiceEventListener(listener)

    with patch.object(pr, "_resolve_doc_id_for_ignore", return_value="doc-reset"):
        pr.ignoreRule(rid, mock_locale_fixture)

    # With rule ignored, doProofreading returns empty aErrors
    res1 = pr.doProofreading("doc-reset", text, mock_locale_fixture, 0, len(text), ())
    assert len(tuple(res1.aErrors)) == 0

    with (
        patch.object(pr, "_resolve_doc_id_for_ignore", return_value="doc-reset"),
        patch.object(pr, "_try_harper_fast_path") as mock_harper,
        patch("plugin.writer.locale.ai_grammar_proofreader.enqueue_misses") as mock_enqueue,
    ):
        pr.resetIgnoreRules()
        # Broadcasts PROOFREAD_AGAIN to listeners
        assert listener.processLinguServiceEvent.call_count == 1
        event = listener.processLinguServiceEvent.call_args[0][0]
        assert event.nEvent == 8

        # Next call returns cached error without running provider or enqueuing
        res2 = pr.doProofreading("doc-reset", text, mock_locale_fixture, 0, len(text), ())
        assert len(tuple(res2.aErrors)) == 1
        mock_harper.assert_not_called()
        mock_enqueue.assert_not_called()


def test_reset_ignore_rules_with_no_listeners_sets_pending() -> None:
    pr = _make_proofreader()
    assert pr._pending_proofread_again is False
    with patch.object(pr, "_resolve_doc_id_for_ignore", return_value="doc-nolistener"):
        pr.resetIgnoreRules()
    assert pr._pending_proofread_again is True


def test_bind_timeout_backs_off() -> None:
    pr = _make_proofreader()
    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=TimeoutError()) as mock_run,
    ):
        pr._ensure_document_bound("doc-timeout")
        assert mock_run.call_count == 1
        assert mock_run.call_args.kwargs["timeout"] == proofreader.BIND_RETRY_SEC
        assert "doc-timeout" in pr._failed_bind_deadlines

        # Within 1.5s, does not hop again
        pr._ensure_document_bound("doc-timeout")
        assert mock_run.call_count == 1

        # Past the deadline, hops again
        pr._failed_bind_deadlines["doc-timeout"] = time.monotonic() - 1.0
        pr._ensure_document_bound("doc-timeout")
        assert mock_run.call_count == 2
def test_llm_cache_hits_use_checked_grammar_text_prefix(mock_config_fixture, mock_locale_fixture) -> None:
    pr = _make_proofreader()
    full_sentence = "word1 word2 word3 word4 word5."
    prefix = "word1 word2 word3"
    with patch("plugin.writer.locale.grammar_proofread_locale.grammar_max_chars", return_value=20):
        gc.cache_put_sentence(
            "en-US",
            prefix,
            [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "r1"}],
            doc_id="test-doc",
            checker_identity="llm:model-x",
        )

        assert gc.cache_get_sentence("en-US", full_sentence, doc_id="test-doc", checker_identity="llm:model-x") is None

        combined, uncached = pr._process_cache_hits("test-doc", "en-US", [(0, len(full_sentence), full_sentence)], checker_identity="llm:model-x")
        assert len(combined) == 1
        assert combined[0]["n_error_start"] == 0
        assert uncached == []

        gc.cache_put_sentence(
            "en-US",
            full_sentence,
            [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "r2"}],
            doc_id="test-doc",
            checker_identity="harper",
        )
        harper_combined, harper_uncached = pr._process_cache_hits("test-doc", "en-US", [(0, len(full_sentence), full_sentence)], checker_identity="harper")
        assert len(harper_combined) == 1
        assert harper_combined[0]["rule_identifier"] == "r2"
        assert harper_uncached == []

