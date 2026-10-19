# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grammar worker LLM orchestration."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
import pytest
from unittest.mock import ANY, MagicMock, patch

from plugin.writer.locale.grammar_worker import (
    _detect_lang_instruction,
    GrammarWorkerContext,
    LangRequeueAction,
    build_grammar_system_prompt,
    call_grammar_llm,
    decide_grammar_completion,
    decide_language_validation,
    detect_languages_for_chunk,
    language_detect_llm_sync,
    run_grammar_check,
    run_llm_and_cache_batch,
)
from plugin.writer.locale.grammar_proofread_text import NormalizedProofError
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem, GrammarWorkQueue
from .test_grammar_work_queue import _grammar_obs_call_sites_present


def _item(
    text: str = "They is here.",
    *,
    doc_id: str = "d1",
    seq: int = 1,
    inflight_key: str = "k1",
    grammar_bcp47: str = "en-US",
    partial_sentence: bool = False,
    original_bcp47: str = "",
    provider: str = "",
    checker_identity: str = "",
) -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=MagicMock(),
        text=text,
        grammar_bcp47=grammar_bcp47,
        partial_sentence=partial_sentence,
        doc_id=doc_id,
        inflight_key=inflight_key,
        enqueue_seq=seq,
        original_bcp47=original_bcp47,
        provider=provider,
        checker_identity=checker_identity,
    )


@pytest.fixture(autouse=True)
def _reset_grammar_client_cache():
    from plugin.writer.locale.grammar_worker import reset_grammar_llm_client_cache

    reset_grammar_llm_client_cache()
    yield
    reset_grammar_llm_client_cache()


def _ec(client: MagicMock | None = None, *, detect_lang_mode: str = "llm") -> MagicMock:
    ec = MagicMock()
    ec.ctx = MagicMock()
    ec.client = client or MagicMock()
    ec.model = "test-model"
    ec.max_tok = 512
    ec.detect_lang_mode = detect_lang_mode
    ec.grammar_bcp47 = "en-US"
    return ec


def test_detect_lang_instruction_french_only_no_en_us() -> None:
    from plugin.writer.locale.grammar_persistence import grammar_registry
    grammar_registry.clear_for_doc("doc-fr")
    try:
        item = _item("Bonjour le monde.", doc_id="doc-fr", grammar_bcp47="fr-FR")
        instr = _detect_lang_instruction([(item, "Bonjour le monde.")])
        assert "fr-FR" in instr
        assert "en-US" not in instr
    finally:
        grammar_registry.clear_for_doc("doc-fr")


def test_build_grammar_system_prompt_batch_vs_single() -> None:
    batch = build_grammar_system_prompt("en-US", set(), batch=True, any_partial=False)
    single = build_grammar_system_prompt("en-US", set(), batch=False, any_partial=False)
    assert "multiple sentences" in batch
    assert "single JSON object" in single


def test_call_grammar_llm_single() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = '{"errors": []}'
    item = _item()
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker.get_active_ignored_reasons", return_value=set()):
        results, elapsed = call_grammar_llm([(item, item.text)], "en-US", ec)
    assert len(results) == 1
    assert elapsed >= 0
    args, kwargs = client.chat_completion_sync.call_args
    assert args[0][1]["content"] == item.text


def test_call_grammar_llm_batch() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = '{"results": [{"errors": []}, {"errors": []}]}'
    a, b = _item("A."), _item("B.")
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker.get_active_ignored_reasons", return_value=set()):
        results, _ = call_grammar_llm([(a, a.text), (b, b.text)], "en-US", ec)
    assert len(results) == 2
    args, kwargs = client.chat_completion_sync.call_args
    assert "1. A.\n2. B." in args[0][1]["content"]
    assert kwargs.get("max_tokens") == ec.max_tok


def test_call_grammar_llm_passes_minimal_reasoning() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = '{"errors": []}'
    item = _item()
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker.get_active_ignored_reasons", return_value=set()):
        call_grammar_llm([(item, item.text)], "en-US", ec)
    _, kwargs = client.chat_completion_sync.call_args
    assert kwargs.get("chat_extra") == {"reasoning": {"effort": "minimal"}}


def test_call_grammar_llm_empty_single_returns_clean_result() -> None:
    client = MagicMock()
    client.chat_completion_sync.return_value = ""
    item = _item()
    ec = _ec(client)
    with patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_worker.get_active_ignored_reasons", return_value=set()):
        results, _ = call_grammar_llm([(item, item.text)], "en-US", ec)
    assert len(results) == 1
    assert results[0] == []
    assert client.chat_completion_sync.call_count == 1


def test_language_detect_llm_sync_retries_on_empty() -> None:
    client = MagicMock()
    client.chat_completion_sync.side_effect = ["", '{"detected_language_bcp47": "en-US"}']
    ec = _ec(client)
    with patch("plugin.framework.queue_executor.grammar_llm_request_gate"):
        out = language_detect_llm_sync(ec, [{"role": "user", "content": "Hi"}], 64)
    assert "en-US" in out
    assert client.chat_completion_sync.call_count == 2
    assert client.chat_completion_sync.call_args_list[1].kwargs["max_tokens"] >= 256


def test_detect_languages_for_chunk_langdetect_mode() -> None:
    item = _item("Bonjour le monde.")
    ec = _ec(detect_lang_mode="langdetect")
    with patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.get_cached_language", return_value=None), \
         patch("plugin.writer.locale.grammar_worker.persisted_grammar_skip_lang_detect", return_value=False), \
         patch("plugin.writer.locale.langdetect_service.detect_languages", return_value=["fr-FR"]), \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status"):
        detected = detect_languages_for_chunk([(item, item.text)], "", ec)
    ec.client.chat_completion_sync.assert_not_called()
    assert detected == ["fr-FR"]


def _phase_item(key: str = "k1") -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=None,
        text="Hello world.",
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="d",
        inflight_key=key,
        enqueue_seq=1,
    )


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        # LLM ``ja`` and document ``ja-JP`` must not trigger a locale change.
        pytest.param("ja-JP", "ja", "ja-JP", id="test_decide_language_validation_ja_tag_matches_ja_jp"),
        pytest.param("en-US", "fr-FR", "fr-FR", id="test_decide_language_validation_single_mismatch_updates_target"),
    ],
)
def test_decide_language_validation_ja_tag_matches_ja_jp(value, value_2, expected) -> None:
    item = _phase_item()
    decision = decide_language_validation([(item, item.text)], value, [value_2])
    assert decision.target_bcp47 == expected
    assert decision.result_chunk == [(item, item.text)]
    assert decision.requeues == ()

def test_decide_language_validation_multi_mismatch_requeues() -> None:
    a, b = _phase_item("k1"), _phase_item("k2")
    decision = decide_language_validation([(a, a.text), (b, b.text)], "en-US", ["en-US", "fr-FR"])
    assert decision.target_bcp47 == "en-US"
    assert decision.result_chunk == [(a, a.text)]
    assert len(decision.requeues) == 1
    assert decision.requeues[0] == LangRequeueAction(b, b.text, "fr-FR", "en-US")


