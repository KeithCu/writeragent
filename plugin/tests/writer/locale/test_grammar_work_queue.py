# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grammar_work_queue.py."""

from __future__ import annotations

from plugin.writer.locale.grammar_work_queue import (
    GrammarWorkQueue,
    GrammarWorkItem,
    deduplicate_grammar_batch,
    inflight_superseded,
    is_stale,
    record_enqueue_latest,
    run_llm_and_cache_batch,
    tail_enqueue_operation,
)
from plugin.writer.locale.grammar_proofread_text import NormalizedProofError
from unittest.mock import MagicMock, patch, ANY
from dataclasses import asdict


def _item(seq: int, key: str = "d|en-US|k1") -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=None,
        full_text="x",
        n_start=0,
        n_end=1,
        grammar_bcp47="en-US",
        partial_sentence=False,
        doc_id="d",
        inflight_key=key,
        enqueue_seq=seq,
    )


def _make_item(
    text: str,
    *,
    doc_id: str = "doc1",
    locale: str = "en-US",
    seq: int = 1,
    inflight_key: str = "",
) -> GrammarWorkItem:
    """Helper to build a work item with sensible defaults."""
    if not inflight_key:
        inflight_key = f"{doc_id}|{locale}|k1"
    return GrammarWorkItem(
        ctx=None,
        full_text=text,
        n_start=0,
        n_end=len(text),
        grammar_bcp47=locale,
        partial_sentence=False,
        doc_id=doc_id,
        inflight_key=inflight_key,
        enqueue_seq=seq,
    )


