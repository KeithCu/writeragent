# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grammar_work_queue.py."""

from __future__ import annotations

import inspect
from pathlib import Path


from plugin.writer.locale.grammar_work_queue import (
    GrammarWorkItem,
    GrammarWorkQueue,
    deduplicate_grammar_batch,
    enqueue_misses,
    filter_stale_and_group,
    inflight_superseded,
    record_enqueue_latest,
    should_replace_for_key,
)
from unittest.mock import MagicMock, patch
import pytest


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


def _item(seq: int, key: str = "d|en-US|k1", *, partial: bool = False) -> GrammarWorkItem:
    return GrammarWorkItem(
        ctx=None,
        text="x",
        grammar_bcp47="en-US",
        partial_sentence=partial,
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
    provider: str = "",
    checker_identity: str = "",
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
        provider=provider,
        checker_identity=checker_identity,
    )


def test_mid_sentence_typing_dedup() -> None:
    """Incomplete sentences share a stable key and should supersede."""
    from plugin.writer.locale.grammar_work_queue import grammar_inflight_key
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


@pytest.mark.parametrize(
    "value, value_2",
    [
        # Different sentences should have different keys (based on their text) and both remain.
        pytest.param("First sentence.", "Second sentence.", id="test_two_sentences_same_document_distinct_inflight_keys_survive"),
        # Two different complete sentences with same relative start (handled by text-based keys) survive.
        pytest.param("Paragraph one is unique.", "Paragraph two is also unique.", id="test_paragraph_collision_survives_dedup"),
    ],
)
def test_two_sentences_same_document_distinct_inflight_keys_survive(value, value_2) -> None:
    from plugin.writer.locale.grammar_work_queue import grammar_inflight_key
    s1 = value
    s2 = value_2
    key1 = grammar_inflight_key("doc1", "en-US", s1, is_complete=True)
    key2 = grammar_inflight_key("doc1", "en-US", s2, is_complete=True)
    
    assert key1 != key2
    
    items = [
        _make_item(s1, seq=1, inflight_key=key1),
        _make_item(s2, seq=2, inflight_key=key2),
    ]
    result = deduplicate_grammar_batch(items)
    assert len(result) == 2


def test_grammar_inflight_key_complete_uses_full_fingerprint() -> None:
    from plugin.writer.locale.grammar_proofread_locale import fingerprint_for_text
    from plugin.writer.locale.grammar_work_queue import grammar_inflight_key

    text = "Hello world."
    assert grammar_inflight_key("doc1", "en-US", text, is_complete=True) == f"doc1|en-US|{fingerprint_for_text(text)}"


def test_grammar_inflight_key_incomplete_stable() -> None:
    from plugin.writer.locale.grammar_work_queue import grammar_inflight_key

    key = grammar_inflight_key("doc1", "en-US", "H", is_complete=False)
    assert key == "doc1|en-US|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    assert key == grammar_inflight_key("doc1", "en-US", "Hello world", is_complete=False)

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
    assert d2["d|en-US|k1"] == 10

    d3, out_of_order3, prev_bad3 = record_enqueue_latest(d, _item(15))
    assert out_of_order3 is False
    assert prev_bad3 is None
    assert d3["d|en-US|k1"] == 15


# NOTE (historical): test_tail_enqueue_operation and the entire Layer 1
# "tail-replace under Queue mutex" mechanism were removed in the TD4
# simplification pass. The drain-loop dict accumulator (this test) plus
# deduplicate_grammar_batch + _latest_seq guards are now the complete story.