def test_decide_language_validation_all_match() -> None:
    a, b = _phase_item("k1"), _phase_item("k2")
    decision = decide_language_validation([(a, a.text), (b, b.text)], "en-US", ["en-US", "en-US"])
    assert decision.result_chunk == [(a, a.text), (b, b.text)]
    assert decision.requeues == ()


def test_decide_language_validation_multi_none_keeps_in_result_chunk() -> None:
    """Multi-batch None detect keeps items in result_chunk under target locale (fails open)."""
    a, b = _phase_item("k1"), _phase_item("k2")
    decision = decide_language_validation([(a, a.text), (b, b.text)], "en-US", [None, None])
    assert decision.result_chunk == [(a, a.text), (b, b.text)]
    assert decision.requeues == ()
    assert decision.target_bcp47 == "en-US"


def test_decide_language_validation_mixed_match_none_mismatch() -> None:
    a, b, c = _phase_item("k1"), _phase_item("k2"), _phase_item("k3")
    decision = decide_language_validation(
        [(a, a.text), (b, b.text), (c, c.text)],
        "en-US",
        ["en-US", None, "fr-FR"],
    )
    assert decision.result_chunk == [(a, a.text), (b, b.text)]
    assert len(decision.requeues) == 1
    assert decision.requeues[0] == LangRequeueAction(c, c.text, "fr-FR", "en-US")
    assert decision.target_bcp47 == "en-US"


@pytest.mark.parametrize(
    "value, value_2, value_3, value_4, expected, expected_2",
    [
        pytest.param(3, 2, "en-US", "en-US", True, False, id="test_decide_grammar_completion_mismatch_requeues_all"),
        pytest.param(1, 1, "ja-JP", "zh-CN", False, True, id="test_decide_grammar_completion_success_with_locale_change"),
        pytest.param(2, 2, "en-US", "en-US", False, False, id="test_decide_grammar_completion_success_same_locale"),
    ],
)
def test_decide_grammar_completion_mismatch_requeues_all(value, value_2, value_3, value_4, expected, expected_2) -> None:
    decision = decide_grammar_completion(value, value_2, value_3, value_4)
    assert decision.requeue_all is expected
    assert decision.apply_locale_after_success is expected_2

def test_decide_grammar_completion_no_apply_when_tags_equivalent() -> None:
    decision = decide_grammar_completion(1, 1, "ja-JP", "ja")
    assert decision.apply_locale_after_success is False

def test_batch_result_summary() -> None:
    from plugin.writer.locale.grammar_worker import _BatchResultSummary

    summary = _BatchResultSummary()
    assert summary.n_written == 0
    assert summary.preview_source() == ""

    summary.record("First sentence.", 2)
    assert summary.n_written == 1
    assert summary.total_issues == 2
    assert summary.chars_checked == 15
    assert summary.preview_source() == "First sentence."

    summary.record("Second sentence.", 1)
    assert summary.n_written == 2
    assert summary.total_issues == 3
    assert summary.chars_checked == 31
    assert summary.preview_source() == "First sentence. \u00b7 Second sentence."

    summary.record("Third sentence.", 0)
    assert summary.n_written == 3
    # Preview source stays first two
    assert summary.preview_source() == "First sentence. \u00b7 Second sentence."


def test_run_grammar_check_single_sentence_provider_dispatch() -> None:
    from plugin.writer.locale.grammar_worker import _SINGLE_SENTENCE_PROVIDERS

    item = _item("Hello world.")
    ec = _ec()

    for provider_name in ("languagetool", "vale", "harper"):
        spec = _SINGLE_SENTENCE_PROVIDERS[provider_name]
        with patch("plugin.framework.config.get_grammar_provider", return_value=provider_name), \
             patch("plugin.framework.config.user_config_dir", return_value="/tmp/test"), \
             patch("plugin.writer.locale.grammar_worker.run_single_sentence_provider") as mock_run:
            run_grammar_check([(item, item.text)], "en-US", "en-US", ec)
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0] == spec.name
            assert kwargs["obs_event_name"] == spec.obs_event
            assert kwargs["emit_request_status"] == spec.emit_request_status


def test_fill_from_cache_and_persistence() -> None:
    from plugin.writer.locale.grammar_worker import _fill_from_cache_and_persistence

    a = _item("Sentence in cache.")
    b = _item("Sentence in persistence.")
    c = _item("Sentence unknown.")
    ec = _ec()
    ec.grammar_bcp47 = "fr-FR"

    with patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.get_cached_language", side_effect=lambda t: "en-US" if t == a.text else None), \
         patch("plugin.writer.locale.grammar_worker.persisted_grammar_skip_lang_detect", side_effect=lambda _ctx, _doc, t, *_rest: t == b.text), \
         patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.put_cached_language") as mock_put:
        res = _fill_from_cache_and_persistence([(a, a.text), (b, b.text), (c, c.text)], ec)

    assert res == ["en-US", "fr-FR", None]
    mock_put.assert_called_once_with(b.text, "fr-FR")


def test_detect_via_llm_batch_success() -> None:
    from plugin.writer.locale.grammar_worker import _detect_via_llm_batch

    client = MagicMock()
    client.chat_completion_sync.return_value = '{"results": [{"detected_language_bcp47": "en-US"}, {"detected_language_bcp47": "de-DE"}]}'
    a, b = _item("Hello."), _item("Guten Tag.")
    ec = _ec(client)

    with patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.put_cached_language") as mock_put:
        res = _detect_via_llm_batch([(a, a.text), (b, b.text)], "Detect language", ec)

    assert res == ["en-US", "de-DE"]
    assert mock_put.call_count == 2


def test_detect_via_llm_batch_mismatch_returns_nones() -> None:
    from plugin.writer.locale.grammar_worker import _detect_via_llm_batch

    client = MagicMock()
    client.chat_completion_sync.return_value = '{"results": [{"detected_language_bcp47": "en-US"}]}'
    a, b = _item("Hello."), _item("Guten Tag.")
    ec = _ec(client)

    with patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"):
        res = _detect_via_llm_batch([(a, a.text), (b, b.text)], "Detect language", ec)

    assert res == [None, None]


def test_detect_via_llm_single_success_and_failure() -> None:
    from plugin.writer.locale.grammar_worker import _detect_via_llm_single

    client = MagicMock()
    client.chat_completion_sync.side_effect = [
        '{"detected_language_bcp47": "es-ES"}',
        'invalid json',
    ]
    ec = _ec(client)

    with patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.put_cached_language") as mock_put:
        res1 = _detect_via_llm_single("Hola mundo.", "Detect language", ec)
        res2 = _detect_via_llm_single("Invalid sentence.", "Detect language", ec)

    assert res1 == "es-ES"
    mock_put.assert_called_once_with("Hola mundo.", "es-ES")
    assert res2 is None


