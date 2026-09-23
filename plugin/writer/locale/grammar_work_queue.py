# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar work queue: work items, batch dedup, pure enqueue/stale helpers, parallel LLM workers.

Queue dedup / stale-suppression mental model
=============================================

The grammar queue must ensure that for any given ``inflight_key``, only the
**newest** snapshot (highest ``enqueue_seq``) ever reaches the LLM.  Two
remaining layers (plus the ``_latest_seq`` generation map) enforce this
invariant.  (A third Layer 1 "tail-replace" existed historically — see below.)

**Layer 2 — Batch-drain dedup** (``_drain_loop`` dict accumulator)

    After the worker wakes on the first ``get()``, it enters a tight
    ``get(timeout=GRAMMAR_WORKER_PAUSE_TIMEOUT_S)`` loop that collects every
    pending item into a ``batch_by_key`` dict keyed by ``inflight_key``.
    For each key only the item with the highest ``enqueue_seq`` is kept
    (same rule as the tested pure ``deduplicate_grammar_batch``).

    *Blind spot*: The dict cannot detect items that were already consumed in a
    *previous* batch and whose ``inflight_key`` was re-enqueued while the
    worker was busy with the LLM.

**Layer 3 — Pre-execute and post-LLM stale checks** (``_latest_seq`` map)

    ``enqueue`` records the newest ``enqueue_seq`` per ``inflight_key`` in
    ``_latest_seq`` (under ``_lock``).  Before sending a batch item to
    the LLM, the worker calls ``_is_stale`` — if a newer enqueue has been
    recorded since this item was drained, it is skipped.  After the LLM
    returns, ``inflight_superseded`` is checked again before writing to the
    sentence cache, catching items superseded during the (possibly slow)
    HTTP round-trip.

**Historical Layer 1 (removed)**

    An earlier O(1) "tail-replace" lived in ``enqueue()``: it acquired
    ``self._q.mutex`` and directly mutated ``self._q.queue[-1]`` when the
    tail shared the same ``inflight_key`` and the incoming item had a higher
    seq.  This was the classic "clever" bit (direct access to a ``Queue``'s
    internal deque + ``unfinished_tasks`` / ``not_empty.notify()``).

    It was removed because:
    - The worker drains so quickly that the queue is *usually empty* on the
      next enqueue during real typing bursts (the exact scenario the comment
      in the old Layer 2 section called out).
    - Layer 2 (the drain dict) + Layer 3 (``_latest_seq`` guards, including
      the language-requeue path) already provide complete protection.
    - Removing it eliminates the highest-cognitive-load construct while
      changing no observable behavior for squiggles, cache, or LLM calls.

**``inflight_key`` design**

    Complete sentences: ``{doc_id}|{locale}|{hash(text)[:16]}``.  Unique
    per sentence, stable if the sentence is unchanged — so two different
    sentences in the same paragraph never collide.

    Incomplete sentences: ``{doc_id}|{locale}|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING``.
    All partial drafts for the active typing spot share one key, ensuring
    every keystroke supersedes the previous draft.

**``enqueue_seq`` as generation stamp**

    A global monotonic counter (``next_enqueue_seq``), not a queue position.
    It records *when* a snapshot was created.  Queue FIFO only orders
    ``get()`` calls; ``enqueue_seq`` records supersede relationships across
    batches and stale checks (the old tail-replace path is no longer one of
    them).
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping


from . import (
    grammar_proofread_locale,
)
from .grammar_obs import emit_grammar_status, grammar_obs
from .grammar_proofread_text import slice_preview_debug



log = logging.getLogger("writeragent.grammar")


@dataclass(frozen=True)
class GrammarWorkItem:
    """One queued grammar job (defined here so dedup tests avoid UNO imports)."""

    ctx: Any
    text: str
    grammar_bcp47: str
    partial_sentence: bool
    doc_id: str
    inflight_key: str
    enqueue_seq: int
    original_bcp47: str = ""
    provider: str = ""





def deduplicate_grammar_batch(batch: list[GrammarWorkItem]) -> list[GrammarWorkItem]:
    """Return one queue item per ``inflight_key``, keeping the highest ``enqueue_seq``."""
    best_by_key: dict[str, GrammarWorkItem] = {}
    for item in batch:
        prev = best_by_key.get(item.inflight_key)
        if should_replace_for_key(prev, item):
            best_by_key[item.inflight_key] = item
        else:
            log.debug("[grammar] queue dedup: dropped older same-key item seq=%s key=%s (newer seq=%s kept)", item.enqueue_seq, item.inflight_key, prev.enqueue_seq if prev else None)
    return list(best_by_key.values())


def record_enqueue_latest(prev: dict[str, int], item: GrammarWorkItem) -> tuple[dict[str, int], bool, int | None]:
    """Return updated ``latest_seq``, whether incoming seq was out-of-order, and prior seq for logging."""
    key = item.inflight_key
    prev_seq = prev.get(key)
    out_of_order = prev_seq is not None and item.enqueue_seq < prev_seq
    new_d = dict(prev)
    new_d[key] = item.enqueue_seq
    return new_d, out_of_order, prev_seq if out_of_order else None


