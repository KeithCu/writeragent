# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grammar work queue: work items, batch dedup, pure enqueue/stale helpers, sequential LLM worker."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

from .grammar_proofread_cache import cache_get_sentence, cache_put_sentence, ignored_rules_snapshot
from .grammar_proofread_locale import (
    GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS,
    GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS,
    GRAMMAR_SYSTEM_PROMPT_TEMPLATE,
    GRAMMAR_WORKER_PAUSE_TIMEOUT_S,
    looks_complete_sentence,
    parse_grammar_json,
)
from .grammar_proofread_text import normalize_errors_for_text, split_into_sentences

log = logging.getLogger("writeragent.grammar")
log.setLevel(logging.DEBUG)


@dataclass(frozen=True)
class GrammarWorkItem:
    """One queued grammar job (defined here so dedup tests avoid UNO imports)."""

    ctx: Any
    full_text: str
    n_start: int
    n_end: int
    grammar_bcp47: str
    partial_sentence: bool
    doc_id: str
    inflight_key: str
    enqueue_seq: int
    # Main-thread sentence text from doProofreading; when set, worker skips split_into_sentences
    # on the slice so substring BreakIterator cannot disagree with cache keys (see _run_llm_and_cache).
    proofread_sentence_text: str = ""


def deduplicate_grammar_batch(batch: list[GrammarWorkItem]) -> list[GrammarWorkItem]:
    """Return one queue item per ``inflight_key``, keeping the highest ``enqueue_seq``."""
    # --- Cross-sentence prefix bug (fixed): older code had a *second* pass that grouped
    # by (doc_id, locale) and dropped slice A if slice B was a string-prefix extension
    # of A (newest enqueue_seq wins). That wrongly dropped sentence 1 when sentence 2's
    # text started with sentence 1's text (e.g. "No." vs "No problem today.") — different
    # inflight_key values, unrelated timelines. One sentence while typing = one key.
    #
    # Do not add cross-key slice-text prefix logic here; tail-replace + this loop suffice.
    #
    # Alternatives if you redesign: (1) prefix-newest-wins restricted to *same*
    # inflight_key only — usually redundant after this map; (2) span-aware dedup using
    # overlapping [n_start,n_end); (3) keep distinct-key slices independent (current).
    # Regression: test_two_sentences_string_prefix_collision_both_survive.
    best_by_key: dict[str, GrammarWorkItem] = {}
    for item in batch:
        prev = best_by_key.get(item.inflight_key)
        # Same physical sentence / typing line: inflight_key matches → keep newer snapshot only.
        if prev is None or item.enqueue_seq > prev.enqueue_seq:
            best_by_key[item.inflight_key] = item
        elif prev is not None and item.enqueue_seq < prev.enqueue_seq:
            log.info("[grammar] queue dedup: dropped older same-key item seq=%s key=%s (newer seq=%s kept)", item.enqueue_seq, item.inflight_key, prev.enqueue_seq)
    return list(best_by_key.values())


TailEnqueueOp = Literal["replace_tail", "append", "skip_tail"]


def record_enqueue_latest(prev: dict[str, int], item: GrammarWorkItem) -> tuple[dict[str, int], bool, int | None]:
    """Return updated ``latest_seq``, whether incoming seq was out-of-order, and prior seq for logging."""
    key = item.inflight_key
    prev_seq = prev.get(key)
    out_of_order = prev_seq is not None and item.enqueue_seq < prev_seq
    new_d = dict(prev)
    new_d[key] = item.enqueue_seq
    return new_d, out_of_order, prev_seq if out_of_order else None


def is_stale(latest_seq: Mapping[str, int], item: GrammarWorkItem) -> bool:
    """True if a newer enqueue has been recorded for this ``inflight_key``."""
    latest = latest_seq.get(item.inflight_key)
    return latest is not None and item.enqueue_seq < latest