def test_persistence_get_and_put_cached_language() -> None:
    from plugin.writer.locale.grammar_persistence import grammar_registry

    grammar_registry.clear_all()
    assert grammar_registry.get_cached_language("Unknown text") is None

    grammar_registry.put_cached_language("Test text", "en-US")
    assert grammar_registry.get_cached_language("Test text") == "en-US"

    grammar_registry.clear_all()
    assert grammar_registry.get_cached_language("Test text") is None

def test_run_llm_and_cache_batch_success() -> None:
    """Verify that multiple items are batched and results are stored in cache."""
    ctx = MagicMock()
    # Mock config to enable checker
    with patch("plugin.framework.config.get_config_int_safe", return_value=4), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.client.model_fetcher.get_grammar_model", return_value="test-model"), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_client_cls, \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put, \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text") as mock_norm, \
         patch("plugin.writer.locale.grammar_proofread_cache.ignored_rules_snapshot", return_value=set()):

        mock_client = mock_client_cls.return_value
        # Mock LLM response with 2 results
        mock_client.chat_completion_sync.return_value = '{"results": [{"errors": [{"wrong": "is", "correct": "are"}]}, {"errors": []}]}'

        # Mock normalization to return a dummy error for the first sentence
        dummy_error = NormalizedProofError(n_error_start=5, n_error_length=2, suggestions=("are",), short_comment="grammar", full_comment="grammar", rule_identifier="wa_grammar_0_0f61208a")
        mock_norm.side_effect = [[dummy_error], []]

        items = [
            GrammarWorkItem(ctx=ctx, text="They is here.", grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key="k1", enqueue_seq=1),
            GrammarWorkItem(ctx=ctx, text="All good.", grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key="k2", enqueue_seq=2),
        ]

        run_llm_and_cache_batch(items)

        # Verify LLM was called once with batch prompt
        assert mock_client.chat_completion_sync.call_count == 1
        args, kwargs = mock_client.chat_completion_sync.call_args
        messages = args[0]
        assert "provide multiple sentences" in messages[0]["content"] # Batch prompt
        assert "1. They is here.\n2. All good." in messages[1]["content"]

        # Verify cache_put_sentence was called for each sentence
        assert mock_put.call_count == 2
        # First call: "They is here." -> one error
        mock_put.assert_any_call(
            "en-US",
            "They is here.",
            [{"n_error_start": 5, "n_error_length": 2, "suggestions": ("are",), "short_comment": "grammar", "full_comment": "grammar", "rule_identifier": "wa_grammar_0_0f61208a"}],
            ctx=ANY,
            doc_id="d1",
        )
        # Second call: "All good." -> no errors
        mock_put.assert_any_call("en-US", "All good.", [], ctx=ANY, doc_id="d1")


def test_run_llm_and_cache_batch_size_1() -> None:
    """Verify that multiple items are processed individually when batch_size is 1."""
    ctx = MagicMock()
    # Mock batch_size to 1 (the default)
    with patch("plugin.framework.config.get_config_int_safe", return_value=1), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.client.model_fetcher.get_grammar_model", return_value="test-model"), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.queue_executor.grammar_llm_request_gate"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_client_cls, \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put, \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text") as mock_norm, \
         patch("plugin.writer.locale.grammar_proofread_cache.ignored_rules_snapshot", return_value=set()):

        mock_client = mock_client_cls.return_value
        mock_client.chat_completion_sync.return_value = '{"errors": []}'
        mock_norm.return_value = []

        # 3 items -> should result in 3 separate LLM calls
        items = [
            GrammarWorkItem(ctx=ctx, text=f"S{i}.", grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key=f"k{i}", enqueue_seq=i)
            for i in range(3)
        ]

        run_llm_and_cache_batch(items)

        # 3 items -> 3 LLM calls
        assert mock_client.chat_completion_sync.call_count == 3
        
        # Verify first call used the single sentence prompt (not batch prompt)
        args, _ = mock_client.chat_completion_sync.call_args_list[0]
        # args[0] is messages
        # args[0][0] is the system message
        assert "Reply with a single JSON object only" in args[0][0]["content"]
        assert "results" not in args[0][0]["content"] # Batch prompt contains "results"
        assert "S0." == args[0][1]["content"] # args[0][1] is the user message

        assert mock_put.call_count == 3

def test_locale_mismatch_proceeds_and_double_caches(
) -> None:
    """Verify that locale mismatch detected during individual check triggers update and double-caches."""
    ctx = MagicMock()
    with patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_config_int_safe", return_value=1), \
         patch("plugin.writer.locale.grammar_proofread_locale.get_grammar_detect_language_mode", return_value="llm"), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_cache_put, \
         patch("plugin.writer.locale.grammar_persistence.apply_language_change", return_value=False) as mock_apply, \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_worker.grammar_obs") as mock_obs, \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_llm_client, \
         patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]):

        # Mock LLM client to return Japanese detection then grammar result
        mock_client_inst = mock_llm_client.return_value
        mock_client_inst.chat_completion_sync.side_effect = [
            '{"detected_language_bcp47": "ja-JP"}', # Detection
            '{"errors": [{"wrong": "\u65e5\u672c\u8a9e", "correct": "\u306b\u307b\u3093\u3054", "type": "grammar", "reason": "test"}]}' # Grammar
        ]
    
        # Track execution order
        call_order = []
        mock_cache_put.side_effect = lambda *args, **kwargs: call_order.append(("cache_put", args[0]))
        mock_apply.side_effect = lambda *args, **kwargs: call_order.append(("apply", args[3]))

        item = GrammarWorkItem(
            ctx=ctx,
            text="\u65e5\u672c\u8a9e\u3067\u66f8\u3044\u3066\u3044\u307e\u3059\u3002",
            grammar_bcp47="zh-CN", # Wrong locale
            partial_sentence=False,
            doc_id="doc123",
            inflight_key="key123",
            enqueue_seq=1,
        )
    
        run_llm_and_cache_batch([item])
    
        # 1. Verify document update was triggered with paragraph_text and failure was observed
        mock_apply.assert_called_once_with(ctx, "doc123", "\u65e5\u672c\u8a9e\u3067\u66f8\u3044\u3066\u3044\u307e\u3059\u3002", "ja-JP", start_pos=0, paragraph_text="")
        mock_obs.assert_any_call("apply_language_change_skip", doc_id="doc123", sentence="\u65e5\u672c\u8a9e\u3067\u66f8\u3044\u3066\u3044\u307e\u3059\u3002", bcp47="ja-JP")
    
        # 2. Verify grammar check was done with ja-JP
        args, _ = mock_client_inst.chat_completion_sync.call_args_list[1]
        messages = args[0]
        sys_prompt = messages[0]["content"]
        assert "ja-JP" in sys_prompt
        assert "Japanese" in sys_prompt
    
        # 3. Verify double caching
        assert mock_cache_put.call_count == 2
        
        # Check ja-JP cache put
        args_ja, _ = mock_cache_put.call_args_list[0]
        assert args_ja[0] == "ja-JP"
        
        # Check zh-CN cache put (the loop breaker)
        args_zh, _ = mock_cache_put.call_args_list[1]
        assert args_zh[0] == "zh-CN"

        # 4. Verify call order: caching must happen BEFORE applying language change in LibreOffice
        assert call_order == [
            ("cache_put", "ja-JP"),
            ("cache_put", "zh-CN"),
            ("apply", "ja-JP"),
        ]


