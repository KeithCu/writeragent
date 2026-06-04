# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grammar_work_queue.py."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from plugin.writer.locale.grammar_work_queue import (
    GrammarWorkQueue,
    GrammarWorkItem,
    deduplicate_grammar_batch,
    filter_stale_and_group,
    inflight_superseded,
    is_stale,
    record_enqueue_latest,
    run_llm_and_cache_batch,
    should_replace_for_key,
)
from plugin.writer.locale.grammar_proofread_text import NormalizedProofError
from unittest.mock import MagicMock, patch, ANY


def _grammar_obs_call_sites_present() -> bool:
    """True when ``grammar_obs(...)`` call sites exist in the work-queue module under test.

    ``make release`` runs pytest against a stripped bundle (``scripts/strip_code.py`` removes
  only ``grammar_obs`` expression statements). Imports and ``grammar_obs.py`` remain.
    """
    from plugin.writer.locale import grammar_work_queue as gwq

    try:
        source = Path(inspect.getfile(gwq)).read_text(encoding="utf-8")
    except OSError:
        return False
    return "grammar_obs(" in source


def _item(seq: int, key: str = "d|en-US|k1") -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=None,
        text="x",
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
        text=text,
        grammar_bcp47=locale,
        partial_sentence=False,
        doc_id=doc_id,
        inflight_key=inflight_key,
        enqueue_seq=seq,
    )


def test_mid_sentence_typing_dedup() -> None:
    """Incomplete sentences share a stable key and should supersede."""
    from plugin.writer.locale.grammar_proofread_locale import grammar_inflight_key
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
    surviving_text = result[0].text
    assert surviving_text == "This is a story."


def test_prefix_dedup_different_paragraphs() -> None:
    """Two different paragraphs (non-prefix) should both survive."""
    items = [
        _make_item("Hello world.", seq=1, doc_id="para_a"),
        _make_item("Goodbye world.", seq=2, doc_id="para_b"),
    ]
    result = deduplicate_grammar_batch(items)
    texts = {r.text for r in result}
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
    texts = {r.text for r in result}
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
    assert item.text == "What is going"


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
    assert item.text == "W"


