# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar work queue: work items, pure enqueue/stale helpers, parallel LLM drain workers.

The invariant: for any ``inflight_key`` only the **newest** snapshot (highest
``enqueue_seq``) is sent to the LLM or written to the sentence cache.

**Supersede map** (``_latest_seq``) -- the one source of truth for staleness.

    ``enqueue`` records the newest ``enqueue_seq`` per ``inflight_key`` under
    ``_lock`` (pruned by ``forget_document``). A worker drops an item when
    ``inflight_superseded`` says a newer seq exists: once before the LLM call
    and again before writing the cache, which catches items superseded during
    the HTTP round-trip. In-place locale retargets record their new key via
    ``note_inflight_generation``.

**Batching / debounce policy** (the ``completes`` / ``partials`` dicts in ``_drain_loop``)

    This is scheduling, not a dedup layer. Partial sentences wait out a 1.0s
    quiet period (``GRAMMAR_WORKER_PAUSE_TIMEOUT_S``) reset by each newer seq
    for the same key; complete sentences coalesce for a fixed 50ms window
    (``GRAMMAR_COMPLETE_COALESCE_S``). Keeping only the highest seq per key in
    the dicts just avoids sending items the supersede map would drop anyway.

**N workers** (``grammar_max_in_flight``)

    Up to N drain workers share one ``queue.Queue``. Each worker owns its own
    ``completes`` / ``partials`` dicts, so with N > 1 the 1.0s quiet period is
    per worker: two keystrokes for the same key can land on different workers,
    and each worker debounces only what it pulled. That is intentional; the
    supersede map is shared, so the older snapshot is still dropped as stale.

**``inflight_key``**

    Complete sentences: ``{doc_id}|{locale}|{fingerprint_for_text}`` -- unique
    per sentence, stable while the sentence is unchanged.
    Incomplete sentences: ``{doc_id}|{locale}|INCOMPLETE_WRITER_AGENT_INTERNAL_STRING``
    -- every partial draft at the typing spot shares one key, so each keystroke
    supersedes the previous draft.

**``enqueue_seq``** is a global monotonic generation stamp (``next_enqueue_seq``),
not a queue position: FIFO orders ``get()`` calls, the seq orders supersedes.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from plugin.framework.quit_hooks import register_quit_hook


from . import (
    grammar_proofread_locale,
)
from .grammar_obs import get_grammar_status, grammar_obs, slice_preview_debug
from .grammar_proofread_locale import (
    looks_complete_sentence,
)


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
    n_start: int = 0
    checker_identity: str = ""
    paragraph_text: str = ""


def grammar_inflight_key(a_document_identifier: str, loc_key: str, sent_text: str, is_complete: bool) -> str:
    """Queue supersede key: distinct per sentence when complete; stable per document when incomplete.

    - Complete sentences use their text hash to avoid collisions across paragraphs.
    - Incomplete sentences use a fixed 'INCOMPLETE' key to prevent typing floods.
    """
    if is_complete:
        context = grammar_proofread_locale.fingerprint_for_text(sent_text)
    else:
        context = "INCOMPLETE_WRITER_AGENT_INTERNAL_STRING"
    return f"{a_document_identifier}|{loc_key}|{context}"


_ENQUEUE_SEQ_COUNTER = itertools.count(1)


def next_enqueue_seq() -> int:
    """Monotonic generation stamp for ``GrammarWorkItem.enqueue_seq`` (supersede / stale detection)."""
    return next(_ENQUEUE_SEQ_COUNTER)


def enqueue_misses(
    work_queue: Any,
    ctx: Any,
    a_doc_id: str,
    loc_key: str,
    uncached_spans: list[tuple[int, int, str]],
    provider: str = "",
    checker_identity: str = "",
    paragraph_text: str = "",
) -> None:
    """Enqueue uncached sentences for background processing."""
    count = len(uncached_spans)
    if count:
        label = f"{count} sentence" if count == 1 else f"{count} sentences"
        get_grammar_status().start(label, result="queued", routine=True)
    for sent_start, sent_end, sent_text in uncached_spans:
        seq = next_enqueue_seq()
        complete_sentence = looks_complete_sentence(sent_text)
        inflight_key = grammar_inflight_key(a_doc_id, loc_key, sent_text, complete_sentence)
        grammar_obs(
            "do_proofreading_enqueue",
            doc_id=a_doc_id,
            grammar_bcp47=loc_key,
            inflight_key=inflight_key,
            enqueue_seq=seq,
            n_start=sent_start,
            n_end=sent_end,
            slice_len=len(sent_text),
            partial_sentence_arg=not complete_sentence,
        )
        work_queue.enqueue(
            GrammarWorkItem(
                ctx=ctx,
                text=sent_text,
                grammar_bcp47=loc_key,
                partial_sentence=not complete_sentence,
                doc_id=a_doc_id,
                inflight_key=inflight_key,
                enqueue_seq=seq,
                provider=provider,
                n_start=sent_start,
                checker_identity=checker_identity,
                paragraph_text=paragraph_text,
            )
        )