def test_language_validation_does_not_trust_persisted_grammar_heuristic() -> None:
    """With detect-language on, embedded grammar must not skip the detect LLM."""
    from plugin.writer.locale.grammar_worker import _run_language_validation
    from plugin.writer.locale.grammar_persistence import grammar_registry

    item = _item("The cat sat.", doc_id="doc99")
    ec = GrammarWorkerContext(
        ctx=object(),
        client=MagicMock(),
        gq=None,
        model="m",
        original_bcp47="en-US",
        grammar_bcp47="en-US",
        max_tok=100,
    )
    mock_p = MagicMock()
    mock_p.get.return_value = []
    mock_lane = MagicMock()
    mock_lane.__enter__ = MagicMock(return_value=None)
    mock_lane.__exit__ = MagicMock(return_value=None)

    try:
        _lang_detect_cache = grammar_registry.lang_detect_cache
        _lang_detect_cache.pop(item.text, None)
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=mock_p):
            with patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.get_cached_language", return_value=None):
                with patch("plugin.framework.queue_executor.grammar_llm_request_gate", return_value=mock_lane):
                    with patch(
                        "plugin.writer.locale.grammar_worker.detect_languages_for_chunk",
                        return_value=["en-US"],
                    ) as mock_detect:
                        decision = _run_language_validation([(item, item.text)], "en-US", "", ec)
        mock_detect.assert_called_once()
        assert mock_detect.call_args.kwargs.get("trust_persisted_grammar_as_lang") is False
        assert decision is not None
        assert decision.result_chunk == [(item, item.text)]
    finally:
        _lang_detect_cache.pop(item.text, None)


def test_language_detect_skips_llm_when_persisted_grammar_exists() -> None:
    """Persisted grammar for sentence (fp) implies skip language-detect LLM (reopen heuristic)."""
    from plugin.writer.locale.grammar_persistence import grammar_registry
    from plugin.writer.locale.grammar_worker import detect_languages_for_chunk

    item = _item("The cat sat.", doc_id="doc99")
    ec = GrammarWorkerContext(
        ctx=object(),
        client=MagicMock(),
        gq=None,
        model="m",
        original_bcp47="en-US",
        grammar_bcp47="en-US",
        max_tok=100,
    )

    mock_p = MagicMock()
    mock_p.get.return_value = []
    _lang_detect_cache = grammar_registry.lang_detect_cache

    try:
        _lang_detect_cache.pop(item.text, None)
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=mock_p) as mock_get_p:
            with patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.get_cached_language", return_value=None):
                detected = detect_languages_for_chunk([(item, item.text)], "", ec)
        ec.client.chat_completion_sync.assert_not_called()
        assert detected == ["en-US"]
        mock_get_p.assert_called_once()
    finally:
        _lang_detect_cache.pop(item.text, None)


def test_language_detect_calls_llm_when_no_persisted_grammar() -> None:
    from plugin.writer.locale.grammar_persistence import grammar_registry
    from plugin.writer.locale.grammar_worker import detect_languages_for_chunk

    item = _item("Fresh sentence.", doc_id="doc100")
    mock_client = MagicMock()
    mock_client.chat_completion_sync.return_value = '{"detected_language_bcp47": "en-US"}'
    ec = GrammarWorkerContext(
        ctx=object(),
        client=mock_client,
        gq=None,
        model="m",
        original_bcp47="en-US",
        grammar_bcp47="en-US",
        max_tok=100,
    )

    mock_p = MagicMock()
    mock_p.get.return_value = None
    _lang_detect_cache = grammar_registry.lang_detect_cache

    mock_lane = MagicMock()
    mock_lane.__enter__ = MagicMock(return_value=None)
    mock_lane.__exit__ = MagicMock(return_value=None)

    try:
        _lang_detect_cache.pop(item.text, None)
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=mock_p):
            with patch("plugin.writer.locale.grammar_persistence.GrammarRegistry.get_cached_language", return_value=None):
                with patch("plugin.framework.queue_executor.grammar_llm_request_gate", return_value=mock_lane):
                    detected = detect_languages_for_chunk([(item, item.text)], "", ec)
        mock_client.chat_completion_sync.assert_called_once()
        assert detected == ["en-US"]
    finally:
        _lang_detect_cache.pop(item.text, None)


@pytest.mark.skipif(
    not _grammar_obs_call_sites_present(),
    reason="Stripped release bundle removes grammar_obs(...) call sites (scripts/strip_code.py)",
)
def test_worker_chunk_skip_empty_result_chunk_obs() -> None:
    """Multi-batch all-None detect yields empty result_chunk; worker must log worker_chunk_skip."""
    from plugin.writer.locale.grammar_worker import _worker_process_chunk

    item_a = _item("Hello one.", inflight_key="k1")
    item_b = _item("Hello two.", inflight_key="k2")
    ec = MagicMock()
    ec.ctx = MagicMock()
    ec.gq = None
    chunk = [(item_a, item_a.text), (item_b, item_b.text)]
    with patch("plugin.writer.locale.grammar_worker.grammar_obs") as mock_obs, \
         patch("plugin.writer.locale.grammar_worker._run_language_validation") as mock_val, \
         patch("plugin.writer.locale.grammar_worker.run_grammar_check") as mock_grammar:
        from plugin.writer.locale.grammar_worker import LanguageValidationDecision

        mock_val.return_value = LanguageValidationDecision(target_bcp47="en-US", result_chunk=[])
        _worker_process_chunk(chunk, ec, "en-US", True, "")
    mock_grammar.assert_not_called()
    mock_obs.assert_any_call("worker_chunk_skip", reason="empty_result_chunk", chunk_len=2, target_bcp47="en-US", requeue_count=0)


@pytest.mark.skipif(
    not _grammar_obs_call_sites_present(),
    reason="Stripped release bundle removes grammar_obs(...) call sites (scripts/strip_code.py)",
)
def test_worker_chunk_skip_lang_validation_failed_obs() -> None:
    from plugin.writer.locale.grammar_worker import _worker_process_chunk

    item = _item("Hello.")
    ec = MagicMock()
    ec.ctx = MagicMock()
    with patch("plugin.writer.locale.grammar_worker.grammar_obs") as mock_obs, \
         patch("plugin.writer.locale.grammar_worker._run_language_validation", return_value=None), \
         patch("plugin.writer.locale.grammar_worker.run_grammar_check") as mock_grammar:
        _worker_process_chunk([(item, item.text)], ec, "en-US", True, "")
    mock_grammar.assert_not_called()
    mock_obs.assert_any_call("worker_chunk_skip", reason="lang_validation_failed", chunk_len=1)