def test_mid_sentence_typing_dedup() -> None:
    """Incomplete sentences share a stable key and should supersede."""
    from plugin.writer.locale.grammar_proofread_text import grammar_inflight_key
    # All incomplete sentences in a doc share the same key
    key = grammar_inflight_key("doc1", "en-US", "H", is_complete=False)
    assert key == "doc1|en-US|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    assert key == grammar_inflight_key("doc1", "en-US", "Hello world", is_complete=False)
    
    items = [
        _make_item("Hello", seq=1, inflight_key=key),
        _make_item("Hello world", seq=2, inflight_key=key),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    assert result[0].enqueue_seq == 2


def test_prefix_dedup_typing_sequence() -> None:
    """Typing 'This is' -> 'This is a' -> 'This is a story.' keeps only newest."""
    items = [
        _make_item("This is", seq=1),
        _make_item("This is a", seq=2),
        _make_item("This is a story.", seq=3),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    surviving_text = result[0].full_text[result[0].n_start : result[0].n_end]
    assert surviving_text == "This is a story."


def test_prefix_dedup_different_paragraphs() -> None:
    """Two different paragraphs (non-prefix) should both survive."""
    items = [
        _make_item("Hello world.", seq=1, doc_id="para_a"),
        _make_item("Goodbye world.", seq=2, doc_id="para_b"),
    ]
    result = deduplicate_grammar_batch(items)
    texts = {r.full_text[r.n_start : r.n_end] for r in result}
    assert texts == {"Hello world.", "Goodbye world."}


def test_supersede_same_key() -> None:
    """Same inflight_key with different sequences -> only highest seq survives."""
    key = "doc1|en-US|k1"
    items = [
        _make_item("Same text.", seq=1, inflight_key=key),
        _make_item("Same text.", seq=3, inflight_key=key),
        _make_item("Same text.", seq=2, inflight_key=key),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    assert result[0].enqueue_seq == 3


def test_mixed_dedup() -> None:
    """Combination of prefix dedup + supersede in one batch."""
    key = "doc_short|en-US|k1"
    items = [
        # Two versions of the same key (supersede: keep seq=5)
        _make_item("Short.", seq=3, doc_id="doc_short", inflight_key=key),
        _make_item("Short.", seq=5, doc_id="doc_short", inflight_key=key),
        # A prefix chain (prefix dedup: keep newest)
        _make_item("The cat", seq=6, doc_id="doc_cat"),
        _make_item("The cat sat on the mat.", seq=7, doc_id="doc_cat"),
        # Unrelated paragraph
        _make_item("Unrelated paragraph.", seq=8, doc_id="doc_other"),
    ]
    result = deduplicate_grammar_batch(items)
    texts = {r.full_text[r.n_start : r.n_end] for r in result}
    # "Short." survives (seq=5), "The cat" dropped (older prefix-related),
    # "The cat sat on the mat." survives (newer), "Unrelated paragraph." survives
    # (distinct doc_id so inflight_key does not collapse unrelated paragraphs).
    assert "Short." in texts
    assert "The cat sat on the mat." in texts
    assert "Unrelated paragraph." in texts
    assert "The cat" not in texts
    assert len(texts) == 3


def test_different_locales_not_deduped() -> None:
    """Same text in different locales should NOT be deduped (different groups)."""
    items = [
        _make_item("Bonjour le monde.", locale="fr-FR", seq=1),
        _make_item("Bonjour le monde.", locale="en-US", seq=2),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 2
    locales = {r.grammar_bcp47 for r in result}
    assert locales == {"fr-FR", "en-US"}


def test_newest_wins_over_longest_for_prefix_related_items() -> None:
    """A newer shorter prefix-related item must survive over older longer text."""
    items = [
        _make_item("What is going on", seq=10),
        _make_item("What is going", seq=11),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    item = result[0]
    assert item.enqueue_seq == 11
    assert item.full_text[item.n_start : item.n_end] == "What is going"


def test_reverse_prefix_chain_executes_only_latest() -> None:
    """Reverse chain reproducer: only newest item survives."""
    items = [
        _make_item("What is going on", seq=1),
        _make_item("What is going o", seq=2),
        _make_item("What is going", seq=3),
        _make_item("What is goin", seq=4),
        _make_item("What is goi", seq=5),
        _make_item("What is go", seq=6),
        _make_item("What is g", seq=7),
        _make_item("What is ", seq=8),
        _make_item("What is", seq=9),
        _make_item("W", seq=10),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 1
    item = result[0]
    assert item.enqueue_seq == 10
    assert item.full_text[item.n_start : item.n_end] == "W"


def test_two_sentences_same_document_distinct_inflight_keys_survive() -> None:
    """Different sentences should have different keys (based on their text) and both remain."""
    from plugin.writer.locale.grammar_proofread_text import grammar_inflight_key
    s1 = "First sentence."
    s2 = "Second sentence."
    key1 = grammar_inflight_key("doc1", "en-US", s1, is_complete=True)
    key2 = grammar_inflight_key("doc1", "en-US", s2, is_complete=True)
    
    assert key1 != key2
    
    items = [
        _make_item(s1, seq=1, inflight_key=key1),
        _make_item(s2, seq=2, inflight_key=key2),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 2


def test_paragraph_collision_survives_dedup() -> None:
    """Two different complete sentences with same relative start (handled by text-based keys) survive."""
    from plugin.writer.locale.grammar_proofread_text import grammar_inflight_key
    
    s1 = "Paragraph one is unique."
    s2 = "Paragraph two is also unique."
    
    key1 = grammar_inflight_key("doc1", "en-US", s1, is_complete=True)
    key2 = grammar_inflight_key("doc1", "en-US", s2, is_complete=True)
    
    assert key1 != key2
    
    items = [
        _make_item(s1, seq=1, inflight_key=key1),
        _make_item(s2, seq=2, inflight_key=key2),
    ]
    
    result = deduplicate_grammar_batch(items)
    assert len(result) == 2


def test_two_sentences_string_prefix_collision_both_survive() -> None:
    """Regression: ``deduplicate_grammar_batch`` must not apply text-prefix rules across *different* ``inflight_key`` values.

    Historical bug: grouping by (doc, locale) and dropping prefix-related slices removed
    the first sentence when the second sentence's text extended the first (e.g. ``No.``
    vs ``No problem today.``). Fix: dedup by ``inflight_key`` only (see comments above
    ``deduplicate_grammar_batch`` in ``grammar_work_queue.py``).
    """
    items = [
        GrammarWorkItem(
            ctx=None,
            full_text="No. No problem today.",
            n_start=0,
            n_end=3,
            grammar_bcp47="en-US",
            partial_sentence=False,
            doc_id="doc1",
            inflight_key="doc1|en-US|0",
            enqueue_seq=1,
        ),
        GrammarWorkItem(
            ctx=None,
            full_text="No. No problem today.",
            n_start=4,
            n_end=len("No. No problem today."),
            grammar_bcp47="en-US",
            partial_sentence=False,
            doc_id="doc1",
            inflight_key="doc1|en-US|4",
            enqueue_seq=2,
        ),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 2



def test_record_enqueue_latest_updates_map() -> None:
    d, out_of_order, prev_bad = record_enqueue_latest({}, _item(1))
    assert d["d|en-US|k1"] == 1
    assert out_of_order is False
    assert prev_bad is None


def test_record_enqueue_latest_detects_out_of_order() -> None:
    d = {"d|en-US|k1": 10}
    d2, out_of_order, prev_bad = record_enqueue_latest(d, _item(5))
    assert out_of_order is True
    assert prev_bad == 10
    assert d2["d|en-US|k1"] == 5


def test_tail_enqueue_operation() -> None:
    a = _item(1)
    b = _item(2)
    c = _item(1, key="other")
    assert tail_enqueue_operation(None, a) == "append"
    assert tail_enqueue_operation(a, b) == "replace_tail"
    assert tail_enqueue_operation(b, a) == "skip_tail"
    assert tail_enqueue_operation(a, c) == "append"


def test_drain_loop_collapses_same_key_items_during_burst() -> None:
    """Regression: during typing bursts the worker pulls items between keystrokes,
    so enqueue's tail-replace path cannot help — the queue is empty between
    each keystroke. The drain loop's accumulator must collapse same-key items
    as they arrive so the worker's batch holds only one item per inflight_key.
    """
    import threading

    q = GrammarWorkQueue()
    incomplete_key = "doc1|en-US|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    complete_a = "doc1|en-US|complete-a"
    complete_b = "doc1|en-US|complete-b"

    items = [
        _item(1, key=incomplete_key),
        _item(2, key=complete_a),
        _item(3, key=incomplete_key),
        _item(4, key=complete_b),
        _item(5, key=incomplete_key),
    ]

    drained: list[list[GrammarWorkItem]] = []
    drain_done = threading.Event()

    def fake_run(group_items, *, grammar_queue=None):
        drained.append(list(group_items))
        q._q.put(None)
        drain_done.set()

    for it in items:
        q._q.put(it)

    with patch("plugin.writer.locale.grammar_work_queue.run_llm_and_cache_batch", side_effect=fake_run), \
         patch("plugin.writer.locale.grammar_work_queue.GRAMMAR_WORKER_PAUSE_TIMEOUT_S", 0.01):
        q._ensure_worker()
        assert drain_done.wait(timeout=2.0), "drain loop did not run"

    assert len(drained) == 1
    survivors = drained[0]
    assert {item.inflight_key for item in survivors} == {incomplete_key, complete_a, complete_b}
    by_key = {item.inflight_key: item for item in survivors}
    assert by_key[incomplete_key].enqueue_seq == 5
    assert by_key[complete_a].enqueue_seq == 2
    assert by_key[complete_b].enqueue_seq == 4


def test_is_stale_and_inflight_superseded() -> None:
    latest = {"k": 9}
    old = _item(7, key="k")
    assert is_stale(latest, old) is True
    assert inflight_superseded(latest, "k", 7) is True
    cur = _item(9, key="k")
    assert is_stale(latest, cur) is False


def test_run_llm_and_cache_batch_success() -> None:
    """Verify that multiple items are batched and results are stored in cache."""
    ctx = MagicMock()
    # Mock config to enable checker
    with patch("plugin.framework.config.get_config_int_safe", return_value=4), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_grammar_model", return_value="test-model"), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.queue_executor.llm_request_lane"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_client_cls, \
         patch("plugin.writer.locale.grammar_work_queue.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_work_queue.cache_put_sentence") as mock_put, \
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_work_queue.normalize_errors_for_text") as mock_norm, \
         patch("plugin.writer.locale.grammar_work_queue.ignored_rules_snapshot", return_value=set()):

        mock_client = mock_client_cls.return_value
        # Mock LLM response with 2 results
        mock_client.chat_completion_sync.return_value = '{"results": [{"errors": [{"wrong": "is", "correct": "are"}]}, {"errors": []}]}'

        # Mock normalization to return a dummy error for the first sentence
        dummy_error = NormalizedProofError(n_error_start=5, n_error_length=2, suggestions=("are",), short_comment="grammar", full_comment="grammar", rule_identifier="wa_grammar_0_0f61208a")
        mock_norm.side_effect = [[dummy_error], []]

        items = [
            GrammarWorkItem(ctx=ctx, full_text="They is here.", n_start=0, n_end=13, grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key="k1", enqueue_seq=1, proofread_sentence_text="They is here."),
            GrammarWorkItem(ctx=ctx, full_text="All good.", n_start=14, n_end=23, grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key="k2", enqueue_seq=2, proofread_sentence_text="All good."),
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


def test_run_llm_and_cache_batch_mismatch_fallback() -> None:
    """Verify fallback to individual processing if LLM returns wrong number of results."""
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config_int_safe", return_value=4), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_grammar_model", return_value="test-model"), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.queue_executor.llm_request_lane"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_client_cls, \
         patch("plugin.writer.locale.grammar_work_queue.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_work_queue.cache_put_sentence"), \
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_work_queue.run_llm_and_cache") as mock_single_run:

        mock_client = mock_client_cls.return_value
        # Mock LLM response with only 1 result instead of 2
        mock_client.chat_completion_sync.return_value = '{"results": [{"errors": []}]}'

        items = [
            GrammarWorkItem(ctx=ctx, full_text="S1.", n_start=0, n_end=3, grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key="k1", enqueue_seq=1, proofread_sentence_text="S1."),
            GrammarWorkItem(ctx=ctx, full_text="S2.", n_start=4, n_end=7, grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key="k2", enqueue_seq=2, proofread_sentence_text="S2."),
        ]

        run_llm_and_cache_batch(items)

        # Should have fallen back to run_llm_and_cache for each
        assert mock_single_run.call_count == 2


def test_run_llm_and_cache_batch_chunking() -> None:
    """Verify that large batches are split into smaller chunks."""
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config_int_safe", return_value=2), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_grammar_model", return_value="test-model"), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.queue_executor.llm_request_lane"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_client_cls, \
         patch("plugin.writer.locale.grammar_work_queue.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_work_queue.cache_put_sentence") as mock_put, \
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_work_queue.normalize_errors_for_text") as mock_norm, \
         patch("plugin.writer.locale.grammar_work_queue.ignored_rules_snapshot", return_value=set()):

        mock_client = mock_client_cls.return_value
        # Mock LLM response with results count that matches chunks
        # Chunk 1 (2 items), Chunk 2 (2 items), Chunk 3 (1 item)
        mock_client.chat_completion_sync.side_effect = [
            '{"results": [{"errors": []}, {"errors": []}]}',
            '{"results": [{"errors": []}, {"errors": []}]}',
            '{"results": [{"errors": []}]}',
        ]
        mock_norm.return_value = []

        # 5 items, batch size 2 -> 3 chunks (2, 2, 1)
        items = [
            GrammarWorkItem(ctx=ctx, full_text=f"Sent {i}.", n_start=0, n_end=10, grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key=f"k{i}", enqueue_seq=i, proofread_sentence_text=f"Sent {i}.")
            for i in range(5)
        ]

        run_llm_and_cache_batch(items)

        # 3 chunks -> 3 LLM calls
        assert mock_client.chat_completion_sync.call_count == 3
        
        # Verify first call had 2 sentences
        args, _ = mock_client.chat_completion_sync.call_args_list[0]
        assert "1. Sent 0.\n2. Sent 1." in args[0][1]["content"]

        # Verify third call had 1 sentence
        args, _ = mock_client.chat_completion_sync.call_args_list[2]
        assert "1. Sent 4." in args[0][1]["content"]

        assert mock_put.call_count == 5


def test_run_llm_and_cache_batch_size_1() -> None:
    """Verify that multiple items are processed individually when batch_size is 1."""
    ctx = MagicMock()
    # Mock batch_size to 1 (the default)
    with patch("plugin.framework.config.get_config_int_safe", return_value=1), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_grammar_model", return_value="test-model"), \
         patch("plugin.framework.config.get_api_config", return_value={}), \
         patch("plugin.framework.queue_executor.llm_request_lane"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_client_cls, \
         patch("plugin.writer.locale.grammar_work_queue.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_work_queue.cache_put_sentence") as mock_put, \
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
         patch("plugin.writer.locale.grammar_work_queue.normalize_errors_for_text") as mock_norm, \
         patch("plugin.writer.locale.grammar_work_queue.ignored_rules_snapshot", return_value=set()):

        mock_client = mock_client_cls.return_value
        mock_client.chat_completion_sync.return_value = '{"errors": []}'
        mock_norm.return_value = []

        # 3 items -> should result in 3 separate LLM calls
        items = [
            GrammarWorkItem(ctx=ctx, full_text=f"S{i}.", n_start=0, n_end=3, grammar_bcp47="en-US", partial_sentence=False, doc_id="d1", inflight_key=f"k{i}", enqueue_seq=i, proofread_sentence_text=f"S{i}.")
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
         patch("plugin.framework.config.get_config_bool_safe", side_effect=lambda c, key, default=False: True if "detect_language" in key else False), \
         patch("plugin.writer.locale.grammar_work_queue.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_work_queue.cache_put_sentence") as mock_cache_put, \
         patch("plugin.writer.locale.grammar_work_queue._apply_language_change") as mock_apply, \
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_llm_client, \
         patch("plugin.writer.locale.grammar_work_queue.normalize_errors_for_text", return_value=[]):

        # Mock LLM client to return Japanese detection then grammar result
        mock_client_inst = mock_llm_client.return_value
        mock_client_inst.chat_completion_sync.side_effect = [
            '{"detected_language_bcp47": "ja-JP"}', # Detection
            '{"errors": [{"wrong": "日本語", "correct": "にほんご", "type": "grammar", "reason": "test"}]}' # Grammar
        ]
    
        item = GrammarWorkItem(
            ctx=ctx,
            full_text="日本語で書いています。",
            n_start=0,
            n_end=10,
            grammar_bcp47="zh-CN", # Wrong locale
            partial_sentence=False,
            doc_id="doc123",
            inflight_key="key123",
            enqueue_seq=1,
            proofread_sentence_text="日本語で書いています。"
        )
    
        run_llm_and_cache_batch([item])
    
        # 1. Verify document update was triggered
        mock_apply.assert_called_once_with(ctx, "doc123", "日本語で書いています。", "ja-JP")
    
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


def test_locale_mismatch_batch_splits_and_double_caches(
) -> None:
    """Verify that locale mismatch in a batch triggers individual check and double-caches for mismatched item."""
    ctx = MagicMock()
    with patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_config_int_safe", return_value=8), \
         patch("plugin.framework.config.get_config_bool_safe", side_effect=lambda c, key, default=False: True if "detect_language" in key else False), \
         patch("plugin.writer.locale.grammar_work_queue.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_work_queue.cache_put_sentence") as mock_cache_put, \
         patch("plugin.writer.locale.grammar_work_queue._apply_language_change") as mock_apply, \
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
         patch("plugin.framework.client.llm_client.LlmClient") as mock_llm_client, \
         patch("plugin.writer.locale.grammar_work_queue.normalize_errors_for_text", return_value=[]):

        # Mock LLM client:
        # 1. Batch detection: [zh-CN, ja-JP]
        # 2. Individual grammar check for item 2 (ja-JP)
        # 3. Batch grammar check for item 1 (zh-CN)
        mock_client_inst = mock_llm_client.return_value
        mock_client_inst.chat_completion_sync.side_effect = [
            '{"results": [{"detected_language_bcp47": "zh-CN"}, {"detected_language_bcp47": "ja-JP"}]}', # Batch Detection
            '{"errors": []}', # Individual Grammar for item 2
            '{"results": [{"errors": []}]}' # Batch Grammar for item 1 (now a batch of 1)
        ]
    
        item1 = GrammarWorkItem(ctx=ctx, full_text="Sentence 1.", n_start=0, n_end=11, grammar_bcp47="zh-CN", partial_sentence=False, doc_id="d1", inflight_key="k1", enqueue_seq=1, proofread_sentence_text="Sentence 1.")
        item2 = GrammarWorkItem(ctx=ctx, full_text="日本語の文章。", n_start=12, n_end=20, grammar_bcp47="zh-CN", partial_sentence=False, doc_id="d1", inflight_key="k2", enqueue_seq=2, proofread_sentence_text="日本語の文章。")
    
        run_llm_and_cache_batch([item1, item2])
    
        # 1. Verify document update for item 2
        mock_apply.assert_called_once_with(ctx, "d1", "日本語の文章。", "ja-JP")
    
        # 2. Verify cache puts:
        # - item 2 ja-JP
        # - item 2 zh-CN (loop breaker)
        # - item 1 zh-CN (original batch)
        assert mock_cache_put.call_count == 3
        
        # Check item 2 ja-JP
        args, _ = mock_cache_put.call_args_list[0]
        assert args[0] == "ja-JP"
        assert args[1] == "日本語の文章。"
        
        # Check item 2 zh-CN
        args, _ = mock_cache_put.call_args_list[1]
        assert args[0] == "zh-CN"
        assert args[1] == "日本語の文章。"
        
        # Check item 1 zh-CN
        args, _ = mock_cache_put.call_args_list[2]
        assert args[0] == "zh-CN"
        assert args[1] == "Sentence 1."

def test_locale_mismatch_batch_cached_detection_double_caches(
) -> None:
    """Verify that locale mismatch in a batch works even when detection results are already cached."""
    ctx = MagicMock()
    # Mock detection cache to return ja-JP for sentence 2
    sent2_text = "日本語の文章。"
    from plugin.writer.locale.grammar_work_queue import _lang_detect_cache
    _lang_detect_cache[sent2_text] = "ja-JP"
    
    try:
        with patch("plugin.framework.config.is_grammar_enabled", return_value=True),              patch("plugin.framework.config.get_config_int_safe", return_value=8),              patch("plugin.framework.config.get_config_bool_safe", side_effect=lambda c, key, default=False: True if "detect_language" in key else False),              patch("plugin.writer.locale.grammar_work_queue.cache_get_sentence", return_value=None),              patch("plugin.writer.locale.grammar_work_queue.cache_put_sentence") as mock_cache_put,              patch("plugin.writer.locale.grammar_work_queue._apply_language_change") as mock_apply,              patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"),              patch("plugin.framework.client.llm_client.LlmClient") as mock_llm_client,              patch("plugin.writer.locale.grammar_work_queue.normalize_errors_for_text", return_value=[]):

            # Mock LLM client:
            # 1. Individual Grammar for item 2 (ja-JP) - triggered by mismatch detection
            # 2. Batch Grammar for item 1 (zh-CN)
            mock_client_inst = mock_llm_client.return_value
            mock_client_inst.chat_completion_sync.side_effect = [
                '{"errors": []}', # Individual Grammar for item 2
                '{"results": [{"errors": []}]}' # Batch Grammar for item 1
            ]
        
            item1 = GrammarWorkItem(ctx=ctx, full_text="Sentence 1.", n_start=0, n_end=11, grammar_bcp47="zh-CN", partial_sentence=False, doc_id="d1", inflight_key="k1", enqueue_seq=1, proofread_sentence_text="Sentence 1.")
            item2 = GrammarWorkItem(ctx=ctx, full_text=sent2_text, n_start=12, n_end=20, grammar_bcp47="zh-CN", partial_sentence=False, doc_id="d1", inflight_key="k2", enqueue_seq=2, proofread_sentence_text=sent2_text)
        
            # This should NOT trigger batch detection because item 2 is cached and we'll mock item 1 too
            _lang_detect_cache["Sentence 1."] = "zh-CN"
            
            run_llm_and_cache_batch([item1, item2])
        
            # 1. Verify document update for item 2
            mock_apply.assert_called_once_with(ctx, "d1", sent2_text, "ja-JP")
        
            # 2. Verify cache puts:
            # - item 2 ja-JP
            # - item 2 zh-CN (loop breaker)
            # - item 1 zh-CN
            assert mock_cache_put.call_count == 3
            
            # Check item 2 ja-JP
            args, _ = mock_cache_put.call_args_list[0]
            assert args[0] == "ja-JP"
            assert args[1] == sent2_text
            
            # Check item 2 zh-CN
            args, _ = mock_cache_put.call_args_list[1]
            assert args[0] == "zh-CN"
            assert args[1] == sent2_text
    finally:
        _lang_detect_cache.pop(sent2_text, None)
        _lang_detect_cache.pop("Sentence 1.", None)