def test_drain_loop_collapses_same_key_items_during_burst() -> None:
    """Completes and partials are dispatched in separate batches.
    Completes coalesce for GRAMMAR_COMPLETE_COALESCE_S, while partials
    debounce for GRAMMAR_WORKER_PAUSE_TIMEOUT_S. Same-key collapse still holds.
    """
    import threading

    q = GrammarWorkQueue()
    incomplete_key = "doc1|en-US|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    complete_a = "doc1|en-US|complete-a"
    complete_b = "doc1|en-US|complete-b"

    items = [
        _item(1, key=incomplete_key, partial=True),
        _item(2, key=complete_a, partial=False),
        _item(3, key=incomplete_key, partial=True),
        _item(4, key=complete_b, partial=False),
        _item(5, key=incomplete_key, partial=True),
    ]

    drained: list[list[GrammarWorkItem]] = []
    drain_done = threading.Event()

    def fake_run(group_items, *, grammar_queue_instance=None):
        drained.append(list(group_items))
        if len(drained) == 2:
            q._q.put(None)
            drain_done.set()

    for it in items:
        q._q.put(it)

    with patch("plugin.writer.locale.grammar_worker.run_llm_and_cache_batch", side_effect=fake_run), \
         patch("plugin.writer.locale.grammar_proofread_locale.GRAMMAR_WORKER_PAUSE_TIMEOUT_S", 0.05), \
         patch("plugin.writer.locale.grammar_proofread_locale.GRAMMAR_COMPLETE_COALESCE_S", 0.01):
        q._ensure_workers(None)
        assert drain_done.wait(timeout=2.0), "drain loop did not run both batches"

    assert len(drained) == 2
    # Batch 1: completes
    completes_batch = drained[0]
    assert {item.inflight_key for item in completes_batch} == {complete_a, complete_b}
    by_key_completes = {item.inflight_key: item for item in completes_batch}
    assert by_key_completes[complete_a].enqueue_seq == 2
    assert by_key_completes[complete_b].enqueue_seq == 4

    # Batch 2: partials (highest seq 5 kept)
    partials_batch = drained[1]
    assert len(partials_batch) == 1
    assert partials_batch[0].inflight_key == incomplete_key
    assert partials_batch[0].enqueue_seq == 5


def test_drain_loop_complete_latency_under_one_second() -> None:
    """One complete item is handed to the batch runner well under a second (does not wait for 1.0s pause)."""
    import threading
    import time

    q = GrammarWorkQueue()
    drained: list[list[GrammarWorkItem]] = []
    done_evt = threading.Event()

    def fake_run(group_items, *, grammar_queue_instance=None):
        drained.append(list(group_items))
        q._q.put(None)
        done_evt.set()

    start_time = time.monotonic()
    with patch("plugin.writer.locale.grammar_worker.run_llm_and_cache_batch", side_effect=fake_run):
        # Leave real GRAMMAR_WORKER_PAUSE_TIMEOUT_S (1.0s) in place
        q._ensure_workers(None)
        q.enqueue(_item(1, key="doc1|en-US|complete-1", partial=False))
        assert done_evt.wait(timeout=2.0)
    elapsed = time.monotonic() - start_time
    assert elapsed < 0.6, f"Complete item took {elapsed}s to drain; expected well under 1.0s"
    assert len(drained) == 1
    assert drained[0][0].enqueue_seq == 1


def test_drain_loop_partial_debounces_until_quiet_period() -> None:
    """Partial enqueues every 50ms for less than the pause produce no call; after quiet period, one call with newest seq."""
    import threading
    import time

    q = GrammarWorkQueue()
    drained: list[list[GrammarWorkItem]] = []
    done_evt = threading.Event()

    def fake_run(group_items, *, grammar_queue_instance=None):
        drained.append(list(group_items))
        q._q.put(None)
        done_evt.set()

    key = "doc1|en-US|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    with patch("plugin.writer.locale.grammar_worker.run_llm_and_cache_batch", side_effect=fake_run), \
         patch("plugin.writer.locale.grammar_proofread_locale.GRAMMAR_WORKER_PAUSE_TIMEOUT_S", 0.2):
        q._ensure_workers(None)
        # Send 3 partial enqueues spaced by 50ms (total 100ms < 200ms pause)
        for seq in (1, 2, 3):
            q.enqueue(_item(seq, key=key, partial=True))
            time.sleep(0.05)
            assert len(drained) == 0

        assert done_evt.wait(timeout=2.0)

    assert len(drained) == 1
    assert drained[0][0].enqueue_seq == 3