def test_grammar_empty_single_response_caches_clean() -> None:
    from plugin.writer.locale.grammar_worker import run_grammar_check

    item = _item("They is here.", seq=1)
    gq = MagicMock()
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        model="test-model",
        max_tok=512,
        gq=gq,
        detect_lang_mode="off",
        grammar_bcp47="en-US",
        original_bcp47="en-US",
    )
    chunk = [(item, item.text)]
    gq.inflight_superseded.return_value = False
    with patch("plugin.framework.config.get_grammar_provider", return_value="ai"), \
         patch("plugin.writer.locale.grammar_worker.call_grammar_llm", return_value=([[]], 50)), \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status") as mock_status, \
         patch("plugin.writer.locale.grammar_worker.requeue_individual_item") as mock_requeue, \
         patch("plugin.writer.locale.grammar_ignore_rules.doc_ignored_rules", return_value=set()), \
         patch("plugin.writer.locale.grammar_proofread_cache.ignored_rules_snapshot", return_value=set()), \
         patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put:
        run_grammar_check(chunk, "en-US", "en-US", ec)
    mock_put.assert_called_once()
    mock_requeue.assert_not_called()
    gq.enqueue.assert_not_called()
    for call in mock_status.call_args_list:
        assert call.args[0] != "failed"


def test_grammar_batch_empty_response_emits_failed_no_requeue() -> None:
    from plugin.writer.locale.grammar_worker import run_grammar_check

    a = _item("First sentence.", seq=1, inflight_key="k1")
    b = _item("Second sentence.", seq=2, inflight_key="k2")
    gq = MagicMock()
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        model="test-model",
        max_tok=512,
        gq=gq,
        detect_lang_mode="off",
        grammar_bcp47="en-US",
        original_bcp47="en-US",
    )
    chunk = [(a, a.text), (b, b.text)]
    with patch("plugin.writer.locale.grammar_worker.call_grammar_llm", return_value=([], 50)), \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status") as mock_status, \
         patch("plugin.writer.locale.grammar_worker.requeue_individual_item") as mock_requeue, \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put:
        run_grammar_check(chunk, "en-US", "en-US", ec)
    mock_status.assert_called_once_with("failed", "Grammar check", result="Empty LLM response")
    mock_requeue.assert_not_called()
    mock_put.assert_not_called()
    gq.enqueue.assert_not_called()


def test_language_mismatch_requeue_does_not_store_clean_rejected_locale() -> None:
    """A rejected CharLocale must not be cached clean, or the sentence never retags."""
    from plugin.writer.locale.grammar_worker import _run_language_validation

    kept = _item("Hello.", inflight_key="k1")
    rejected = _item("Bonjour.", inflight_key="k2")
    gq = MagicMock()
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        gq=gq,
        model="m",
        original_bcp47="en-US",
        grammar_bcp47="en-US",
        max_tok=64,
    )
    with (
        patch("plugin.writer.locale.grammar_worker.detect_languages_for_chunk", return_value=["en-US", "fr-FR"]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put,
    ):
        decision = _run_language_validation([(kept, kept.text), (rejected, rejected.text)], "en-US", "", ec)
    mock_put.assert_not_called()
    assert decision is not None
    assert len(decision.requeues) == 1
    gq.enqueue.assert_called_once()
    enqueued = gq.enqueue.call_args.args[0]
    assert enqueued.text == "Bonjour."
    assert enqueued.grammar_bcp47 == "fr-FR"
    assert enqueued.original_bcp47 == "en-US"
    assert isinstance(enqueued.enqueue_seq, int)


def test_persisted_grammar_skip_does_not_adopt_legacy() -> None:
    """A language probe must not retag a locale-blind row."""
    from plugin.writer.locale.grammar_worker import persisted_grammar_skip_lang_detect

    mock_p = MagicMock()
    mock_p.get.return_value = []
    with (
        patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=mock_p),
        patch("plugin.writer.locale.grammar_proofread_cache.sentence_identity_fp", return_value="fp"),
    ):
        assert persisted_grammar_skip_lang_detect(object(), "doc", "Hello.", "en-US") is True
    mock_p.get.assert_called_once_with("fp", "en-US", adopt_legacy=False)


def test_inplace_locale_retarget_mints_seq_and_drops_older() -> None:
    """Detected-locale key is visible to inflight_superseded. An older seq is dropped."""
    from plugin.writer.locale.grammar_proofread_locale import grammar_inflight_key
    from plugin.writer.locale.grammar_worker import LanguageValidationDecision, _worker_process_chunk

    text = "Bonjour."
    item = _item(text, seq=3, inflight_key="old-key")
    new_key = grammar_inflight_key(item.doc_id, "fr-FR", text, True)
    q = GrammarWorkQueue()
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        gq=q,
        model="m",
        original_bcp47="en-US",
        grammar_bcp47="en-US",
        max_tok=64,
    )
    decision = LanguageValidationDecision(target_bcp47="fr-FR", result_chunk=[(item, text)])
    seen: list[GrammarWorkItem] = []

    def _capture(chunk: list[tuple[GrammarWorkItem, str]], *_args: object) -> None:
        seen.append(chunk[0][0])

    with (
        patch("plugin.writer.locale.grammar_worker._run_language_validation", return_value=decision),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check", side_effect=_capture),
    ):
        _worker_process_chunk([(item, text)], ec, "en-US", True, "")
    assert len(seen) == 1
    retargeted = seen[0]
    assert retargeted.grammar_bcp47 == "fr-FR"
    assert retargeted.inflight_key == new_key
    assert q._latest_seq[new_key] == retargeted.enqueue_seq
    assert q.inflight_superseded(new_key, retargeted.enqueue_seq) is False
    assert q.note_inflight_generation(new_key, retargeted.enqueue_seq + 1) is False
    assert q.inflight_superseded(new_key, retargeted.enqueue_seq) is True

    stale = GrammarWorkQueue()
    stale._latest_seq[new_key] = 10**9
    stale_ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        gq=stale,
        model="m",
        original_bcp47="en-US",
        grammar_bcp47="en-US",
        max_tok=64,
    )
    with (
        patch("plugin.writer.locale.grammar_worker._run_language_validation", return_value=decision),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check") as mock_grammar,
    ):
        _worker_process_chunk([(item, text)], stale_ec, "en-US", True, "")
    mock_grammar.assert_not_called()
    assert stale._latest_seq[new_key] == 10**9


def test_grammar_mismatch_requeue_skips_cache_placeholder() -> None:
    from plugin.writer.locale.grammar_worker import run_grammar_check

    a = _item("First sentence.", seq=1, inflight_key="k1")
    b = _item("Second sentence.", seq=2, inflight_key="k2")
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        model="test-model",
        max_tok=512,
        gq=MagicMock(),
        detect_lang_mode="off",
        grammar_bcp47="en-US",
        original_bcp47="en-US",
    )
    chunk = [(a, a.text), (b, b.text)]
    with patch("plugin.writer.locale.grammar_worker.call_grammar_llm", return_value=([[]], 50)), \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put, \
         patch("plugin.writer.locale.grammar_worker.requeue_individual_item") as mock_requeue:
        run_grammar_check(chunk, "en-US", "en-US", ec)
    assert mock_requeue.call_count == 2
    for _args, kwargs in mock_requeue.call_args_list:
        assert kwargs.get("cache_placeholder") is False
    mock_put.assert_not_called()


