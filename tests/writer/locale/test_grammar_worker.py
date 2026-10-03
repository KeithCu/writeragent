# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grammar worker LLM orchestration."""

from __future__ import annotations

import pytest
from unittest.mock import ANY, MagicMock, patch

from plugin.writer.locale.grammar_worker import (
    GrammarWorkerContext,
    LangRequeueAction,
    _worker_build_chunks,
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
from plugin.writer.locale.grammar_work_queue import GrammarWorkItem
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
    )


def _ec(client: MagicMock | None = None, *, detect_lang_mode: str = "llm") -> MagicMock:
    ec = MagicMock()
    ec.ctx = MagicMock()
    ec.client = client or MagicMock()
    ec.model = "test-model"
    ec.max_tok = 512
    ec.detect_lang_mode = detect_lang_mode
    ec.grammar_bcp47 = "en-US"
    return ec


def test_worker_build_chunks_truncates_batch_and_single() -> None:
    long_a = "A" * 100
    long_b = "B" * 80
    batch_items = [
        (_item(long_a, seq=1, inflight_key="a"), long_a),
        (_item(long_b, seq=2, inflight_key="b"), long_b),
    ]
    chunks, _instr = _worker_build_chunks(
        batch_items, MagicMock(), batch_size=8, max_chars=10, detect_lang_enabled=False
    )
    assert len(chunks) == 1 and len(chunks[0]) == 2
    assert chunks[0][0][1] == "A" * 10
    assert chunks[0][1][1] == "B" * 10

    single_chunks, _instr = _worker_build_chunks(
        [(_item(long_a), long_a)], MagicMock(), batch_size=8, max_chars=10, detect_lang_enabled=False
    )
    assert len(single_chunks) == 1
    assert single_chunks[0][0][1] == "A" * 10

    short = "Hi."
    short_chunks, _instr = _worker_build_chunks(
        [(_item(short), short), (_item("Bye.", inflight_key="k2"), "Bye.")],
        MagicMock(),
        batch_size=8,
        max_chars=50,
        detect_lang_enabled=False,
    )
    assert short_chunks[0][0][1] == "Hi."
    assert short_chunks[0][1][1] == "Bye."


def test_worker_build_chunks_detect_prefilter_before_truncate() -> None:
    long_partial = "x" * 100
    items = [(_item(long_partial, partial_sentence=True), long_partial)]
    chunks, instr = _worker_build_chunks(
        items, MagicMock(), batch_size=8, max_chars=10, detect_lang_enabled=True
    )
    assert chunks == []
    assert instr == ""


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
    args, _ = client.chat_completion_sync.call_args
    assert "1. A.\n2. B." in args[0][1]["content"]


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
         patch("plugin.framework.client.langdetect_service.detect_languages", return_value=["fr-FR"]), \
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


def test_decide_language_validation_ja_tag_matches_ja_jp() -> None:
    """LLM ``ja`` and document ``ja-JP`` must not trigger a locale change."""
    item = _phase_item()
    decision = decide_language_validation([(item, item.text)], "ja-JP", ["ja"])
    assert decision.target_bcp47 == "ja-JP"
    assert decision.result_chunk == [(item, item.text)]
    assert decision.requeues == ()


def test_decide_language_validation_single_mismatch_updates_target() -> None:
    item = _phase_item()
    decision = decide_language_validation([(item, item.text)], "en-US", ["fr-FR"])
    assert decision.target_bcp47 == "fr-FR"
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


def test_decide_language_validation_multi_none_drops_from_result_chunk() -> None:
    """Multi-batch None detect drops items silently (observe-only; single-item falls back to target)."""
    a, b = _phase_item("k1"), _phase_item("k2")
    decision = decide_language_validation([(a, a.text), (b, b.text)], "en-US", [None, None])
    assert decision.result_chunk == []
    assert decision.requeues == ()
    assert decision.target_bcp47 == "en-US"


def test_decide_grammar_completion_mismatch_requeues_all() -> None:
    decision = decide_grammar_completion(3, 2, "en-US", "en-US")
    assert decision.requeue_all is True
    assert decision.apply_locale_after_success is False


def test_decide_grammar_completion_success_with_locale_change() -> None:
    decision = decide_grammar_completion(1, 1, "ja-JP", "zh-CN")
    assert decision.requeue_all is False
    assert decision.apply_locale_after_success is True


def test_decide_grammar_completion_no_apply_when_tags_equivalent() -> None:
    decision = decide_grammar_completion(1, 1, "ja-JP", "ja")
    assert decision.apply_locale_after_success is False


def test_decide_grammar_completion_success_same_locale() -> None:
    decision = decide_grammar_completion(2, 2, "en-US", "en-US")
    assert decision.requeue_all is False
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
         patch("plugin.writer.locale.grammar_persistence.apply_language_change") as mock_apply, \
         patch("plugin.writer.locale.grammar_worker.emit_grammar_status"), \
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
    
        # 1. Verify document update was triggered
        mock_apply.assert_called_once_with(ctx, "doc123", "\u65e5\u672c\u8a9e\u3067\u66f8\u3044\u3066\u3044\u307e\u3059\u3002", "ja-JP")
    
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