def test_drain_loop_complete_dispatched_while_partial_pending() -> None:
    """A complete item is dispatched while partial enqueues are still resetting the partial deadline."""
    import threading
    import time

    q = GrammarWorkQueue()
    drained: list[list[GrammarWorkItem]] = []
    complete_done = threading.Event()
    all_done = threading.Event()

    def fake_run(group_items, *, grammar_queue_instance=None):
        drained.append(list(group_items))
        if len(drained) == 1:
            complete_done.set()
        elif len(drained) == 2:
            q._q.put(None)
            all_done.set()

    incomplete_key = "doc1|en-US|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    complete_key = "doc1|en-US|complete-key"

    with patch("plugin.writer.locale.grammar_worker.run_llm_and_cache_batch", side_effect=fake_run), \
         patch("plugin.writer.locale.grammar_proofread_locale.GRAMMAR_WORKER_PAUSE_TIMEOUT_S", 0.4):
        q._ensure_workers(None)
        q.enqueue(_item(1, key=incomplete_key, partial=True))
        time.sleep(0.02)
        q.enqueue(_item(2, key=complete_key, partial=False))

        assert complete_done.wait(timeout=0.25), "Complete was not dispatched while partial was pending"
        assert len(drained) == 1
        assert drained[0][0].inflight_key == complete_key

        assert all_done.wait(timeout=1.0)
        assert len(drained) == 2
        assert drained[1][0].inflight_key == incomplete_key


def test_drain_loop_exception_resilience() -> None:
    """A raising batch runner does not prevent a later item on that same worker from running."""
    import threading

    q = GrammarWorkQueue()
    calls: list[int] = []
    first_failed = threading.Event()
    second_done = threading.Event()

    def fake_run(group_items, *, grammar_queue_instance=None):
        seq = group_items[0].enqueue_seq
        calls.append(seq)
        if seq == 1:
            first_failed.set()
            raise RuntimeError("Boom!")
        if seq == 2:
            q._q.put(None)
            second_done.set()

    with patch("plugin.writer.locale.grammar_worker.run_llm_and_cache_batch", side_effect=fake_run), \
         patch("plugin.writer.locale.grammar_proofread_locale.GRAMMAR_COMPLETE_COALESCE_S", 0.01):
        q._ensure_workers(None)
        q.enqueue(_item(1, key="k1", partial=False))
        assert first_failed.wait(timeout=2.0)
        q.enqueue(_item(2, key="k2", partial=False))
        assert second_done.wait(timeout=2.0)

    assert calls == [1, 2]


def test_inflight_superseded() -> None:
    latest = {"k": 9}
    assert inflight_superseded(latest, "k", 7) is True
    assert inflight_superseded(latest, "k", 9) is False
    assert inflight_superseded(latest, "other", 1) is False


def test_note_inflight_generation_publishes_key_and_keeps_newer() -> None:
    """A retarget records its key. An older seq does not erase a newer one."""
    q = GrammarWorkQueue()
    assert q.note_inflight_generation("doc|fr-FR|abc", 4) is False
    assert q.inflight_superseded("doc|fr-FR|abc", 4) is False
    assert q.inflight_superseded("doc|fr-FR|abc", 3) is True
    assert q.note_inflight_generation("doc|fr-FR|abc", 2) is True
    assert q._latest_seq["doc|fr-FR|abc"] == 4
    assert q.note_inflight_generation("doc|fr-FR|abc", 8) is False
    assert q.inflight_superseded("doc|fr-FR|abc", 4) is True


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

    def sleep_unlocked(_sec: float) -> None:
        assert not q._lock.locked()

    with patch("plugin.writer.locale.grammar_proofread_locale.grammar_max_in_flight", return_value=3), \
         patch("plugin.writer.locale.grammar_work_queue.threading.Thread", side_effect=track_thread), \
         patch("plugin.writer.locale.grammar_work_queue.time.sleep", side_effect=sleep_unlocked) as sleep_mock:
        q._ensure_workers(ctx)
    assert sleep_mock.call_count == 0
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


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param(3, 5, True, id="test_should_replace_for_key_newer_wins"),
        pytest.param(5, 3, False, id="test_should_replace_for_key_older_loses"),
        # Same seq does not replace — only strictly newer wins.
        pytest.param(4, 4, False, id="test_should_replace_for_key_equal_seq_loses"),
    ],
)
def test_should_replace_for_key_newer_wins(value, value_2, expected) -> None:
    assert should_replace_for_key(_item(value), _item(value_2)) is expected

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
    assert ("doc_a", "en-US", "", "") in groups
    assert ("doc_b", "fr-FR", "", "") in groups
    assert len(groups[("doc_a", "en-US", "", "")]) == 2
    assert len(groups[("doc_b", "fr-FR", "", "")]) == 1