def inflight_superseded(latest_seq: Mapping[str, int], inflight_key: str, enqueue_seq: int) -> bool:
    """True if ``enqueue_seq`` is older than the latest known generation for ``inflight_key``."""
    latest = latest_seq.get(inflight_key)
    return latest is not None and enqueue_seq < latest


def tail_enqueue_operation(tail: GrammarWorkItem | None, incoming: GrammarWorkItem) -> TailEnqueueOp:
    """O(1) tail decision: replace newest same-key, append different key, or skip stale same-key."""
    if tail is None:
        return "append"
    if tail.inflight_key != incoming.inflight_key:
        return "append"
    if incoming.enqueue_seq > tail.enqueue_seq:
        return "replace_tail"
    return "skip_tail"


_ENQUEUE_SEQ_LOCK = threading.Lock()
_ENQUEUE_SEQ = 0


def next_enqueue_seq() -> int:
    """Monotonic generation stamp for ``GrammarWorkItem.enqueue_seq`` (supersede / stale detection)."""
    global _ENQUEUE_SEQ
    with _ENQUEUE_SEQ_LOCK:
        _ENQUEUE_SEQ += 1
        return _ENQUEUE_SEQ


def grammar_obs(event: str, **fields: Any) -> None:
    """DEBUG-only observability for queue / worker (grep ``[grammar] obs`` in logs)."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.debug("[grammar] obs %s %s", event, kv)


def slice_preview_debug(text: str, max_len: int = 72) -> str:
    """Compact one-line preview for DEBUG logs (avoid dumping huge paragraphs)."""
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}…"


def _grammar_text_preview(text: str) -> str:
    words = text.strip().split()
    return " ".join(words[:3]) if words else "(empty)"


def emit_grammar_status(phase: str, text: str, *, result: str = "", elapsed_ms: int | None = None) -> None:
    try:
        from plugin.framework.event_bus import global_event_bus

        global_event_bus.emit("grammar:status", phase=phase, preview=_grammar_text_preview(text), length=len(text), result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        log.debug("[grammar] status emit failed: %s", e, exc_info=True)


def run_llm_and_cache(
    ctx: Any,
    full_text: str,
    n_start: int,
    n_end: int,
    enqueue_seq: int,
    inflight_key: str,
    grammar_bcp47: str,
    partial_sentence: bool = False,
    *,
    proofread_sentence_text: str = "",
    grammar_queue: Any | None = None,
) -> None:
    """Process one queue item: LLM request(s) + sentence cache write(s)."""
    try:
        from plugin.framework.config import get_api_config, get_config_bool, get_config_str, get_text_model
        from plugin.framework.queue_executor import is_agent_active, llm_request_lane
        from plugin.framework.client.llm_client import LlmClient

        from .grammar_proofread_locale import grammar_english_name_for_bcp47

        gq = grammar_queue or _grammar_queue_singleton

        try:
            if not get_config_bool(ctx, "doc.grammar_proofreader_enabled"):
                grammar_obs("worker_skip", reason="grammar_disabled_after_enqueue", enqueue_seq=enqueue_seq, inflight_key=inflight_key)
                return
        except Exception as e:
            log.warning("[grammar] worker: get_config_bool enabled: %s", e, exc_info=True)
            return
        try:
            pause_during_agent = get_config_bool(ctx, "doc.grammar_proofreader_pause_during_agent")
        except Exception as e:
            log.warning("[grammar] worker: get_config_bool pause_during_agent: %s", e, exc_info=True)
            pause_during_agent = False
        if pause_during_agent and is_agent_active():
            log.info("[grammar] worker skipped: agent active and pause_during_agent enabled")
            grammar_obs("worker_skip", reason="pause_during_agent", enqueue_seq=enqueue_seq, inflight_key=inflight_key)
            return
        slice_txt = full_text[n_start:n_end]
        grammar_obs("worker_slice", enqueue_seq=enqueue_seq, inflight_key=inflight_key, grammar_bcp47=grammar_bcp47, partial_sentence=partial_sentence, n_start=n_start, n_end=n_end, slice_len=len(slice_txt), slice_preview=slice_preview_debug(slice_txt))
        if len(slice_txt) > GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS:
            log.info("[grammar] worker skipped: slice len %s > GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS %s", len(slice_txt), GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS)
            grammar_obs("worker_skip", reason="slice_exceeds_safety_max_chars", slice_len=len(slice_txt), max_chars=GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS)
            return
        if proofread_sentence_text:
            if proofread_sentence_text != slice_txt:
                log.warning("[grammar] worker slice mismatch (using enqueue proofread_sentence_text) n_start=%s n_end=%s", n_start, n_end)
            to_process = [proofread_sentence_text]
            grammar_obs("worker_use_enqueue_sentence_text", enqueue_seq=enqueue_seq, len_proof=len(proofread_sentence_text))
        else:
            sentences = split_into_sentences(ctx, grammar_bcp47, slice_txt)
            if not sentences:
                to_process = [slice_txt]
                grammar_obs("worker_split_fallback_whole_slice", enqueue_seq=enqueue_seq, slice_len=len(slice_txt))
            else:
                to_process = [txt for _off, txt in sentences]
                if len(to_process) > 1:
                    log.info("[grammar] worker: slice split into %s parts; processing each separately", len(to_process))
                    grammar_obs("worker_multi_fragment_slice", enqueue_seq=enqueue_seq, fragment_count=len(to_process))

        uncached_texts: list[str] = []
        for sent_text in to_process:
            if cache_get_sentence(grammar_bcp47, sent_text) is None:
                uncached_texts.append(sent_text)
        if not uncached_texts:
            log.info("[grammar] worker skipped: all sentence(s) already cached (race) len=%s", len(slice_txt))
            grammar_obs("worker_skip", reason="all_sentences_cached_race", enqueue_seq=enqueue_seq, slice_len=len(slice_txt))
            return

        max_tok = GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS
        try:
            model = get_config_str(ctx, "doc.grammar_proofreader_model").strip() or get_text_model(ctx)
        except Exception as e:
            log.warning("[grammar] worker: model resolution: %s", e, exc_info=True)
            model = ""
        _lang = grammar_english_name_for_bcp47(grammar_bcp47)
        base_sys = GRAMMAR_SYSTEM_PROMPT_TEMPLATE.format(lang_name=_lang, bcp47=grammar_bcp47)

        client = LlmClient(get_api_config(ctx), ctx)
        total_norms = 0
        for sent_text in uncached_texts:
            llm_text = sent_text
            if len(llm_text) > GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS:
                llm_text = llm_text[:GRAMMAR_PROOFREAD_SAFETY_MAX_CHARS]
                grammar_obs("worker_sentence_truncated_to_safety", enqueue_seq=enqueue_seq, truncated_len=len(llm_text))
            use_partial = partial_sentence or not looks_complete_sentence(llm_text)
            sys_prompt = base_sys
            if use_partial:
                sys_prompt += " The input may be a partial sentence; prefer conservative grammar suggestions and avoid broad rewrites."
            grammar_obs("worker_llm_request_prepare", enqueue_seq=enqueue_seq, llm_text_len=len(llm_text), llm_preview=slice_preview_debug(llm_text, 96))
            messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": llm_text}]
            log.info(
                "[grammar] LLM request (one sentence) llm_text_len=%s max_tokens=%s model=%s",
                len(llm_text),
                max_tok,
                model or "(default text model)",
            )
            request_start = time.monotonic()
            emit_grammar_status("request", llm_text, result="LLM request")
            with llm_request_lane():
                content = client.chat_completion_sync(messages, max_tokens=max_tok, model=model or None, response_format={"type": "json_object"}, prepend_dev_build_system_prefix=False)
            elapsed_ms = int((time.monotonic() - request_start) * 1000)
            log.debug("[grammar] LLM raw response length=%s", len(content or ""))
            if gq.inflight_superseded(inflight_key, enqueue_seq):
                log.info("[grammar] worker skip cache_put: superseded during LLM seq=%s key=%s", enqueue_seq, inflight_key)
                grammar_obs("worker_skip", reason="superseded_during_llm", enqueue_seq=enqueue_seq, inflight_key=inflight_key)
                continue
            items = parse_grammar_json(content or "")
            log.info("[grammar] parsed %s error item(s) from JSON", len(items))
            ignored = ignored_rules_snapshot()
            norms = normalize_errors_for_text(llm_text, 0, len(llm_text), items, ignored, ctx, grammar_bcp47)
            total_norms += len(norms)
            sent_errors = [asdict(n) for n in norms]
            cache_put_sentence(grammar_bcp47, llm_text, sent_errors)
            issue_word = "issue" if len(norms) == 1 else "issues"
            emit_grammar_status("complete", llm_text, result=f"{len(norms)} {issue_word}", elapsed_ms=elapsed_ms)

        log.info("[grammar] cached errors for %s uncached sentence(s), slice len=%s", len(uncached_texts), len(slice_txt))
        grammar_obs(
            "worker_cache_put_done",
            enqueue_seq=enqueue_seq,
            uncached_sentence_count=len(uncached_texts),
            normalized_issue_count=total_norms,
            slice_len=len(slice_txt),
        )
    except Exception as e:
        log.error("[grammar] worker failed: %s", e, exc_info=True)
        try:
            emit_grammar_status("failed", full_text[n_start:n_end], result=type(e).__name__)
        except Exception:
            pass


class GrammarWorkQueue:
    """Single-worker sequential queue for grammar LLM requests (stampede + per-key supersede)."""

    def __init__(self) -> None:
        self._q: queue.Queue[GrammarWorkItem | None] = queue.Queue()
        self._seq_lock = threading.Lock()
        self._latest_seq: dict[str, int] = {}
        self._worker_started = False
        self._worker_lock = threading.Lock()

    @staticmethod
    def _slice_preview(item: GrammarWorkItem, max_len: int = 48) -> str:
        slice_txt = item.full_text[item.n_start : item.n_end]
        compact = " ".join(slice_txt.split())
        if len(compact) <= max_len:
            return compact
        return f"{compact[:max_len]}…"

    def _latest_seq_for(self, inflight_key: str) -> int | None:
        with self._seq_lock:
            return self._latest_seq.get(inflight_key)

    def _is_stale(self, item: GrammarWorkItem) -> bool:
        with self._seq_lock:
            return is_stale(self._latest_seq, item)

    def inflight_superseded(self, inflight_key: str, enqueue_seq: int) -> bool:
        """True if a newer grammar enqueue has been recorded for this key (e.g. user kept typing)."""
        with self._seq_lock:
            return inflight_superseded(self._latest_seq, inflight_key, enqueue_seq)

    def enqueue(self, item: GrammarWorkItem) -> None:
        """Add a work item; starts the drain worker on first call."""
        with self._seq_lock:
            self._latest_seq, out_of_order, superseded_prev_seq = record_enqueue_latest(self._latest_seq, item)
            if out_of_order:
                log.error("[grammar] queue enqueue: out-of-order seq detected for key=%s: incoming seq=%s < latest seq=%s; stale detection may be unreliable", item.inflight_key, item.enqueue_seq, superseded_prev_seq)
        log.info("[grammar] queue enqueue doc_id=%s locale=%s seq=%s key=%s len=%s preview=%r", item.doc_id, item.grammar_bcp47, item.enqueue_seq, item.inflight_key, len(item.full_text[item.n_start : item.n_end]), self._slice_preview(item))
        grammar_obs("queue_enqueue", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, inflight_key=item.inflight_key, n_start=item.n_start, n_end=item.n_end, slice_len=len(item.full_text[item.n_start : item.n_end]), partial_sentence=item.partial_sentence, preview=slice_preview_debug(item.full_text[item.n_start : item.n_end]))  # fmt: skip

        with self._q.mutex:
            tail = self._q.queue[-1] if self._q.queue else None
            op = tail_enqueue_operation(tail, item)
            if op == "replace_tail":
                assert tail is not None
                log.info("[grammar] queue replace-at-tail key=%s: seq=%s replacing older seq=%s", item.inflight_key, item.enqueue_seq, tail.enqueue_seq)
                grammar_obs("queue_replace_tail", inflight_key=item.inflight_key, new_seq=item.enqueue_seq, old_seq=tail.enqueue_seq)
                self._q.queue[-1] = item
            elif op == "append":
                self._q.queue.append(item)
                self._q.unfinished_tasks += 1
                self._q.not_empty.notify()
            else:
                log.info("[grammar] queue skip-stale-tail key=%s: incoming seq=%s <= existing seq=%s", item.inflight_key, item.enqueue_seq, tail.enqueue_seq if tail else None)

        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_started:
                return
            self._worker_started = True
        t = threading.Thread(target=self._drain_loop, name="writeragent-grammar-queue", daemon=True)
        t.start()

    def _drain_loop(self) -> None:
        """Block-dequeue, batch-drain pending items, deduplicate, process sequentially."""
        while True:
            first = self._q.get()
            if first is None:
                break
            batch: list[GrammarWorkItem] = [first]
            while True:
                try:
                    more = self._q.get(timeout=GRAMMAR_WORKER_PAUSE_TIMEOUT_S)
                    if more is None:
                        return
                    batch.append(more)
                except queue.Empty:
                    break
            log.info("[grammar] queue drain: batch_size=%s", len(batch))
            grammar_obs("queue_drain_batch", batch_size=len(batch), seqs=tuple(x.enqueue_seq for x in batch), keys=tuple(x.inflight_key for x in batch))
            survivors = deduplicate_grammar_batch(batch)
            log.info("[grammar] queue drain: survivors=%s", len(survivors))
            grammar_obs("queue_drain_survivors", survivor_count=len(survivors), seqs=tuple(x.enqueue_seq for x in survivors))
            for item in survivors:
                latest = self._latest_seq_for(item.inflight_key)
                if self._is_stale(item):
                    log.info("[grammar] queue stale-skip doc_id=%s locale=%s seq=%s latest=%s key=%s preview=%r", item.doc_id, item.grammar_bcp47, item.enqueue_seq, latest, item.inflight_key, self._slice_preview(item))
                    grammar_obs("queue_stale_skip", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, latest_seq=latest, inflight_key=item.inflight_key)
                    continue
                try:
                    log.info("[grammar] queue execute doc_id=%s locale=%s seq=%s latest=%s key=%s len=%s preview=%r", item.doc_id, item.grammar_bcp47, item.enqueue_seq, latest, item.inflight_key, len(item.full_text[item.n_start : item.n_end]), self._slice_preview(item))
                    grammar_obs("queue_execute", doc_id=item.doc_id, locale=item.grammar_bcp47, seq=item.enqueue_seq, latest_seq=latest, inflight_key=item.inflight_key, n_start=item.n_start, n_end=item.n_end, slice_len=len(item.full_text[item.n_start : item.n_end]), partial_sentence=item.partial_sentence)
                    run_llm_and_cache(
                        item.ctx,
                        item.full_text,
                        item.n_start,
                        item.n_end,
                        item.enqueue_seq,
                        item.inflight_key,
                        item.grammar_bcp47,
                        partial_sentence=item.partial_sentence,
                        proofread_sentence_text=item.proofread_sentence_text,
                        grammar_queue=self,
                    )
                except Exception as e:
                    log.error("[grammar] queue worker item failed: %s", e, exc_info=True)


_grammar_queue_singleton = GrammarWorkQueue()

grammar_queue: GrammarWorkQueue = _grammar_queue_singleton