def test_grammar_check_routes_to_languagetool() -> None:
    from plugin.writer.locale.grammar_worker import run_grammar_check

    a = _item("This has an error.", seq=1, inflight_key="k1")
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        model="test-model",
        max_tok=512,
        gq=MagicMock(),
        detect_lang_mode="off",
        grammar_bcp47="en-US",
        original_bcp47="en-US",
    )
    chunk = [(a, a.text)]

    with patch("plugin.framework.config.get_grammar_provider", return_value="languagetool"), \
         patch("plugin.scripting.client.run_languagetool_check") as mock_lt_check, \
         patch("plugin.writer.locale.grammar_worker.process_grammar_results") as mock_process:
        mock_lt_check.return_value = {
            "errors": [{"n_error_start": 0, "n_error_length": 4, "wrong": "This", "correct": "That"}]
        }
        run_grammar_check(chunk, "en-US", "en-US", ec)

        mock_lt_check.assert_called_once_with(ec.ctx, "This has an error.", "en-US")
        mock_process.assert_called_once()


def test_grammar_check_routes_to_vale() -> None:
    from plugin.writer.locale.grammar_worker import run_grammar_check

    a = _item("This is a passive voice sentence.", seq=1, inflight_key="k1")
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        model="test-model",
        max_tok=512,
        gq=MagicMock(),
        detect_lang_mode="off",
        grammar_bcp47="en-US",
        original_bcp47="en-US",
    )
    chunk = [(a, a.text)]

    with patch("plugin.framework.config.get_grammar_provider", return_value="vale"), \
         patch("plugin.scripting.client.run_vale_check") as mock_vale_check, \
         patch("plugin.framework.config.user_config_dir", return_value="/tmp"), \
         patch("plugin.writer.locale.grammar_worker.process_grammar_results") as mock_process:
        mock_vale_check.return_value = {
            "errors": [{"n_error_start": 0, "n_error_length": 4, "wrong": "This", "correct": "That", "suggestions": ["That"]}]
        }
        run_grammar_check(chunk, "en-US", "en-US", ec)

        mock_vale_check.assert_called_once_with(ec.ctx, "This is a passive voice sentence.", "/tmp", "Microsoft,Google,write-good")
        mock_process.assert_called_once()


def test_grammar_check_routes_to_harper() -> None:
    from plugin.writer.locale.grammar_worker import run_grammar_check

    a = _item("This is a test sentence.", seq=1, inflight_key="k1")
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        model="test-model",
        max_tok=512,
        gq=MagicMock(),
        detect_lang_mode="off",
        grammar_bcp47="en-AU",
        original_bcp47="en-AU",
    )
    chunk = [(a, a.text)]

    with patch("plugin.framework.config.get_grammar_provider", return_value="harper"), \
         patch("plugin.writer.locale.harper.run_harper_check") as mock_harper_check, \
         patch("plugin.framework.config.user_config_dir", return_value="/tmp"), \
         patch("plugin.writer.locale.grammar_worker.process_grammar_results") as mock_process:
        mock_harper_check.return_value = {
            "errors": [{"n_error_start": 0, "n_error_length": 4, "wrong": "This", "correct": "That", "suggestions": ["That"]}]
        }
        run_grammar_check(chunk, "en-AU", "en-AU", ec)

        mock_harper_check.assert_called_once_with(ec.ctx, "This is a test sentence.", "/tmp", bcp47="en-AU")
        mock_process.assert_called_once()


def test_thread_llm_client_cached_across_batches() -> None:
    from plugin.writer.locale.grammar_worker import _get_or_create_thread_llm_client

    ctx = MagicMock()
    api_cfg = {"endpoint": "https://api.example.com", "api_key": "k1", "request_timeout": 30}
    with patch("plugin.framework.client.llm_client.LlmClient") as mock_cls:
        mock_instance = MagicMock()
        mock_instance._stopped = False
        mock_cls.return_value = mock_instance

        c1 = _get_or_create_thread_llm_client(api_cfg, ctx)
        c2 = _get_or_create_thread_llm_client(api_cfg, ctx)

        assert c1 is c2
        assert mock_cls.call_count == 1
        assert mock_cls.call_args[1].get("register_with_send") is False


def test_thread_llm_client_rebuilds_on_endpoint_change() -> None:
    from plugin.writer.locale.grammar_worker import _get_or_create_thread_llm_client

    ctx = MagicMock()
    cfg1 = {"endpoint": "https://api1.example.com", "api_key": "k1", "request_timeout": 30}
    cfg2 = {"endpoint": "https://api2.example.com", "api_key": "k1", "request_timeout": 30}
    with patch("plugin.framework.client.llm_client.LlmClient") as mock_cls:
        inst1 = MagicMock()
        inst1._stopped = False
        inst2 = MagicMock()
        inst2._stopped = False
        mock_cls.side_effect = [inst1, inst2]

        c1 = _get_or_create_thread_llm_client(cfg1, ctx)
        c2 = _get_or_create_thread_llm_client(cfg2, ctx)

        assert c1 is inst1
        assert c2 is inst2
        inst1.stop.assert_called_once()
        assert mock_cls.call_count == 2


def test_thread_llm_client_separate_per_thread() -> None:
    import threading
    from plugin.writer.locale.grammar_worker import _get_or_create_thread_llm_client

    ctx = MagicMock()
    api_cfg = {"endpoint": "https://api.example.com", "api_key": "k1", "request_timeout": 30}
    results: list[Any] = []

    with patch("plugin.framework.client.llm_client.LlmClient", side_effect=lambda *args, **kwargs: MagicMock(_stopped=False)):
        def worker():
            results.append(_get_or_create_thread_llm_client(api_cfg, ctx))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert len(results) == 2
    assert results[0] is not results[1]


def test_thread_llm_client_not_reused_when_stopped() -> None:
    from plugin.writer.locale.grammar_worker import _get_or_create_thread_llm_client

    ctx = MagicMock()
    api_cfg = {"endpoint": "https://api.example.com", "api_key": "k1", "request_timeout": 30}
    with patch("plugin.framework.client.llm_client.LlmClient") as mock_cls:
        inst1 = MagicMock()
        inst1._stopped = False
        inst2 = MagicMock()
        inst2._stopped = False
        mock_cls.side_effect = [inst1, inst2]

        c1 = _get_or_create_thread_llm_client(api_cfg, ctx)
        assert c1 is inst1

        inst1._stopped = True

        c2 = _get_or_create_thread_llm_client(api_cfg, ctx)
        assert c2 is inst2
        assert mock_cls.call_count == 2