def inflight_superseded(latest_seq: Mapping[str, int], inflight_key: str, enqueue_seq: int) -> bool:
    """True if ``latest_seq`` records a newer generation than ``enqueue_seq`` for ``inflight_key``.

    Used for pre-execute skip (via ``GrammarWorkQueue._is_stale``) and post-LLM cache skip.
    """
    latest = latest_seq.get(inflight_key)
    return latest is not None and enqueue_seq < latest


def should_replace_for_key(existing: GrammarWorkItem | None, incoming: GrammarWorkItem) -> bool:
    """True if ``incoming`` should replace ``existing`` in a per-key accumulator.

    Used by the ``_drain_loop`` dict accumulator and by ``deduplicate_grammar_batch``
    (unit tests / any external caller). A missing ``existing`` always returns True.
    """
    return existing is None or incoming.enqueue_seq > existing.enqueue_seq


def filter_stale_and_group(
    items: list[GrammarWorkItem],
    is_stale_fn: Any,
) -> dict[tuple[str, str], list[GrammarWorkItem]]:
    """Drop stale items and group the rest by ``(doc_id, grammar_bcp47)``.

    ``is_stale_fn`` is called with each item; items for which it returns True
    are skipped (with an obs log).  Returns a dict mapping
    ``(doc_id, locale)`` to the non-stale items in that group.
    """
    groups: dict[tuple[str, str], list[GrammarWorkItem]] = defaultdict(list)
    stale_count = 0
    for item in items:
        if is_stale_fn(item):
            grammar_obs("queue_stale_skip", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key)
            stale_count += 1
            continue
        groups[(item.doc_id, item.grammar_bcp47)].append(item)
    if stale_count > 0:
        grammar_obs("batch_stats", sentences_stale_skipped=stale_count, survivor_count=sum(len(v) for v in groups.values()))
    return dict(groups)


_ENQUEUE_SEQ_COUNTER = itertools.count(1)


def next_enqueue_seq() -> int:
    """Monotonic generation stamp for ``GrammarWorkItem.enqueue_seq`` (supersede / stale detection)."""
    return next(_ENQUEUE_SEQ_COUNTER)


@dataclass(frozen=True)
class _PendingGrammarDone:
    text: str
    result: str
    elapsed_ms: int | None
    preview_source: str | None
    length_hint: int | None