def test_filter_stale_and_group_splits_harper_and_llm() -> None:
    h = _make_item("S1.", doc_id="doc_a", locale="en-US", seq=1, inflight_key="h1", provider="harper", checker_identity="harper")
    llm_item = _make_item("S2.", doc_id="doc_a", locale="en-US", seq=2, inflight_key="l1", provider="llm", checker_identity="llm:model-x")
    groups = filter_stale_and_group([h, llm_item], lambda _: False)
    assert ("doc_a", "en-US", "harper", "harper") in groups
    assert ("doc_a", "en-US", "llm", "llm:model-x") in groups
    assert len(groups) == 2


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


def test_enqueue_misses() -> None:
    queue = MagicMock()
    ctx = MagicMock()
    uncached = [(0, 14, "Sentence one. "), (14, 27, "Sentence two.")]
    enqueue_misses(
        queue,
        ctx,
        "doc1",
        "en-US",
        uncached,
        provider="custom-prov",
        checker_identity="custom-ident",
        paragraph_text="Sentence one. Sentence two.",
    )
    assert queue.enqueue.call_count == 2
    item1 = queue.enqueue.call_args_list[0][0][0]
    item2 = queue.enqueue.call_args_list[1][0][0]
    assert item1.text == "Sentence one. "
    assert item1.provider == "custom-prov"
    assert item1.checker_identity == "custom-ident"
    assert item1.doc_id == "doc1"
    assert item1.grammar_bcp47 == "en-US"
    assert item2.text == "Sentence two."
    assert item2.provider == "custom-prov"


def test_end_status_cycle_without_done_emits_one_skipped_terminal() -> None:
    q = GrammarWorkQueue()
    with patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status") as mock_status:
        q.begin_status_cycle()
        q.end_status_cycle()
        q.end_status_cycle()
    assert mock_status.call_count == 1
    assert mock_status.call_args[0] == ("done", "batch")
    assert mock_status.call_args[1].get("result") == "skipped"


def test_forget_document_prunes_only_that_document() -> None:
    q = GrammarWorkQueue()
    q.note_inflight_generation("doc1|en-US|k1", 1)
    q.note_inflight_generation("doc1|fr-FR|k2", 2)
    q.note_inflight_generation("doc2|en-US|k3", 3)
    q.forget_document("doc1")
    with q._lock:
        assert "doc1|en-US|k1" not in q._latest_seq
        assert "doc1|fr-FR|k2" not in q._latest_seq
        assert q._latest_seq.get("doc2|en-US|k3") == 3


def test_remove_persistence_if_prunes_queue_generations() -> None:
    import time
    from typing import Any, cast
    from plugin.writer.locale.grammar_persistence import grammar_registry
    from plugin.writer.locale.grammar_work_queue import grammar_queue

    doc_id = f"test_doc_k192_{time.time_ns()}"
    fake = MagicMock()
    with grammar_registry.lock:
        grammar_registry.doc_persistence_instances[doc_id] = cast("Any", fake)
    key = f"{doc_id}|en-US|abc"
    grammar_queue.note_inflight_generation(key, 5)
    try:
        assert grammar_registry.remove_persistence_if(doc_id, cast("Any", fake)) is True
        with grammar_queue._lock:
            assert key not in grammar_queue._latest_seq
    finally:
        with grammar_registry.lock:
            grammar_registry.doc_persistence_instances.pop(doc_id, None)
            grammar_registry.doc_locales.pop(doc_id, None)
        grammar_queue.forget_document(doc_id)


def test_enqueue_misses_emits_one_queued_status() -> None:
    queue = MagicMock()
    ctx = MagicMock()
    uncached = [(0, 14, "Sentence one. "), (14, 27, "Sentence two.")]
    with patch("plugin.writer.locale.grammar_work_queue.emit_grammar_status") as mock_status:
        enqueue_misses(
            queue,
            ctx,
            "doc1",
            "en-US",
            uncached,
        )
    assert mock_status.call_count == 1
    assert mock_status.call_args[0][0] == "start"
    assert mock_status.call_args[0][1] == "2 sentences"
    assert mock_status.call_args[1].get("result") == "queued"
    assert queue.enqueue.call_count == 2