def test_worker_process_chunk_fails_open_when_language_detection_raises() -> None:
    from plugin.writer.locale.grammar_worker import _worker_process_chunk

    item = _item("Hello world.")
    ec = _ec(detect_lang_mode="langdetect")
    with (
        patch("plugin.writer.locale.grammar_worker.detect_languages_for_chunk", side_effect=RuntimeError("Venv missing")),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check") as mock_grammar,
    ):
        _worker_process_chunk([(item, item.text)], ec, "en-US", True, "")
    mock_grammar.assert_called_once()
    assert mock_grammar.call_args[0][0] == [(item, item.text)]
    assert mock_grammar.call_args[0][1] == "en-US"


def test_process_grammar_results_caches_ignored_rules_and_excludes_from_status() -> None:
    from plugin.writer.locale.grammar_worker import process_grammar_results

    item = _item("They is here.", doc_id="doc-1")
    ec = _ec()
    ec.gq = None
    errors = [{"wrong": "They is", "correct": "They are", "type": "grammar", "reason": "agr", "rule_identifier": "wa_g_rule||agr"}]
    norm_err = NormalizedProofError(
        n_error_start=0,
        n_error_length=7,
        suggestions=("They are",),
        short_comment="agr",
        full_comment="agr",
        rule_identifier="wa_g_rule||agr",
    )

    with (
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[norm_err]),
        patch("plugin.writer.locale.grammar_ignore_rules.doc_ignored_rules", return_value={"agr"}),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put,
        patch("plugin.writer.locale.grammar_worker.emit_done_status") as mock_done,
    ):
        process_grammar_results([(item, item.text)], [errors], "en-US", "en-GB", 100, ec)

        # Both cache puts (detected and original locale) receive the raw provider error
        assert mock_put.call_count == 2
        payload1 = mock_put.call_args_list[0][0][2]
        payload2 = mock_put.call_args_list[1][0][2]
        assert len(payload1) == 1
        assert payload1[0]["rule_identifier"] == "wa_g_rule||agr"
        assert len(payload2) == 1
        assert payload2[0]["rule_identifier"] == "wa_g_rule||agr"

        # Status text summary excludes the ignored rule (0 issues)
        mock_done.assert_called_once()
        assert "0 issues" in mock_done.call_args[1]["result"]
def test_call_grammar_llm_and_cache_cap_prefix() -> None:
    """Keeper 1: LLM boundaries prompt and cache checked prefix while apply_language_change gets full text."""
    from plugin.writer.locale.grammar_worker import run_llm_and_cache_batch

    full_text = "word1 word2 word3 word4 word5."
    prefix = "word1 word2 word3"

    ctx = MagicMock()
    item = _item(
        full_text,
        seq=1,
        inflight_key="k1",
        grammar_bcp47="en-US",
        original_bcp47="fr-FR",
        provider="llm",
        checker_identity="llm:model-x",
    )
    item = replace(item, ctx=ctx)

    with (
        patch("plugin.framework.config.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.get_config_int_safe", return_value=1),
        patch("plugin.writer.locale.grammar_proofread_locale.grammar_max_chars", return_value=20),
        patch("plugin.writer.locale.grammar_proofread_locale.get_grammar_detect_language_mode", return_value="off"),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_cache_put,
        patch("plugin.writer.locale.grammar_persistence.apply_language_change", return_value=True) as mock_apply,
        patch("plugin.writer.locale.grammar_worker.emit_grammar_status"),
        patch("plugin.writer.locale.grammar_worker.grammar_obs"),
        patch("plugin.framework.client.llm_client.LlmClient") as mock_llm_client,
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]),
    ):
        mock_client = mock_llm_client.return_value
        mock_client.chat_completion_sync.return_value = '{"errors": []}'

        run_llm_and_cache_batch([item])

        args, _ = mock_client.chat_completion_sync.call_args
        assert args[0][1]["content"] == prefix

        assert any(call.args[1] == prefix for call in mock_cache_put.call_args_list)
        assert not any(call.args[1] == full_text for call in mock_cache_put.call_args_list)

        mock_apply.assert_called_once()
        assert mock_apply.call_args[0][2] == full_text


def test_worker_runs_model_from_checker_identity() -> None:
    """Keeper 2: LLM group sets model from checker_identity and writes cache under that identity."""
    from plugin.writer.locale.grammar_worker import run_llm_and_cache_batch

    ctx = MagicMock()
    item = _item(
        "Hello world.",
        provider="llm",
        checker_identity="llm:model-a",
    )
    item = replace(item, ctx=ctx)

    with (
        patch("plugin.framework.config.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.get_config_int_safe", return_value=1),
        patch("plugin.writer.locale.grammar_proofread_locale.get_grammar_detect_language_mode", return_value="off"),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_cache_put,
        patch("plugin.writer.locale.grammar_worker.emit_grammar_status"),
        patch("plugin.writer.locale.grammar_worker.grammar_obs"),
        patch("plugin.framework.client.model_fetcher.get_grammar_model", return_value="model-b"),
        patch("plugin.framework.client.llm_client.LlmClient") as mock_llm_client,
        patch("plugin.writer.locale.grammar_proofread_text.normalize_errors_for_text", return_value=[]),
    ):
        mock_client = mock_llm_client.return_value
        mock_client.chat_completion_sync.return_value = '{"errors": []}'

        run_llm_and_cache_batch([item])

        args, kwargs = mock_client.chat_completion_sync.call_args
        assert kwargs.get("model") == "model-a"

        mock_cache_put.assert_called_once()
        assert mock_cache_put.call_args.kwargs.get("checker_identity") == "llm:model-a"


def test_worker_batch_gates_provider_rules() -> None:
    """Keeper 2: Queued LanguageTool still runs after live provider is off; empty-provider batch does not."""
    from plugin.writer.locale.grammar_worker import _worker_batch_gates

    item_lt = _item("Hello.", provider="languagetool")
    item_empty = _item("Hello.", provider="")

    with patch("plugin.framework.config.is_grammar_enabled", return_value=False):
        assert _worker_batch_gates(None, [item_lt]) is True
        assert _worker_batch_gates(None, [item_empty]) is False


def test_worker_detection_superseded_drops_item() -> None:
    """Keeper 4: Detection enqueues a newer generation for the same key before returning; grammar runner not called."""
    from plugin.writer.locale.grammar_worker import run_llm_and_cache_batch
    from plugin.writer.locale.grammar_work_queue import GrammarWorkQueue

    gq = GrammarWorkQueue()
    item = _item("Hello world.", doc_id="d1", seq=1, inflight_key="k1", grammar_bcp47="en-US")
    gq.note_inflight_generation("k1", 1)

    def _detect_side_effect(*args, **kwargs):
        gq.note_inflight_generation("k1", 2)
        return ["en-US"]

    with (
        patch("plugin.framework.config.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.get_config_int_safe", return_value=1),
        patch("plugin.writer.locale.grammar_proofread_locale.get_grammar_detect_language_mode", return_value="llm"),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None),
        patch("plugin.writer.locale.grammar_worker.detect_languages_for_chunk", side_effect=_detect_side_effect),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check") as mock_run_grammar,
        patch("plugin.writer.locale.grammar_worker.emit_grammar_status"),
        patch("plugin.writer.locale.grammar_worker.grammar_obs"),
        patch("plugin.framework.client.llm_client.LlmClient"),
    ):
        run_llm_and_cache_batch([item], grammar_queue_instance=gq)

    mock_run_grammar.assert_not_called()