class GrammarWorkQueue:
    """Multi-worker queue for grammar LLM requests (stampede + per-key supersede).

    Up to ``doc.grammar_proofreader_max_in_flight`` daemon drain threads share one
    ``queue.Queue``; each batch still respects ``grammar_llm_request_gate`` for HTTP.

    TD4 note: an ``InflightTracker`` wrapper around ``_lock`` + ``_latest_seq``
    was evaluated and rejected — the tracker would absorb 2 fields and 3 thin methods
    but ``GrammarWorkQueue`` is already small enough that an extra indirection adds more
    cognitive load than it removes.  The pure functions (``should_replace_for_key``,
    ``filter_stale_and_group``, ``inflight_superseded``, ``record_enqueue_latest``)
    keep the logic testable without wrapping the state.
    """

    _lock: threading.Lock
    _worker_count: int
    _status_inflight: int

    def __init__(self) -> None:
        self._q: queue.Queue[GrammarWorkItem | None] = queue.Queue()
        self._lock = threading.Lock()
        self._latest_seq: dict[str, int] = {}
        self._worker_count = 0
        self._status_inflight = 0
        self._pending_done: _PendingGrammarDone | None = None

    def begin_status_cycle(self) -> None:
        """Mark one ``run_llm_and_cache_batch`` in flight (sidebar ``done`` is deferred)."""
        with self._lock:
            self._status_inflight += 1

    def record_done_status(
        self,
        text: str,
        *,
        result: str = "",
        elapsed_ms: int | None = None,
        preview_source: str | None = None,
        length_hint: int | None = None,
    ) -> None:
        """Remember the latest chunk result; emitted when the last in-flight batch finishes."""
        with self._lock:
            self._pending_done = _PendingGrammarDone(text, result, elapsed_ms, preview_source, length_hint)

    def end_status_cycle(self) -> None:
        """Drop in-flight count; emit a single sidebar ``done`` when all parallel batches finish."""
        pending: _PendingGrammarDone | None = None
        with self._lock:
            self._status_inflight = max(0, self._status_inflight - 1)
            if self._status_inflight == 0:
                pending = self._pending_done
                self._pending_done = None
        if pending is not None:
            emit_grammar_status(
                "done",
                pending.text,
                result=pending.result,
                elapsed_ms=pending.elapsed_ms,
                preview_source=pending.preview_source,
                length_hint=pending.length_hint,
            )

    def _is_stale(self, item: GrammarWorkItem) -> bool:
        with self._lock:
            return inflight_superseded(self._latest_seq, item.inflight_key, item.enqueue_seq)

    def inflight_superseded(self, inflight_key: str, enqueue_seq: int) -> bool:
        """True if a newer grammar enqueue has been recorded for this key (e.g. user kept typing)."""
        with self._lock:
            return inflight_superseded(self._latest_seq, inflight_key, enqueue_seq)

    def enqueue(self, item: GrammarWorkItem) -> None:
        """Add a work item; starts the drain worker on first call.

        Same-key deduplication for rapid typing is the Layer 2 ``batch_by_key``
        dict inside ``_drain_loop`` (the worker drains so quickly that the queue
        is usually empty on the next enqueue during bursts). Cross-batch and
        in-flight supersedes use ``_latest_seq`` (Layer 3), including
        language-detection requeues that mint a fresh higher seq.
        """
        with self._lock:
            self._latest_seq, out_of_order, superseded_prev_seq = record_enqueue_latest(self._latest_seq, item)
            if out_of_order:
                log.error("[grammar] queue enqueue: out-of-order seq detected for key=%s: incoming seq=%s < latest seq=%s; stale detection may be unreliable", item.inflight_key, item.enqueue_seq, superseded_prev_seq)
        grammar_obs(
            "queue_enqueue",
            sentences_queued=1,
            doc_id=item.doc_id,
            locale=item.grammar_bcp47,
            seq=item.enqueue_seq,
            inflight_key=item.inflight_key,
            slice_len=len(item.text),
            partial_sentence=item.partial_sentence,
            preview=slice_preview_debug(item.text),
        )  # fmt: skip

        # Normal append.  (Historical Layer 1 "tail-replace" under _q.mutex was
        # removed in the TD4 simplification pass because it was ineffective
        # during the common rapid-drain burst case; see the module docstring.)
        self._q.put(item)
        self._ensure_workers(item.ctx)

    def _ensure_workers(self, ctx: Any) -> None:
        desired = grammar_proofread_locale.grammar_max_in_flight(ctx)
        while True:
            with self._lock:
                if self._worker_count >= desired:
                    return
                i = self._worker_count
                self._worker_count += 1
            if i > 0:
                # Stagger extra drain threads; do not hold _lock across sleep.
                from plugin.framework.client.request_controls import LLM_MIN_REQUEST_INTERVAL_SEC

                time.sleep(LLM_MIN_REQUEST_INTERVAL_SEC)
            t = threading.Thread(target=self._drain_loop, name=f"writeragent-grammar-queue-{i}", daemon=True)
            t.start()

    def _drain_loop(self) -> None:
        """Block-dequeue, batch-drain pending items, deduplicate, process one batch.

        ``None`` is a poison-pill; nothing enqueues it today. A real shutdown must
        put one ``None`` per drain thread, or extra workers block forever.
        """
        while True:
            first = self._q.get()
            if first is None:
                break
            # Layer 2 fast path: collapse same-key items as they arrive instead
            # of appending all then dedup-ing.  This is now the *primary*
            # dedup point for rapid typing (the historical Layer 1 tail-replace
            # at enqueue time was removed because the worker drains so quickly
            # that the queue is usually empty between keystrokes anyway).
            batch_by_key: dict[str, GrammarWorkItem] = {first.inflight_key: first}
            while True:
                try:
                    more = self._q.get(timeout=grammar_proofread_locale.GRAMMAR_WORKER_PAUSE_TIMEOUT_S)
                    if more is None:
                        return
                    prev = batch_by_key.get(more.inflight_key)
                    if should_replace_for_key(prev, more):
                        batch_by_key[more.inflight_key] = more
                except queue.Empty:
                    break
            batch = list(batch_by_key.values())
            grammar_obs("queue_drain_batch", batch_size=len(batch), seqs=tuple(x.enqueue_seq for x in batch), keys=tuple(x.inflight_key for x in batch))
            # Same-key newest-wins already applied in batch_by_key (same rule as
            # deduplicate_grammar_batch, which remains the tested pure helper).
            grammar_obs(
                "queue_drain_survivors",
                survivor_count=len(batch),
                seqs=tuple(x.enqueue_seq for x in batch),
            )

            groups = filter_stale_and_group(batch, self._is_stale)

            for (doc_id, locale), group_items in groups.items():
                try:
                    grammar_obs("queue_execute_batch", doc_id=doc_id, locale=locale, item_count=len(group_items))
                    import plugin.writer.locale.grammar_worker as grammar_worker
                    grammar_worker.run_llm_and_cache_batch(group_items, grammar_queue_instance=self)
                except Exception:
                    log.exception("[grammar] queue worker batch failed doc=%s loc=%s", doc_id, locale)


_grammar_queue_singleton = GrammarWorkQueue()

grammar_queue: GrammarWorkQueue = _grammar_queue_singleton