class GrammarWorkQueue:
    """Multi-worker queue for grammar LLM requests (stampede + per-key supersede).

    Up to ``doc.grammar_proofreader_max_in_flight`` daemon drain workers share one
    ``queue.Queue``; each batch still respects ``grammar_llm_request_gate`` for HTTP.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[GrammarWorkItem | None] = queue.Queue()
        self._lock: threading.Lock = threading.Lock()
        self._latest_seq: dict[str, int] = {}
        self._worker_count: int = 0
        self._stopping: bool = False

    def inflight_superseded(self, inflight_key: str, enqueue_seq: int) -> bool:
        """True if a newer grammar enqueue has been recorded for this key (e.g. user kept typing)."""
        with self._lock:
            latest = self._latest_seq.get(inflight_key)
            return latest is not None and enqueue_seq < latest

    def filter_stale_and_group(
        self,
        items: list[GrammarWorkItem],
    ) -> dict[tuple[str, str, str, str], list[GrammarWorkItem]]:
        """Drop stale items and group the rest by ``(doc_id, grammar_bcp47, provider, checker_identity)``."""
        groups: dict[tuple[str, str, str, str], list[GrammarWorkItem]] = defaultdict(list)
        stale_count = 0
        for item in items:
            if self.inflight_superseded(item.inflight_key, item.enqueue_seq):
                grammar_obs(
                    "queue_stale_skip",
                    doc_id=item.doc_id,
                    locale=item.grammar_bcp47,
                    seq=item.enqueue_seq,
                    inflight_key=item.inflight_key,
                )
                stale_count += 1
                continue
            groups[(item.doc_id, item.grammar_bcp47, item.provider, item.checker_identity)].append(item)
        if stale_count > 0:
            grammar_obs(
                "batch_stats",
                sentences_stale_skipped=stale_count,
                survivor_count=sum(len(v) for v in groups.values()),
            )
        return dict(groups)

    def note_inflight_generation(self, inflight_key: str, enqueue_seq: int) -> bool:
        """Record a retargeted generation. True when this seq is already stale.

        In-place locale changes do not ``enqueue`` (the worker keeps the item).
        The new key still has to land in ``_latest_seq`` or a later enqueue of
        that sentence cannot supersede it. An older seq does not replace a
        newer one, so the older generation stays droppable.
        """
        return self._record_generation(inflight_key, enqueue_seq) is not None

    def _record_generation(self, inflight_key: str, enqueue_seq: int) -> int | None:
        """Record *enqueue_seq* unless older; return the newer latest seq when stale, else None."""
        with self._lock:
            latest = self._latest_seq.get(inflight_key)
            if latest is not None and enqueue_seq < latest:
                return latest
            self._latest_seq[inflight_key] = enqueue_seq
            return None

    def forget_document(self, doc_id: str) -> None:
        """Drop generation stamps for a closed document (keys start with ``doc_id + '|'``)."""
        if not doc_id:
            return
        prefix = f"{doc_id}|"
        with self._lock:
            for key in [k for k in self._latest_seq if k.startswith(prefix)]:
                del self._latest_seq[key]

    def enqueue(self, item: GrammarWorkItem) -> None:
        """Add a work item; starts the drain worker on first call.

        Records the seq in the supersede map (``_latest_seq``) first, so any
        older snapshot of the same key is dropped as stale wherever it is.
        """
        newer_seq = self._record_generation(item.inflight_key, item.enqueue_seq)
        if newer_seq is not None:
            log.error(
                "[grammar] queue enqueue: out-of-order seq detected for key=%s: incoming seq=%s < latest seq=%s; stale detection may be unreliable",
                item.inflight_key,
                item.enqueue_seq,
                newer_seq,
            )
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

        if item.doc_id and item.grammar_bcp47:
            from .grammar_persistence import grammar_registry

            grammar_registry.record_enqueued_locale(item.doc_id, item.grammar_bcp47)
        self._q.put(item)
        self._ensure_workers()

    def stop(self) -> None:
        """Retire every drain worker; runs as a quit hook.

        After this call no batch is dispatched: workers already waiting leave on
        their sentinel, a worker that is mid-batch returns from the batch and
        then leaves, and whatever is still queued or debouncing is dropped.
        Nothing is joined: harper-ls and the LLM are not waited on at quit.
        ``_worker_count`` is read under the lock that ``_ensure_workers``
        increments it under, so every started worker gets exactly one sentinel.
        """
        with self._lock:
            self._stopping = True
            count = self._worker_count
        for _unused in range(count):
            self._q.put(None)

    def _ensure_workers(self) -> None:
        from plugin.framework.worker_pool import run_in_background

        desired = grammar_proofread_locale.grammar_max_in_flight()
        while True:
            with self._lock:
                if self._stopping or self._worker_count >= desired:
                    return
                i = self._worker_count
                self._worker_count += 1
            # Infinite loop: a dedicated daemon thread, never a shared pool slot.
            run_in_background(self._drain_loop, name=f"writeragent-grammar-queue-{i}", dedicated=True)

    def _drain_loop(self) -> None:
        """Block-dequeue, batch-drain pending items (newest seq per key), process one batch.

        ``None`` is the sentinel ``stop`` puts once per worker; the worker returns without dispatching.
        """
        completes: dict[str, GrammarWorkItem] = {}
        partials: dict[str, tuple[float, GrammarWorkItem]] = {}

        def _absorb(item: GrammarWorkItem) -> None:
            if item.partial_sentence:
                prev_partial = partials.get(item.inflight_key)
                if prev_partial is None or item.enqueue_seq > prev_partial[1].enqueue_seq:
                    deadline = time.monotonic() + grammar_proofread_locale.GRAMMAR_WORKER_PAUSE_TIMEOUT_S
                    partials[item.inflight_key] = (deadline, item)
            else:
                prev = completes.get(item.inflight_key)
                if prev is None or item.enqueue_seq > prev.enqueue_seq:
                    completes[item.inflight_key] = item

        def _dispatch(batch: list[GrammarWorkItem]) -> None:
            if not batch or self._stopping:
                return
            grammar_obs(
                "queue_drain_batch",
                batch_size=len(batch),
                seqs=tuple(x.enqueue_seq for x in batch),
                keys=tuple(x.inflight_key for x in batch),
            )
            groups = self.filter_stale_and_group(batch)
            for (doc_id, locale, _prov, _ident), group_items in groups.items():
                grammar_obs(
                    "queue_execute_batch",
                    doc_id=doc_id,
                    locale=locale,
                    item_count=len(group_items),
                )
                import plugin.writer.locale.grammar_worker as grammar_worker

                grammar_worker.run_llm_and_cache_batch(group_items, grammar_queue_instance=self)

        def _slurp(deadline: float) -> bool:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    nxt = self._q.get(timeout=remaining)
                except queue.Empty:
                    return False
                if nxt is None:
                    return True
                _absorb(nxt)

        while True:
            try:
                # 1. Dispatch partial sentences whose quiet period has expired.
                now = time.monotonic()
                ready_keys = [k for k, (dl, _it) in partials.items() if dl <= now]
                if ready_keys:
                    ready_items = [partials.pop(k)[1] for k in ready_keys]
                    _dispatch(ready_items)
                    continue

                # 2. Wait for incoming items if completes is empty.
                if not completes:
                    if partials:
                        wait_timeout = max(0.0, min(dl for dl, _it in partials.values()) - time.monotonic())
                    else:
                        wait_timeout = None

                    try:
                        first = self._q.get(timeout=wait_timeout)
                    except queue.Empty:
                        continue

                    if first is None:
                        return

                    _absorb(first)

                # 3. If completes has items, coalesce for fixed window.
                if completes:
                    # Fixed coalesce window (deadline does not move when more items arrive)
                    coalesce_deadline = time.monotonic() + grammar_proofread_locale.GRAMMAR_COMPLETE_COALESCE_S
                    if _slurp(coalesce_deadline):
                        return

                    batch = list(completes.values())
                    completes.clear()
                    _dispatch(batch)

            except Exception:
                log.exception("[grammar] drain loop error")


grammar_queue = GrammarWorkQueue()
register_quit_hook(grammar_queue.stop)