def test_worker_detection_cache_hit_applies_locale_without_grammar_call() -> None:
    """Keeper 4: Detection returns fr-FR, fr-FR cache is primed, grammar runner not called, apply_language_change called with fr-FR."""
    from plugin.writer.locale.grammar_worker import run_llm_and_cache_batch

    ctx = MagicMock()
    item = _item("Bonjour le monde.", doc_id="d1", seq=1, inflight_key="k1", grammar_bcp47="en-US")
    item = replace(item, ctx=ctx)

    def _cache_get(locale, text, **kwargs):
        if locale == "fr-FR":
            return []
        return None

    with (
        patch("plugin.framework.config.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.get_config_int_safe", return_value=1),
        patch("plugin.writer.locale.grammar_proofread_locale.get_grammar_detect_language_mode", return_value="llm"),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", side_effect=_cache_get),
        patch("plugin.writer.locale.grammar_persistence.apply_language_change", return_value=True) as mock_apply,
        patch("plugin.writer.locale.grammar_worker.detect_languages_for_chunk", return_value=["fr-FR"]),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check") as mock_run_grammar,
        patch("plugin.writer.locale.grammar_worker.emit_grammar_status"),
        patch("plugin.writer.locale.grammar_worker.grammar_obs"),
        patch("plugin.framework.client.llm_client.LlmClient"),
    ):
        run_llm_and_cache_batch([item])

    mock_run_grammar.assert_not_called()
    mock_apply.assert_called_once_with(ctx, "d1", item.text, "fr-FR", start_pos=0, paragraph_text="")


def test_multi_item_requeue_does_not_cache_clean_row_for_rejected_locale() -> None:
    """Keeper 4: Multi-item mismatch requeues individual item and does not store a clean row for rejected locale."""
    from plugin.writer.locale.grammar_worker import _run_language_validation, GrammarWorkerContext

    a = _item("Bonjour le monde.", seq=1, inflight_key="k1", grammar_bcp47="en-US")
    b = _item("Hello world.", seq=2, inflight_key="k2", grammar_bcp47="en-US")
    ec = GrammarWorkerContext(
        ctx=MagicMock(),
        client=MagicMock(),
        gq=MagicMock(),
        model="m",
        original_bcp47="en-US",
        grammar_bcp47="en-US",
        max_tok=512,
    )

    with (
        patch("plugin.writer.locale.grammar_worker.detect_languages_for_chunk", return_value=["fr-FR", "en-US"]),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_put,
        patch("plugin.writer.locale.grammar_worker.requeue_individual_item") as mock_requeue,
    ):
        decision = _run_language_validation([(a, a.text), (b, b.text)], "en-US", "", ec)

    assert decision is not None
    assert len(decision.requeues) == 1
    assert decision.requeues[0].new_bcp47 == "fr-FR"
    mock_put.assert_not_called()
    mock_requeue.assert_called_once()
    assert mock_requeue.call_args.kwargs.get("cache_placeholder") is False


def test_detect_once_per_group_eight_items_batch_1() -> None:
    """Keeper 5: Eight complete items, detection mode llm, batch size 1, exactly one detect call then grammar calls."""
    from plugin.writer.locale.grammar_worker import run_llm_and_cache_batch

    items = [_item(f"Sentence number {i}.", seq=i, inflight_key=f"k{i}", grammar_bcp47="en-US") for i in range(1, 9)]

    detect_call_count = 0
    grammar_call_count = 0

    def _mock_detect(chunk, instr, ec, **kwargs):
        nonlocal detect_call_count
        detect_call_count += 1
        res = ["en-US"] * len(chunk)
        if len(chunk) == 8:
            res[7] = "fr-FR"
        return res

    def _mock_grammar_check(chunk, current_bcp47, orig_bcp47, ec):
        nonlocal grammar_call_count
        grammar_call_count += 1

    with (
        patch("plugin.framework.config.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.get_config_int_safe", return_value=1),
        patch("plugin.writer.locale.grammar_proofread_locale.get_grammar_detect_language_mode", return_value="llm"),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None),
        patch("plugin.writer.locale.grammar_worker.detect_languages_for_chunk", side_effect=_mock_detect),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check", side_effect=_mock_grammar_check),
        patch("plugin.writer.locale.grammar_worker.emit_grammar_status"),
        patch("plugin.writer.locale.grammar_worker.grammar_obs"),
        patch("plugin.framework.client.llm_client.LlmClient"),
    ):
        run_llm_and_cache_batch(items)

    assert detect_call_count == 1
    assert grammar_call_count == 8


def test_detect_batch_parse_failure_affects_only_that_chunk() -> None:
    """Keeper 5: A detect-batch parse failure affects only that detect chunk."""
    from plugin.writer.locale.grammar_worker import run_llm_and_cache_batch

    items = [_item(f"Sentence {i}.", seq=i, inflight_key=f"k{i}", grammar_bcp47="en-US") for i in range(1, 10)]

    chunks_seen = []

    def _mock_detect(chunk, instr, ec, **kwargs):
        chunks_seen.append(len(chunk))
        if len(chunk) == 8:
            return [None] * len(chunk)
        return ["en-US"]

    grammar_locales = []

    def _mock_grammar_check(chunk, current_bcp47, orig_bcp47, ec):
        grammar_locales.append((len(chunk), current_bcp47))

    with (
        patch("plugin.framework.config.is_grammar_enabled", return_value=True),
        patch("plugin.framework.config.get_config_int_safe", return_value=1),
        patch("plugin.writer.locale.grammar_proofread_locale.get_grammar_detect_language_mode", return_value="llm"),
        patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None),
        patch("plugin.writer.locale.grammar_worker.detect_languages_for_chunk", side_effect=_mock_detect),
        patch("plugin.writer.locale.grammar_worker.run_grammar_check", side_effect=_mock_grammar_check),
        patch("plugin.writer.locale.grammar_worker.emit_grammar_status"),
        patch("plugin.writer.locale.grammar_worker.grammar_obs"),
        patch("plugin.framework.client.llm_client.LlmClient"),
    ):
        run_llm_and_cache_batch(items)

    assert chunks_seen == [8, 1]
    assert len(grammar_locales) == 9
    assert all(loc == "en-US" for _, loc in grammar_locales)


def test_lang_detect_cache_normalizes_trailing_whitespace() -> None:
    """Keeper 5: 'Hello.' and 'Hello. ' share a lang-detect entry; 'Hello' and 'Hello.' do not."""
    from plugin.writer.locale.grammar_persistence import grammar_registry

    grammar_registry.lang_detect_cache.clear()
    grammar_registry.put_cached_language("Hello.", "en-US")

    assert grammar_registry.get_cached_language("Hello. ") == "en-US"
    assert grammar_registry.get_cached_language("Hello.\n\t ") == "en-US"

    assert grammar_registry.get_cached_language("Hello") is None