def test_two_sentences_same_document_distinct_inflight_keys_survive() -> None:
    """Different sentences should have different keys (based on their text) and both remain."""
    from plugin.writer.locale.grammar_proofread_locale import grammar_inflight_key
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
    from plugin.writer.locale.grammar_proofread_locale import grammar_inflight_key
    
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
            text="No.",
            grammar_bcp47="en-US",
            partial_sentence=False,
            doc_id="doc1",
            inflight_key="doc1|en-US|0",
            enqueue_seq=1,
        ),
        GrammarWorkItem(
            ctx=None,
            text="No problem today.",
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


# NOTE (historical): test_tail_enqueue_operation and the entire Layer 1
# "tail-replace under Queue mutex" mechanism were removed in the TD4
# simplification pass. The drain-loop dict accumulator (this test) plus
# deduplicate_grammar_batch + _latest_seq guards are now the complete story.


def test_drain_loop_collapses_same_key_items_during_burst() -> None:
    """Regression: the drain loop's batch_by_key accumulator is now the
    *primary* (and, after removal of historical Layer 1 tail-replace, the
    only enqueue-time-path-independent) mechanism that collapses same-key
    items during rapid typing.

    The worker drains so quickly that the queue is empty between keystrokes
    in the common case; items that arrive while a previous batch is being
    processed (or while the worker is blocked on get()) are collapsed here
    using should_replace_for_key.  deduplicate_grammar_batch then acts as the
    canonical safety net.

    Historical note (old text for reference): during typing bursts the worker pulls items between keystrokes,
    so enqueue's tail-replace path cannot help \u2014 the queue is empty between
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
         patch("plugin.writer.locale.grammar_proofread_locale.GRAMMAR_WORKER_PAUSE_TIMEOUT_S", 0.01):
        q._ensure_workers(None)
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


def test_done_status_deferred_until_last_parallel_batch() -> None:
    q = GrammarWorkQueue()
    emitted: list[str] = []

    def capture_done(phase: str, text: str, **kwargs: object) -> None:
        if phase == "done":
            emitted.append(str(kwargs.get("result") or text))

    with patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status", side_effect=capture_done):
        q.begin_status_cycle()
        q.begin_status_cycle()
        q.record_done_status("a", result="first")
        q.end_status_cycle()
        assert emitted == []
        q.record_done_status("b", result="second")
        q.end_status_cycle()
        assert emitted == ["second"]


def test_ensure_workers_spawns_up_to_config() -> None:
    q = GrammarWorkQueue()
    ctx = MagicMock()
    started: list[str] = []

    def track_thread(*_args, **kwargs):
        started.append(kwargs["name"])
        mock_t = MagicMock()
        mock_t.start = MagicMock()
        return mock_t

    with patch("plugin.writer.locale.grammar_proofread_locale.grammar_max_in_flight", return_value=3), \
         patch("plugin.writer.locale.grammar_work_queue.threading.Thread", side_effect=track_thread), \
         patch("plugin.writer.locale.grammar_work_queue.time.sleep") as sleep_mock:
        q._ensure_workers(ctx)
    assert sleep_mock.call_count == 2
    assert q._worker_count == 3
    assert started == ["writeragent-grammar-queue-0", "writeragent-grammar-queue-1", "writeragent-grammar-queue-2"]
    # Idempotent: second call does not spawn more when count already matches desired.
    with patch("plugin.writer.locale.grammar_proofread_locale.grammar_max_in_flight", return_value=3), \
         patch("plugin.writer.locale.grammar_work_queue.threading.Thread", side_effect=track_thread):
        q._ensure_workers(ctx)
    assert q._worker_count == 3
    assert len(started) == 3


# ---------------------------------------------------------------------------
# Tests for should_replace_for_key (TD4 extraction)
# ---------------------------------------------------------------------------

def test_should_replace_for_key_first_item_always_replaces() -> None:
    """Missing existing (None) means the incoming item is the first for this key."""
    assert should_replace_for_key(None, _item(1)) is True


def test_should_replace_for_key_newer_wins() -> None:
    assert should_replace_for_key(_item(3), _item(5)) is True


def test_should_replace_for_key_older_loses() -> None:
    assert should_replace_for_key(_item(5), _item(3)) is False


def test_should_replace_for_key_equal_seq_loses() -> None:
    """Same seq does not replace — only strictly newer wins."""
    assert should_replace_for_key(_item(4), _item(4)) is False


# ---------------------------------------------------------------------------
# Tests for filter_stale_and_group (TD4 extraction)
# ---------------------------------------------------------------------------

def test_filter_stale_and_group_skips_stale() -> None:
    items = [_item(1, key="k1"), _item(2, key="k2"), _item(3, key="k3")]
    stale_keys = {"k2"}
    groups = filter_stale_and_group(items, lambda it: it.inflight_key in stale_keys)
    all_items = [it for g in groups.values() for it in g]
    assert len(all_items) == 2
    keys = {it.inflight_key for it in all_items}
    assert "k2" not in keys


def test_filter_stale_and_group_groups_by_doc_locale() -> None:
    a = _make_item("S1.", doc_id="doc_a", locale="en-US", seq=1, inflight_key="a1")
    b = _make_item("S2.", doc_id="doc_b", locale="fr-FR", seq=2, inflight_key="b1")
    c = _make_item("S3.", doc_id="doc_a", locale="en-US", seq=3, inflight_key="a2")
    groups = filter_stale_and_group([a, b, c], lambda _: False)
    assert ("doc_a", "en-US") in groups
    assert ("doc_b", "fr-FR") in groups
    assert len(groups[("doc_a", "en-US")]) == 2
    assert len(groups[("doc_b", "fr-FR")]) == 1


def test_filter_stale_and_group_all_stale_returns_empty() -> None:
    items = [_item(1), _item(2)]
    groups = filter_stale_and_group(items, lambda _: True)
    assert groups == {}


def test_drain_batch_accumulation_matches_deduplicate() -> None:
    """Verify that dict-based accumulation (Layer 2 fast path) produces the same
    result as ``deduplicate_grammar_batch`` for the same input."""
    key = "d|en-US|k1"
    items = [
        _item(1, key=key),
        _item(5, key=key),
        _item(3, key=key),
        _item(2, key="other"),
    ]
    # Simulate dict accumulator (same logic as _drain_loop)
    batch_by_key: dict[str, GrammarWorkItem] = {}
    for it in items:
        prev = batch_by_key.get(it.inflight_key)
        if should_replace_for_key(prev, it):
            batch_by_key[it.inflight_key] = it
    dict_result = sorted(batch_by_key.values(), key=lambda x: x.inflight_key)

    # Canonical dedup
    dedup_result = sorted(deduplicate_grammar_batch(items), key=lambda x: x.inflight_key)

    assert len(dict_result) == len(dedup_result)
    for a, b in zip(dict_result, dedup_result):
        assert a.inflight_key == b.inflight_key
        assert a.enqueue_seq == b.enqueue_seq


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
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
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
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
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
         patch("plugin.framework.config.get_config_bool_safe", side_effect=lambda c, key, default=False: True if "detect_language" in key else False), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_get_sentence", return_value=None), \
         patch("plugin.writer.locale.grammar_proofread_cache.cache_put_sentence") as mock_cache_put, \
         patch("plugin.writer.locale.grammar_work_queue._apply_language_change") as mock_apply, \
         patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status"), \
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
    from plugin.writer.locale.grammar_work_queue import GrammarWorkerContext, _run_language_validation, _lang_detect_cache

    item = _make_item("The cat sat.", doc_id="doc99")
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
        _lang_detect_cache.pop(item.text, None)
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=mock_p):
            with patch("plugin.writer.locale.grammar_worker_llm.get_cached_language", return_value=None):
                with patch("plugin.framework.queue_executor.grammar_llm_request_gate", return_value=mock_lane):
                    with patch(
                        "plugin.writer.locale.grammar_work_queue.detect_languages_for_chunk",
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
    from plugin.writer.locale.grammar_work_queue import GrammarWorkerContext, _detect_languages, _lang_detect_cache

    item = _make_item("The cat sat.", doc_id="doc99")
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

    try:
        _lang_detect_cache.pop(item.text, None)
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=mock_p) as mock_get_p:
            with patch("plugin.writer.locale.grammar_worker_llm.get_cached_language", return_value=None):
                detected = _detect_languages([(item, item.text)], "", ec)
        ec.client.chat_completion_sync.assert_not_called()
        assert detected == ["en-US"]
        mock_get_p.assert_called_once()
    finally:
        _lang_detect_cache.pop(item.text, None)


def test_language_detect_calls_llm_when_no_persisted_grammar() -> None:
    from plugin.writer.locale.grammar_work_queue import GrammarWorkerContext, _detect_languages, _lang_detect_cache

    item = _make_item("Fresh sentence.", doc_id="doc100")
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

    mock_lane = MagicMock()
    mock_lane.__enter__ = MagicMock(return_value=None)
    mock_lane.__exit__ = MagicMock(return_value=None)

    try:
        _lang_detect_cache.pop(item.text, None)
        with patch("plugin.writer.locale.grammar_persistence.get_persistence", return_value=mock_p):
            with patch("plugin.writer.locale.grammar_worker_llm.get_cached_language", return_value=None):
                with patch("plugin.framework.queue_executor.grammar_llm_request_gate", return_value=mock_lane):
                    detected = _detect_languages([(item, item.text)], "", ec)
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
    from plugin.writer.locale.grammar_work_queue import _worker_process_chunk

    item_a = _make_item("Hello one.", inflight_key="k1")
    item_b = _make_item("Hello two.", inflight_key="k2")
    ec = MagicMock()
    ec.ctx = MagicMock()
    ec.gq = None
    chunk = [(item_a, item_a.text), (item_b, item_b.text)]
    with patch("plugin.writer.locale.grammar_work_queue.grammar_obs") as mock_obs, \
         patch("plugin.writer.locale.grammar_work_queue._run_language_validation") as mock_val, \
         patch("plugin.writer.locale.grammar_work_queue._run_grammar_check") as mock_grammar:
        from plugin.writer.locale.grammar_worker_phases import LanguageValidationDecision

        mock_val.return_value = LanguageValidationDecision(target_bcp47="en-US", result_chunk=[])
        _worker_process_chunk(chunk, ec, "en-US", True, "")
    mock_grammar.assert_not_called()
    mock_obs.assert_any_call("worker_chunk_skip", reason="empty_result_chunk", chunk_len=2, target_bcp47="en-US", requeue_count=0)


@pytest.mark.skipif(
    not _grammar_obs_call_sites_present(),
    reason="Stripped release bundle removes grammar_obs(...) call sites (scripts/strip_code.py)",
)
def test_worker_chunk_skip_lang_validation_failed_obs() -> None:
    from plugin.writer.locale.grammar_work_queue import _worker_process_chunk

    item = _make_item("Hello.")
    ec = MagicMock()
    ec.ctx = MagicMock()
    with patch("plugin.writer.locale.grammar_work_queue.grammar_obs") as mock_obs, \
         patch("plugin.writer.locale.grammar_work_queue._run_language_validation", return_value=None), \
         patch("plugin.writer.locale.grammar_work_queue._run_grammar_check") as mock_grammar:
        _worker_process_chunk([(item, item.text)], ec, "en-US", True, "")
    mock_grammar.assert_not_called()
    mock_obs.assert_any_call("worker_chunk_skip", reason="lang_validation_failed", chunk_len=1)
